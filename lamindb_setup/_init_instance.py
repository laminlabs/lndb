import importlib
import sys
from pathlib import Path
from typing import Optional, Union
from uuid import UUID, uuid4
import os
from lamin_utils import logger
from pydantic import PostgresDsn
from django.db.utils import OperationalError, ProgrammingError
from django.core.exceptions import FieldError
from lamindb_setup.dev.upath import LocalPathClasses, UPath
from ._close import close as close_instance
from ._docstrings import instance_description as description
from ._settings import settings
from ._silence_loggers import silence_loggers
from .dev import InstanceSettings
from .dev._docs import doc_args
from .dev._settings_storage import StorageSettings
from .dev.upath import create_path
from ._init_vault import _init_vault


def get_schema_module_name(schema_name) -> str:
    import importlib.util

    name_attempts = [f"lnschema_{schema_name.replace('-', '_')}", schema_name]
    for name in name_attempts:
        module_spec = importlib.util.find_spec(name)
        if module_spec is not None:
            return name
    raise ImportError(
        f"Python package for '{schema_name}' is not installed, tried two package names:"
        f" {name_attempts}\nHave you installed the schema package using `pip install`?"
    )


def register_storage(ssettings: StorageSettings):
    from lnschema_core.models import Storage
    from lnschema_core.users import current_user_id

    storage, created = Storage.objects.update_or_create(
        root=ssettings.root_as_str,
        defaults=dict(
            root=ssettings.root_as_str,
            type=ssettings.type,
            region=ssettings.region,
            created_by_id=current_user_id(),
        ),
    )
    if created:
        logger.save(f"saved: {storage}")
    return storage


def register_user(usettings):
    from lnschema_core.models import User

    if usettings.handle != "laminapp-admin":
        try:
            # need to have try except because of integer primary key migration
            user, created = User.objects.update_or_create(
                uid=usettings.uid,
                defaults=dict(
                    handle=usettings.handle,
                    name=usettings.name,
                ),
            )
            if created:
                logger.save(f"saved: {user}")
        # for users with only read access, except via ProgrammingError
        # ProgrammingError: permission denied for table lnschema_core_user
        except (OperationalError, FieldError, ProgrammingError):
            pass


def register_user_and_storage(isettings: InstanceSettings, usettings):
    """Register user & storage in DB."""
    from django.db.utils import OperationalError

    try:
        register_user(usettings)
        register_storage(isettings.storage)
    except OperationalError as error:
        logger.warning(f"instance seems not set up ({error})")


def reload_schema_modules(isettings: InstanceSettings):
    schema_names = ["core"] + list(isettings.schema)
    schema_module_names = [get_schema_module_name(n) for n in schema_names]

    for schema_module_name in schema_module_names:
        if schema_module_name in sys.modules:
            schema_module = importlib.import_module(schema_module_name)
            importlib.reload(schema_module)


def reload_lamindb(isettings: InstanceSettings):
    # only touch lamindb if we're operating from lamindb
    reload_schema_modules(isettings)
    if "lamindb" in sys.modules:
        import lamindb

        importlib.reload(lamindb)
    verbosity = logger._verbosity
    logger.set_verbosity(2)
    logger.success(f"loaded instance: {isettings.identifier}")
    logger.set_verbosity(verbosity)


ERROR_SQLITE_CACHE = """
Your cached local SQLite file exists, while your cloud SQLite file ({}) doesn't.
Either delete your cache ({}) or add it back to the cloud (if delete was accidental).
"""


