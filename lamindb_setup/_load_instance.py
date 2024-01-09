from pathlib import Path
from typing import Optional, Union, Dict, Tuple
from uuid import UUID
import os
from lamin_utils import logger
from lamindb_setup.dev.upath import UPath
from lamindb_setup.dev._hub_utils import (
    LaminDsn,
    LaminDsnModel,
)
from ._close import close as close_instance
from ._init_instance import load_from_isettings
from ._settings import InstanceSettings, settings
from ._silence_loggers import silence_loggers
from .dev._settings_load import load_instance_settings
from .dev._settings_storage import StorageSettings
from .dev._settings_store import instance_settings_file
from .dev.cloud_sqlite_locker import unlock_cloud_sqlite_upon_exception

# this is for testing purposes only
# set to True only to test failed load
_TEST_FAILED_LOAD = False


def check_db_dsn_equal_up_to_credentials(db_dsn_hub, db_dsn_local):
    return (
        db_dsn_hub.scheme == db_dsn_local.scheme
        and db_dsn_hub.host == db_dsn_local.host
        and db_dsn_hub.database == db_dsn_local.database
        and db_dsn_hub.port == db_dsn_local.port
    )


def update_db_using_local(
    hub_instance_result: Dict[str, str], settings_file: Path, db: Optional[str] = None
) -> Optional[str]:
    db_updated = None
    # check if postgres
    if hub_instance_result["db_scheme"] == "postgresql":
        db_dsn_hub = LaminDsnModel(db=hub_instance_result["db"])
        if db is not None:
            db_dsn_local = LaminDsnModel(db=db)
        else:
            # read directly from the environment
            if os.getenv("LAMINDB_INSTANCE_DB") is not None:
                logger.important("loading db URL from env variable LAMINDB_INSTANCE_DB")
                db_dsn_local = LaminDsnModel(db=os.getenv("LAMINDB_INSTANCE_DB"))
            # read from a cached settings file
            elif settings_file.exists():
                isettings = load_instance_settings(settings_file)
                db_dsn_local = LaminDsnModel(db=isettings.db)
            else:
                # just take the default hub result and ensure there is actually a user
                if db_dsn_hub.db.user == "none" and db_dsn_hub.db.password == "none":
                    raise PermissionError(
                        "No database access, please ask your admin to provide you with"
                        " a DB URL and pass it via --db <db_url>"
                    )
                db_dsn_local = db_dsn_hub
        if not check_db_dsn_equal_up_to_credentials(db_dsn_hub.db, db_dsn_local.db):
            raise ValueError(
                "The local differs from the hub database information:"
                "\n 1. did you pass a wrong db URL with --db?"
                "\n 2. did your database get updated by an admin?"
            )
        db_updated = LaminDsn.build(
            scheme=db_dsn_hub.db.scheme,
            user=db_dsn_local.db.user,
            password=db_dsn_local.db.password,
            host=db_dsn_hub.db.host,
            port=db_dsn_hub.db.port,
            database=db_dsn_hub.db.database,
        )
    return db_updated


