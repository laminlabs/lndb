import os
from pathlib import Path
from subprocess import call

import sqlmodel as sqm
from lamin_logger import logger

from ._db import insert
from ._settings_instance import InstanceSettings
from ._settings_user import UserSettings


def check_migrate(
    *,
    usettings: UserSettings,
    isettings: InstanceSettings,
    schema: str = "lnschema_core",
):
    if schema == "lnschema_core":
        import lnschema_core
    else:
        raise NotImplementedError

    with sqm.Session(isettings.db_engine()) as session:
        version_table = session.exec(sqm.select(lnschema_core.version_yvzi)).all()

    versions = [row.v for row in version_table]

    current_version = lnschema_core.__version__

    if current_version not in versions:
        logger.warning(
            "Run the command in the shell to respond to the following dialogue."
        )

        response = input("Do you want to migrate (y/n)?")

        if os.environ.get("NBPRJ_TEST_SESSION") is not None:
            response = "y"

        if response != "y":
            logger.warning(
                "Your database does not seem up to date with the latest schema."
                "Either install a previous API version or migrate the database."
            )
            return None

        migrate(
            version=current_version,
            usettings=usettings,
            isettings=isettings,
            schema="lnschema_core",
        )


def migrate(
    *,
    version: str,
    usettings: UserSettings,
    isettings: InstanceSettings,
    schema: str = "lnschema_core",
):
    """Migrate database to latest version."""
    if schema == "lnschema_core":
        import lnschema_core
    else:
        raise NotImplementedError

    schema_root = Path(lnschema_core.__file__).parent
    alembic_ini = schema_root / "alembic.ini"

    # modify alembic.ini
    with open(alembic_ini) as f:
        content = f.read()
    content = content.replace(
        "script_location = lnschema_core/migrations",
        "script_location = migrations",
    ).replace(
        "sqlalchemy.url = sqlite:///tests/testdb.lndb",
        f"sqlalchemy.url = sqlite:///{isettings._sqlite_file_local}",
    )
    with open(alembic_ini, "w") as f:
        f.write(content)

    retcode = call("alembic --name yvzi upgrade head", cwd=f"{schema_root}", shell=True)

    if retcode == 0:
        logger.success(f"Successfully migrated {schema} to v{version}.")
        isettings._update_cloud_sqlite_file()

        insert.version_yvzi(
            lnschema_core.__version__, lnschema_core._migration, usettings.id
        )
    else:
        logger.error("Automatic migration failed.")

    # clean up
    with open(alembic_ini) as f:
        content = f.read()
    content = content.replace(
        "script_location = migrations",
        "script_location = lnschema_core/migrations",
    ).replace(
        f"sqlalchemy.url = sqlite:///{isettings._sqlite_file_local}",
        "sqlalchemy.url = sqlite:///tests/testdb.lndb",
    )
    with open(alembic_ini, "w") as f:
        f.write(content)
