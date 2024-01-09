# flake8: noqa
import builtins
import os
from pathlib import Path
import shutil
import time
from lamin_utils import logger
from ._settings_store import current_instance_settings_file
from ._settings_instance import InstanceSettings

IS_RUN_FROM_IPYTHON = getattr(builtins, "__IPYTHON__", False)
IS_SETUP = False
CONN_MAX_AGE = 299
MISSING_MIGRATIONS_WARNING = """

Your database is not up to date with your installed Python library.

Your database has the latest migrations:
{deployed_latest_migrations}

Your Python library has the latest migrations:
{defined_latest_migrations}

Only if you are an admin and manage migrations manually, deploy them to the database: lamin migrate deploy

Otherwise, downgrade your Python library to match the database!
"""
AHEAD_MIGRATIONS_WARNING = """

Your database is ahead of your installed Python library.

Your database has the latest migrations:
{deployed_latest_migrations}

Your Python library has the latest migrations:
{defined_latest_migrations}

Please update your Python library to match the database!
"""


def get_migrations_to_sync():
    from .._migrate import migrate

    deployed_latest_migs = migrate.deployed_migrations(latest=True)
    defined_latest_migs = migrate.defined_migrations(latest=True)

    # in case a new app was added in the defined migrations,
    # reflect this with a dummy migration "0000_"
    for app in defined_latest_migs.keys():
        if app not in deployed_latest_migs:
            deployed_latest_migs[app] = "0000_"

    status = "synced"
    latest_migrs = ([], [])

    for app, deployed_latest_mig in deployed_latest_migs.items():
        deployed_latest_mig_nr = int(deployed_latest_mig.split("_")[0])
        defined_latest_mig = defined_latest_migs.get(app)

        if defined_latest_mig:
            defined_latest_mig_nr = int(defined_latest_mig.split("_")[0])

            if deployed_latest_mig_nr != defined_latest_mig_nr:
                deployed_mig_str = f"{app}.{deployed_latest_mig}"
                defined_mig_str = f"{app}.{defined_latest_mig}"
                status = (
                    "missing"
                    if deployed_latest_mig_nr < defined_latest_mig_nr
                    else "ahead"
                )
                latest_migrs[0].append(deployed_mig_str)
                latest_migrs[1].append(defined_mig_str)

    return status, latest_migrs


def close_if_health_check_failed(self) -> None:
    if self.close_at is not None:
        if time.monotonic() >= self.close_at:
            self.close()
        self.close_at = time.monotonic() + CONN_MAX_AGE


# this bundles set up and migration management
def setup_django(
    isettings: InstanceSettings,
    deploy_migrations: bool = False,
    create_migrations: bool = False,
    configure_only: bool = False,
    init: bool = False,
    view_schema: bool = False,
):
    if IS_RUN_FROM_IPYTHON:
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"

    import dj_database_url
    import django
    from django.db import connections
    from django.conf import settings
    from django.core.management import call_command

    # configuration
    default_db = dj_database_url.config(
        default=isettings.db,
        # see comment next to patching BaseDatabaseWrapper below
        conn_max_age=CONN_MAX_AGE,
        conn_health_checks=True,
    )
    if not settings.configured:
        DATABASES = {
            "default": default_db,
        }
        from .._init_instance import get_schema_module_name

        schema_names = ["core"] + list(isettings.schema)
        installed_apps = [get_schema_module_name(n) for n in schema_names]
        if view_schema:
            installed_apps = installed_apps[::-1]  # to fix how apps appear
            installed_apps += ["schema_graph", "django.contrib.staticfiles"]

        kwargs = dict(
            INSTALLED_APPS=installed_apps,
            DATABASES=DATABASES,
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            TIME_ZONE="UTC",
            USE_TZ=True,
        )
        if view_schema:
            kwargs.update(
                DEBUG=True,
                ROOT_URLCONF="lamindb_setup._schema",
                SECRET_KEY="dummy",
                TEMPLATES=[
                    {
                        "BACKEND": "django.template.backends.django.DjangoTemplates",
                        "APP_DIRS": True,
                    },
                ],
                STATIC_ROOT=f"{Path.home().as_posix()}/.lamin/",
                STATICFILES_FINDERS=[
                    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
                ],
                STATIC_URL="static/",
            )
        settings.configure(**kwargs)
        django.setup(set_prefix=False)

    # compare this with add_db_connection in lamindb._registry
    # is used to update the default connection string
    connections.settings["default"] = default_db

    # https://laminlabs.slack.com/archives/C04FPE8V01W/p1698239551460289
    from django.db.backends.base.base import BaseDatabaseWrapper

    BaseDatabaseWrapper.close_if_health_check_failed = close_if_health_check_failed

    if configure_only:
        return None

    # migrations management
    if create_migrations:
        call_command("makemigrations")
        return None

    # check that migrations have been deployed
    settings_file_existed = isettings._get_settings_file().exists()
    # make a temporary copy of the current settings file
    current_settings_file = current_instance_settings_file()
    current_settings_file_existed = current_settings_file.exists()
    if current_settings_file_existed:
        shutil.copy(current_settings_file, current_settings_file.with_name("_tmp.env"))
    isettings._persist()  # temporarily make settings available to migrations, should probably if fails

    if deploy_migrations:
        # deploy migrations
        call_command("migrate", verbosity=2)
        # only update if called from lamin migrate deploy
        # if called from load_schema(..., init=True)
        # no need to update the remote sqlite
        isettings._update_cloud_sqlite_file(unlock_cloud_sqlite=False)
    else:
        if init:
            call_command("migrate", verbosity=0)
        else:
            status, latest_migrs = get_migrations_to_sync()
            if status == "synced":
                pass
            else:
                warning_func = (
                    MISSING_MIGRATIONS_WARNING
                    if status == "missing"
                    else AHEAD_MIGRATIONS_WARNING
                )
                logger.warning(
                    warning_func.format(
                        deployed_latest_migrations=latest_migrs[0],
                        defined_latest_migrations=latest_migrs[1],
                    )
                )

    # clean up temporary settings files
    if not settings_file_existed:
        isettings._get_settings_file().unlink()
    current_instance_settings_file().unlink()
    if current_settings_file_existed:
        shutil.copy(current_settings_file.with_name("_tmp.env"), current_settings_file)
        current_settings_file.with_name("_tmp.env").unlink()

    global IS_SETUP
    IS_SETUP = True