@unlock_cloud_sqlite_upon_exception(ignore_prev_locker=True)
def load(
    identifier: str,
    *,
    db: Optional[str] = None,
    storage: Optional[Union[str, Path, UPath]] = None,
    _raise_not_reachable_error: bool = True,
    _test: bool = False,
    _vault: bool = False,
) -> Optional[Union[str, Tuple]]:
    """Load existing instance.

    Args:
        identifier: `str` - The instance identifier `owner/name`.
            You can also pass the URL: `https://lamin.ai/owner/name`.
            If the instance is owned by you,
            it suffices to pass the instance name.
        db: Load the instance with an updated database URL.
        storage: `Optional[PathLike] = None` - Load the instance with an
            updated default storage.
    """
    from .dev._hub_core import load_instance as load_instance_from_hub

    owner, name = get_owner_name_from_identifier(identifier)

    # compare normalized identifier with a potentially previously loaded identifier
    if settings._instance_exists:
        if f"{owner}/{name}" == settings.instance.identifier:
            logger.important(f"instance already loaded: {settings.instance.identifier}")
            return None
        else:
            close_instance()

    settings_file = instance_settings_file(name, owner)

    # the following will return a string if the instance does not exist
    # on the hub
    hub_result = load_instance_from_hub(owner=owner, name=name)

    # if hub_result is not a string, it means it made a request
    # that successfully returned metadata
    if not isinstance(hub_result, str):
        instance_result, storage_result = hub_result
        db_updated = update_db_using_local(instance_result, settings_file, db=db)
        isettings = InstanceSettings(
            id=UUID(instance_result["id"]),
            owner=owner,
            name=name,
            storage_root=storage_result["root"],
            storage_region=storage_result["region"],
            db=db_updated,
            schema=instance_result["schema_str"],
        )
        # we don't need to use the vault for sqlite instances
        if instance_result["db"] is not None and _vault:
            isettings._db_from_vault = get_db_from_vault(
                instance_id=instance_result["id"],
                scheme=instance_result["db_scheme"],
                host=instance_result["db_host"],
                port=instance_result["db_port"],
                name=instance_result["db_database"],
            )
    else:
        error_message = (
            f"Instance {owner}/{name} not loadable from hub with response:"
            f" '{hub_result}'.\nCheck whether instance exists and you have access:"
            f" https://lamin.ai/{owner}/{name}?tab=collaborators"
        )
        if settings_file.exists():
            isettings = load_instance_settings(settings_file)
            if isettings.is_remote:
                if _raise_not_reachable_error:
                    raise RuntimeError(error_message)
                return "instance-not-reachable"
            logger.info(f"found cached instance metadata: {settings_file}")
        else:
            if _raise_not_reachable_error:
                raise RuntimeError(error_message)
            return "instance-not-reachable"
        # mimic instance_result from hub
        instance_result = {"id": isettings.id.hex}

    if storage is not None:
        update_isettings_with_storage(isettings, storage)
    if _test:
        isettings._persist()  # this is to test the settings
        return None
    silence_loggers()
    check, msg = isettings._load_db(
        do_not_lock_for_laminapp_admin=True
    )  # this also updates local SQLite
    if not check:
        local_db = isettings._is_cloud_sqlite and isettings._sqlite_file_local.exists()
        if local_db:
            logger.warning(
                "SQLite file does not exist in the cloud, but exists locally:"
                f" {isettings._sqlite_file_local}\nTo push the file to the cloud, call:"
                " lamin close"
            )
        elif _raise_not_reachable_error:
            raise RuntimeError(msg)
        else:
            logger.warning(
                f"instance exists with id {isettings.id.hex}, but database is not"
                " loadable: re-initializing"
            )
            return "instance-corrupted-or-deleted", instance_result
    # this is for testing purposes only
    if _TEST_FAILED_LOAD:
        raise RuntimeError("Technical testing error.")

    if storage is not None and isettings.dialect == "sqlite":
        update_root_field_in_default_storage(isettings)
    load_from_isettings(isettings)
    return None


def get_db_from_vault(instance_id, scheme, host, port, name, role=None):
    try:
        from lamin_vault.client._create_vault_client import (
            create_vault_authenticated_client,
        )
        from lamin_vault.client.postgres._get_db_from_vault import (
            get_db_from_vault as get_db_from_vault_base,
        )

        vault_client = create_vault_authenticated_client(
            access_token=settings.user.access_token, instance_id=instance_id
        )

        if role is None:
            role = f"{instance_id}-{settings.user.uuid}-db"

        return get_db_from_vault_base(
            vault_client=vault_client,
            scheme=scheme,
            host=host,
            port=port,
            name=name,
            role=role,
        )

    except Exception:
        logger.warning("Failed to connect to vault!")
    return None


def get_public_read_db_from_vault(
    instance_id,
    scheme,
    host,
    port,
    name,
):
    get_db_from_vault(
        scheme=scheme,
        host=host,
        port=port,
        name=name,
        role=f"{instance_id}-public-read-db",
    )


def get_owner_name_from_identifier(identifier: str):
    if "/" in identifier:
        if identifier.startswith("https://lamin.ai/"):
            identifier = identifier.replace("https://lamin.ai/", "")
        split = identifier.split("/")
        if len(split) > 2:
            raise ValueError(
                "The instance identifier needs to be 'owner/name', the instance name"
                " (owner is current user) or the URL: https://lamin.ai/owner/name."
            )
        owner, name = split
    else:
        owner = settings.user.handle
        name = identifier
    return owner, name


def update_isettings_with_storage(
    isettings: InstanceSettings, storage: Union[str, Path, UPath]
) -> None:
    ssettings = StorageSettings(storage)
    if ssettings.is_cloud:
        try:  # triggering ssettings.id makes a lookup in the storage table
            logger.success(f"loaded storage: {ssettings.id} / {ssettings.root_as_str}")
        except RuntimeError as e:
            logger.error(
                "storage not registered!\n"
                "load instance without the `storage` arg and register storage root: "
                f"`lamin set storage --storage {storage}`"
            )
            raise e
    else:
        # local storage
        # assumption is you want to merely update the storage location
        isettings._storage = ssettings  # need this here already
    # update isettings in place
    isettings._storage = ssettings


# this is different from register!
# register registers a new storage location
# update_root_field_in_default_storage updates the root
# field in the default storage locations
def update_root_field_in_default_storage(isettings: InstanceSettings):
    from lnschema_core.models import Storage

    storages = Storage.objects.all()
    if len(storages) != 1:
        raise RuntimeError(
            "You have several storage locations: Can't identify in which storage"
            " location the root column is to be updated!"
        )
    storage = storages[0]
    storage.root = isettings.storage.root_as_str
    storage.save()
    logger.save(f"updated storage root {storage.id} to {isettings.storage.root_as_str}")
