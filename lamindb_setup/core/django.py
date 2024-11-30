from __future__ import annotations

# flake8: noqa
import builtins
import os
from pathlib import Path
import time
from lamin_utils import logger
from ._settings_store import current_instance_settings_file
from ._settings_instance import InstanceSettings
import sys

IS_RUN_FROM_IPYTHON = getattr(builtins, "__IPYTHON__", False)
IS_SETUP = False
IS_MIGRATING = False
CONN_MAX_AGE = 299


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
    from django.conf import settings
    from django.core.management import call_command

    # configuration
    if not settings.configured:
        default_db = dj_database_url.config(
            env="LAMINDB_DJANGO_DATABASE_URL",
            default=isettings.db,
            # see comment next to patching BaseDatabaseWrapper below
            conn_max_age=CONN_MAX_AGE,
            conn_health_checks=True,
        )
        DATABASES = {
            "default": default_db,
        }
        from .._init_instance import get_schema_module_name

        schema_names = ["core"] + list(isettings.schema)
        raise_import_error = True if init else False
        installed_apps = [
            package_name
            for n in schema_names
            if (
                package_name := get_schema_module_name(
                    n, raise_import_error=raise_import_error
                )
            )
            is not None
        ]
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
        if True:
            kwargs.update(
                {
                    "DEBUG": True,
                    "LOGGING": {
                        "version": 1,
                        "handlers": {
                            "console": {
                                "level": "DEBUG",
                                "class": "logging.StreamHandler",
                            }
                        },
                        "loggers": {
                            "django.db.backends": {
                                "level": "DEBUG",
                                "handlers": ["console"],
                            }
                        },
                    },
                }
            )
        settings.configure(**kwargs)
        django.setup(set_prefix=False)
        # https://laminlabs.slack.com/archives/C04FPE8V01W/p1698239551460289
        from django.db.backends.base.base import BaseDatabaseWrapper

        BaseDatabaseWrapper.close_if_health_check_failed = close_if_health_check_failed

    if configure_only:
        return None

    # migrations management
    if create_migrations:
        call_command("makemigrations")
        return None

    if deploy_migrations:
        call_command("migrate", verbosity=2)
        isettings._update_cloud_sqlite_file(unlock_cloud_sqlite=False)
    elif init:
        global IS_MIGRATING
        IS_MIGRATING = True
        call_command("migrate", verbosity=0)
        IS_MIGRATING = False

    global IS_SETUP
    IS_SETUP = True

    if isettings.keep_artifacts_local:
        isettings._search_local_root()
