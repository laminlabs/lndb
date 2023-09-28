from typing import Optional
from ._settings import settings
from .dev.cloud_sqlite_locker import (
    unlock_cloud_sqlite_upon_exception,
)
from lamin_vault.client._init_instance_vault import init_instance_vault
from lamin_vault.client._create_vault_client import create_vault_admin_client
from .dev._hub_utils import LaminDsnModel
from pydantic import PostgresDsn


@unlock_cloud_sqlite_upon_exception(ignore_prev_locker=True)
def init_vault(
    *,
    db: Optional[PostgresDsn] = None,
) -> Optional[str]:
    """Initialize vault for current LaminDB instance.

    Args:
        db: {}
    """
    _init_vault(db, settings.instance.id)
    return None


def _init_vault(db, instance_id):
    db_dsn = LaminDsnModel(db=db)
    vault_admin_client = create_vault_admin_client(
        access_token=settings.user.access_token, instance_id=instance_id
    )
    init_instance_vault(
        vault_admin_client=vault_admin_client,
        instance_id=instance_id,
        admin_account_id=settings.user.uuid,
        db_host=db_dsn.db.host,
        db_port=db_dsn.db.port,
        db_name=db_dsn.db.database,
        vault_db_username=db_dsn.db.user,
        vault_db_password=db_dsn.db.password,
    )
