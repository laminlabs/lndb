from lamin_utils import logger

from ._settings import settings
from .dev._settings_store import current_instance_settings_file
from .dev._setup_bionty_sources import delete_bionty_sources_yaml
from .dev.cloud_sqlite_locker import clear_locker


def close(mute: bool = False) -> None:
    """Close existing instance.

    Returns `None` if succeeds, otherwise an exception is raised.
    """
    if current_instance_settings_file().exists():
        instance = settings.instance.identifier
        try:
            settings.instance._update_cloud_sqlite_file()
        except Exception as e:
            if isinstance(e, FileNotFoundError):
                logger.warning("did not find local cache file")
            elif isinstance(e, PermissionError):
                logger.warning("did not upload cache file - not enough permissions")
            else:
                raise e
        current_instance_settings_file().unlink()
        delete_bionty_sources_yaml()
        clear_locker()
        settings._instance_settings = None
        if not mute:
            logger.success(f"closed instance: {instance}")
    else:
        if not mute:
            logger.info("no instance loaded")
