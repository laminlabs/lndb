from __future__ import annotations

import os
import shutil
from .upath import LocalPathClasses, convert_pathlike
from pathlib import Path
from typing import Literal, Optional, Set, Tuple
from uuid import UUID
from ._hub_client import call_with_fallback
from ._hub_crud import select_account_handle_name_by_lnid
from lamin_utils import logger
from .cloud_sqlite_locker import (
    InstanceLockedException,
    EXPIRATION_TIME,
)
from ._hub_utils import LaminDsnModel, LaminDsn
from ._settings_save import save_instance_settings
from ._settings_storage import StorageSettings
from ._settings_store import current_instance_settings_file, instance_settings_file
from .upath import UPath

LOCAL_STORAGE_ROOT_WARNING = (
    "No local storage root found, set via `ln.setup.settings.instance.local_storage ="
    " local_root`"
)


def sanitize_git_repo_url(repo_url: str) -> str:
    assert repo_url.startswith("https://")
    return repo_url.replace(".git", "")


class InstanceSettings:
    """Instance settings."""

    def __init__(
        self,
        id: UUID,  # instance id/uuid
        owner: str,  # owner handle
        name: str,  # instance name
        storage: StorageSettings,  # storage location
        local_storage: bool = False,  # default to local storage
        uid: Optional[str] = None,  # instance uid/lnid
        db: Optional[str] = None,  # DB URI
        schema: Optional[str] = None,  # comma-separated string of schema names
        git_repo: Optional[str] = None,  # a git repo URL
    ):
        from ._hub_utils import validate_db_arg

        self._id: UUID = id
        self._owner: str = owner
        self._name: str = name
        self._uid: Optional[str] = uid
        self._storage: StorageSettings = storage
        validate_db_arg(db)
        self._db: Optional[str] = db
        self._schema_str: Optional[str] = schema
        self._git_repo = None if git_repo is None else sanitize_git_repo_url(git_repo)
        # local storage
        self._local_storage_on = local_storage
        self._local_storage = None
        if self._local_storage_on:
            self._search_local_root()

    def __repr__(self):
        """Rich string representation."""
        representation = f"Current instance: {self.slug}"
        attrs = ["owner", "name", "storage", "db", "schema", "git_repo"]
        for attr in attrs:
            value = getattr(self, attr)
            if attr == "storage":
                representation += f"\n- storage root: {value.root_as_str}"
                representation += f"\n- storage region: {value.region}"
            elif attr == "db":
                if self.dialect != "sqlite":
                    model = LaminDsnModel(db=value)
                    db_print = LaminDsn.build(
                        scheme=model.db.scheme,
                        user=model.db.user,
                        password="***",
                        host="***",
                        port=model.db.port,
                        database=model.db.database,
                    )
                else:
                    db_print = value
                representation += f"\n- {attr}: {db_print}"
            else:
                representation += f"\n- {attr}: {value}"
        return representation

    @property
    def owner(self) -> str:
        """Instance owner. A user or organization account handle."""
        return self._owner

    @property
    def name(self) -> str:
        """Instance name."""
        return self._name

    def _search_local_root(self):
        from lnschema_core.models import Storage

        records = Storage.objects.filter(type="local").all()
        for record in records:
            if Path(record.root).exists():
                self._local_storage = StorageSettings(record.root)
                logger.important(f"defaulting to local storage: {record}")
                break
        if self._local_storage is None:
            logger.warning(LOCAL_STORAGE_ROOT_WARNING)

    @property
    def local_storage(self) -> StorageSettings:
        """Default local storage.

        Warning: Only enable if you're sure you want to use the more complicated
        storage mode across local & cloud locations.

        As an admin, enable via: `ln.setup.settings.instance.local_storage =
        local_root`.

        If enabled, you'll save artifacts to a default local storage
        location at :attr:`~lamindb.setup.settings.InstanceSettings.local_storage`.

        Upon passing `upload=True` in `artifact.save(upload=True)`, you upload the
        artifact to the default cloud storage location:
        :attr:`~lamindb.setup.core.InstanceSettings.storage`.
        """
        if not self._local_storage_on:
            raise ValueError("Local storage is not enabled for this instance.")
        if self._local_storage is None:
            self._search_local_root()
        if self._local_storage is None:
            raise ValueError(LOCAL_STORAGE_ROOT_WARNING)
        return self._local_storage

    @local_storage.setter
    def local_storage(self, local_root: Path):
        from ._hub_core import update_instance_record
        from .._init_instance import register_storage

        self._search_local_root()
        if self._local_storage is not None:
            raise ValueError(
                "You already configured a local storage root for this instance in this"
                f" environment: {self.local_storage.root}"
            )
        local_root = convert_pathlike(local_root)
        assert isinstance(local_root, LocalPathClasses)
        self._local_storage = StorageSettings(local_root)  # type: ignore
        register_storage(self._local_storage)  # type: ignore
        self._local_storage_on = True
        update_instance_record(self.id, {"local_storage": self._local_storage_on})

    @property
    def identifier(self) -> str:
        """Unique semantic identifier."""
        logger.warning(
            "InstanceSettings.identifier is deprecated and will be removed, use"
            " InstanceSettings.slug instead"
        )
        return self.slug

    @property
    def slug(self) -> str:
        """Unique semantic identifier of form `"{account_handle}/{instance_name}"`."""
        return f"{self.owner}/{self.name}"

    @property
    def git_repo(self) -> Optional[str]:
        """Sync transforms with scripts in git repository.

        Provide the full git repo URL.
        """
        return self._git_repo

    @property
    def id(self) -> UUID:
        """The internal instance id."""
        return self._id

    @property
    def uid(self) -> Optional[str]:
        """The user-facing instance id."""
        return self._uid

    @property
    def schema(self) -> Set[str]:
        """Schema modules in addition to core schema."""
        if self._schema_str is None:
            return {}  # type: ignore
        else:
            return {schema for schema in self._schema_str.split(",") if schema != ""}

    @property
    def _sqlite_file(self) -> UPath:
        """SQLite file."""
        return self.storage.key_to_filepath(f"{self.id.hex}.lndb")

    @property
    def _sqlite_file_local(self) -> Path:
        """Local SQLite file."""
        return self.storage.cloud_to_local_no_update(self._sqlite_file)

    def _update_cloud_sqlite_file(self, unlock_cloud_sqlite: bool = True) -> None:
        """Upload the local sqlite file to the cloud file."""
        if self._is_cloud_sqlite:
            sqlite_file = self._sqlite_file
            logger.warning(
                f"updating & unlocking cloud SQLite '{sqlite_file}' of instance"
                f" '{self.slug}'"
            )
            cache_file = self.storage.cloud_to_local_no_update(sqlite_file)
            sqlite_file.upload_from(cache_file, print_progress=True)  # type: ignore
            cloud_mtime = sqlite_file.modified.timestamp()  # type: ignore
            # this seems to work even if there is an open connection
            # to the cache file
            os.utime(cache_file, times=(cloud_mtime, cloud_mtime))
            if unlock_cloud_sqlite:
                self._cloud_sqlite_locker.unlock()

    def _update_local_sqlite_file(self, lock_cloud_sqlite: bool = True) -> None:
        """Download the cloud sqlite file if it is newer than local."""
        if self._is_cloud_sqlite:
            logger.warning(
                "updating local SQLite & locking cloud SQLite (sync back & unlock:"
                " lamin close)"
            )
            if lock_cloud_sqlite:
                self._cloud_sqlite_locker.lock()
                self._check_sqlite_lock()
            sqlite_file = self._sqlite_file
            cache_file = self.storage.cloud_to_local_no_update(sqlite_file)
            sqlite_file.synchronize(cache_file, print_progress=True)  # type: ignore

    def _check_sqlite_lock(self):
        if not self._cloud_sqlite_locker.has_lock:
            locked_by = self._cloud_sqlite_locker._locked_by
            lock_msg = "Cannot load the instance, it is locked by "
            user_info = call_with_fallback(
                select_account_handle_name_by_lnid,
                lnid=locked_by,
            )
            if user_info is None:
                lock_msg += f"uid: '{locked_by}'."
            else:
                lock_msg += (
                    f"'{user_info['handle']}' (uid: '{locked_by}', name:"
                    f" '{user_info['name']}')."
                )
            lock_msg += (
                " The instance will be automatically unlocked after"
                f" {int(EXPIRATION_TIME/3600/24)}d of no activity."
            )
            raise InstanceLockedException(lock_msg)

    @property
    def db(self) -> str:
        """Database connection string (URI)."""
        if self._db is None:
            # here, we want the updated sqlite file
            # hence, we don't use self._sqlite_file_local()
            # error_no_origin=False because on instance init
            # the sqlite file is not yet in the cloud
            sqlite_filepath = self.storage.cloud_to_local(
                self._sqlite_file, error_no_origin=False
            )
            return f"sqlite:///{sqlite_filepath}"
        else:
            return self._db

    @property
    def dialect(self) -> Literal["sqlite", "postgresql"]:
        """SQL dialect."""
        if self._db is None or self._db.startswith("sqlite://"):
            return "sqlite"
        else:
            assert self._db.startswith("postgresql"), f"Unexpected DB value: {self._db}"
            return "postgresql"

    @property
    def session(self):
        raise NotImplementedError

    @property
    def _is_cloud_sqlite(self) -> bool:
        # can we make this a private property, Sergei?
        # as it's not relevant to the user
        """Is this a cloud instance with sqlite db."""
        return self.dialect == "sqlite" and self.storage.type_is_cloud

    @property
    def _cloud_sqlite_locker(self):
        # avoid circular import
        from .cloud_sqlite_locker import empty_locker, get_locker

        if self._is_cloud_sqlite:
            try:
                return get_locker(self)
            except PermissionError:
                logger.warning("read-only access - did not access locker")
                return empty_locker
        else:
            return empty_locker

    @property
    def storage(self) -> StorageSettings:
        """Low-level access to storage location."""
        return self._storage

    @property
    def is_remote(self) -> bool:
        """Boolean indicating if an instance has no local component."""
        if not self.storage.type_is_cloud:
            return False

        def is_local_uri(uri: str):
            if "@localhost:" in uri:
                return True
            if "@0.0.0.0:" in uri:
                return True
            if "@127.0.0.1" in uri:
                return True
            return False

        if self.dialect == "postgresql":
            if is_local_uri(self.db):
                return False
        # returns True for cloud SQLite
        # and remote postgres
        return True

    def _get_settings_file(self) -> Path:
        return instance_settings_file(self.name, self.owner)

    def _persist(self) -> None:
        assert self.name is not None

        filepath = self._get_settings_file()
        # persist under filepath for later reference
        save_instance_settings(self, filepath)
        # persist under current file for auto load
        shutil.copy2(filepath, current_instance_settings_file())
        # persist under settings class for same session reference
        # need to import here to avoid circular import
        from ._settings import settings

        settings._instance_settings = self

    def _init_db(self):
        from .django import setup_django

        setup_django(self, init=True)

    def _load_db(
        self, do_not_lock_for_laminapp_admin: bool = False
    ) -> Tuple[bool, str]:
        # Is the database available and initialized as LaminDB?
        # returns a tuple of status code and message
        if self.dialect == "sqlite" and not self._sqlite_file.exists():
            legacy_file = self.storage.key_to_filepath(f"{self.name}.lndb")
            if legacy_file.exists():
                raise RuntimeError(
                    "The SQLite file has been renamed!\nPlease rename your SQLite file"
                    f" {legacy_file} to {self._sqlite_file}"
                )
            return False, f"SQLite file {self._sqlite_file} does not exist"
        from lamindb_setup import settings  # to check user
        from .django import setup_django

        # lock in all cases except if do_not_lock_for_laminapp_admin is True and
        # user is `laminapp-admin`
        # value doesn't matter if not a cloud sqlite instance
        lock_cloud_sqlite = self._is_cloud_sqlite and (
            not do_not_lock_for_laminapp_admin
            or settings.user.handle != "laminapp-admin"
        )
        # we need the local sqlite to setup django
        self._update_local_sqlite_file(lock_cloud_sqlite=lock_cloud_sqlite)
        # setting up django also performs a check for migrations & prints them
        # as warnings
        # this should fail, e.g., if the db is not reachable
        setup_django(self)
        return True, ""