@doc_args(
    description.storage_root,
    description.name,
    description.db,
    description.schema,
)
def init(
    *,
    storage: Union[str, Path, UPath],
    name: Optional[str] = None,
    db: Optional[PostgresDsn] = None,
    schema: Optional[str] = None,
    _test: bool = False,
    _vault: bool = False,
) -> None:
    """Creating and loading a LaminDB instance.

    Args:
        storage: {}
        name: {}
        db: {}
        schema: {}
    """
    # clean up in next refactor
    # avoid circular import
    from ._load_instance import load
    from .dev._hub_core import init_instance as init_instance_hub
    from .dev._hub_utils import (
        get_storage_region,
        validate_db_arg,
        validate_schema_arg,
        validate_storage_root_arg,
    )

    #
    if name is not None and "/" in name:
        logger.warning(
            "Please provide a valid instance name: '/' delimiter not allowed."
        )
        raise ValueError("Invalid instance name: '/' delimiter not allowed.")

    assert settings.user.uid  # check user is logged in
    owner = settings.user.handle

    if settings._instance_exists:
        close_instance()

    schema = validate_schema_arg(schema)
    validate_storage_root_arg(str(storage))
    validate_db_arg(db)
    if storage is None:
        raise ValueError("Pass storage argument")

    name_str = infer_instance_name(storage=storage, name=name, db=db)
    # test whether instance exists by trying to load it
    response = load(
        f"{owner}/{name_str}", _raise_not_reachable_error=False, _test=_test
    )
    if response is None:
        return None  # successful load!

    # for internal use when creating instances through CICD
    if isinstance(response, tuple) and response[0] == "instance-corrupted-or-deleted":
        hub_result = response[1]
        response = response[0]
        instance_id = UUID(hub_result["id"])
    else:
        instance_id_str = os.getenv("LAMINDB_INSTANCE_ID_INIT")
        if instance_id_str is None:
            instance_id = uuid4()
        else:
            instance_id = UUID(instance_id_str)

    isettings = InstanceSettings(
        id=instance_id,
        owner=owner,
        name=name_str,
        storage_root=storage,
        storage_region=get_storage_region(storage),  # type: ignore
        db=db,
        schema=schema,
    )

    if isettings._is_cloud_sqlite:
        if (
            not isettings._sqlite_file.exists()
            and isettings._sqlite_file_local.exists()
        ):
            raise RuntimeError(
                ERROR_SQLITE_CACHE.format(
                    isettings._sqlite_file, isettings._sqlite_file_local
                )
            )

    if isettings.is_remote and response != "instance-corrupted-or-deleted":
        from importlib import metadata

        try:
            lamindb_version = metadata.version("lamindb")
        except metadata.PackageNotFoundError:
            lamindb_version = None
        result = init_instance_hub(
            id=instance_id,
            name=name_str,
            storage=str(storage),
            db=db,
            schema=schema,
            lamindb_version=lamindb_version,
        )
        if not isinstance(result, UUID):
            raise RuntimeError(f"Registering instance on hub failed:\n{result}")
        logger.save(f"registered instance on hub: https://lamin.ai/{owner}/{name_str}")

        if db is not None and _vault:
            _init_vault(db=db, instance_id=result)

    if _test:
        isettings._persist()
        return None

    silence_loggers()

    isettings._init_db()
    load_from_isettings(isettings, init=True)
    if isettings._is_cloud_sqlite:
        isettings._cloud_sqlite_locker.lock()
        logger.warning(
            "locked instance (to unlock and push changes to the cloud SQLite file,"
            " call: lamin close)"
        )
    if not isettings.is_remote:
        verbosity = logger._verbosity
        logger.set_verbosity(4)
        logger.info("did not register local instance on hub")
        logger.set_verbosity(verbosity)
    return None


def load_from_isettings(
    isettings: InstanceSettings,
    *,
    init: bool = False,
) -> None:
    from .dev._setup_bionty_sources import load_bionty_sources, write_bionty_sources

    if init:
        # during init both user and storage need to be registered
        register_user_and_storage(isettings, settings.user)
        write_bionty_sources(isettings)
        isettings._update_cloud_sqlite_file(unlock_cloud_sqlite=False)
    else:
        # when loading, django is already set up
        #
        # only register user if the instance is loaded
        # for the first time in an environment
        # this is our best proxy for that the user might not
        # yet be registered
        if not isettings._get_settings_file().exists():
            register_user(settings.user)
        load_bionty_sources(isettings)
    isettings._persist()
    reload_lamindb(isettings)


def infer_instance_name(
    *,
    storage: Union[str, Path, UPath],
    name: Optional[str] = None,
    db: Optional[PostgresDsn] = None,
):
    if name is not None:
        return name
    if db is not None:
        # better way of accessing the database name?
        return str(db).split("/")[-1]

    storage_path = create_path(storage)
    if isinstance(storage_path, LocalPathClasses):
        name = storage_path.stem
    else:
        name = storage_path._url.netloc
    name = name.lower()

    return name
