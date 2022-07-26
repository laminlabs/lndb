from pathlib import Path
from typing import Union

from cloudpathlib import CloudPath
from lamin_logger import logger
from sqlmodel import SQLModel

from ._db import insert_if_not_exists
from ._docs import doc_args
from ._hub import sign_in_hub, sign_up_hub
from ._settings import description
from ._settings_load import (
    load_or_create_instance_settings,
    load_or_create_user_settings,
    setup_storage_dir,
    switch_user,
)
from ._settings_save import save_instance_settings, save_user_settings


def setup_instance_db():
    """Setup database.

    Contains:
    - Database creation.
    - Sign-up and/or log-in.
    """
    instance_settings = load_or_create_instance_settings()
    user_settings = load_or_create_user_settings()
    if instance_settings.storage_dir is None:
        logger.warning("Instance is not configured. Call `lndb init` or `lndb load`.")
        return None
    instance_name = instance_settings.instance_name
    sqlite_file = instance_settings._sqlite_file
    schema_modules = instance_settings.schema_modules

    if sqlite_file.exists():
        logger.info(f"Using instance: {sqlite_file}")
    else:
        if schema_modules is not None and "biology" in schema_modules:
            import lndb_schema_biology  # noqa

            logger.info(f"Loading schema module {schema_modules}.")
        SQLModel.metadata.create_all(instance_settings.db_engine())
        instance_settings._update_cloud_sqlite_file()
        logger.info(f"Created instance {instance_name}: {sqlite_file}")

    insert_if_not_exists.user(user_settings.user_email, user_settings.user_id)


def sign_up_first_time(email):
    user_settings = load_or_create_user_settings()
    user_settings.user_email = email
    save_user_settings(user_settings)
    secret = sign_up_hub(email)
    if secret is None:  # user already exists
        logger.error("User already exists! Please login instead: `lndb login`.")
        return "user-exists"
    user_settings.user_secret = secret
    save_user_settings(user_settings)
    return None  # user needs to confirm email now


def log_in_user(
    *,
    email: Union[str, None] = None,
    secret: Union[str, None] = None,
):
    if email:
        switch_user(email)

    user_settings = load_or_create_user_settings()

    if secret:
        user_settings.user_secret = secret

    if user_settings.user_email is None:
        raise RuntimeError(
            "No stored user email, please call: lndb login --email <your-email>"
        )

    if user_settings.user_secret is None:
        raise RuntimeError(
            "No stored user secret, please call: lndb login --email <your-email>"
            " --email <your-secret>"
        )

    user_id = sign_in_hub(user_settings.user_email, user_settings.user_secret)
    user_settings.user_id = user_id
    save_user_settings(user_settings)


@doc_args(
    description.storage_dir,
    description._dbconfig,
    description.schema_modules,
)
def setup_instance(
    *,
    storage: Union[str, Path, CloudPath],
    dbconfig: str = "sqlite",
    schema: Union[str, None] = None,
) -> None:
    """Setup LaminDB.

    Args:
        storage: {}
        dbconfig: {}
        schema: {}
    """
    # settings.user_email & settings.user_secret are set
    instance_settings = load_or_create_instance_settings()
    user_settings = load_or_create_user_settings()
    if user_settings.user_id is None:
        if (
            user_settings.user_email is not None
            and user_settings.user_secret is not None  # noqa
        ):
            # complete user setup, this *only* happens after *sign_up_first_time*
            logger.info("Completing user sign up. Only happens once!")
            log_in_user(
                email=user_settings.user_email, secret=user_settings.user_secret
            )
            user_settings = (
                load_or_create_user_settings()
            )  # need to reload, here, to get user_id
        else:
            raise RuntimeError("Login user: lndb login --email")
    save_user_settings(user_settings)

    # setup storage
    if storage is None:
        if instance_settings.storage_dir is None:
            raise RuntimeError(
                "No storage in .env, please call: lndb init --storage <location>"
            )
        else:
            storage = instance_settings.storage_dir
    else:
        instance_settings.storage_dir = setup_storage_dir(storage)

    # setup _config
    instance_settings._dbconfig = dbconfig
    if dbconfig != "sqlite":
        raise NotImplementedError()

    # setup schema
    if schema is not None:
        if schema == "biology":
            instance_settings.schema_modules = schema
        else:
            raise RuntimeError("Unknown schema module. Only know 'biology'.")
    save_instance_settings(instance_settings)

    setup_instance_db()
    return None
