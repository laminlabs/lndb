import os

import pytest
from laminhub_rest.core.collaborator._add_collaborator import add_collaborator
from laminhub_rest.core.collaborator._delete_collaborator import delete_collaborator

import lamindb_setup as ln_setup
from lamindb_setup.dev._hub_client import connect_hub_with_auth
from lamindb_setup.dev._hub_crud import sb_update_instance


def test_load_remote_instance():
    ln_setup.login("testuser1")
    ln_setup.delete("load_remote_instance", force=True)
    ln_setup.init(storage="s3://lamindb-ci/load_remote_instance", _test=True)
    ln_setup.load("testuser1/lamindb-ci", _test=True)
    assert ln_setup.settings.instance.id is not None
    assert ln_setup.settings.instance.storage.is_cloud
    assert (
        ln_setup.settings.instance.storage.root_as_str
        == "s3://lamindb-ci/load_remote_instance"
    )
    assert (
        ln_setup.settings.instance._sqlite_file.as_posix()
        == f"s3://lamindb-ci/load_remote_instance/{ln_setup.settings.instance.id.hex}.lndb"  # noqa
    )


def test_load_after_revoked_access():
    # can't currently test this on staging as I'm missing the accounts
    if os.getenv("LAMIN_ENV") == "prod":
        ln_setup.login("testuser1@lamin.ai")
        admin_hub = connect_hub_with_auth()
        add_collaborator(
            "testuser2",
            "laminlabs",
            "static-test-instance-private-sqlite",
            "write",
            admin_hub,
        )
        ln_setup.login("testuser2@lamin.ai")
        ln_setup.load(
            "https://lamin.ai/laminlabs/static-test-instance-private-sqlite", _test=True
        )
        assert ln_setup.settings.instance.storage.root_as_str == "s3://lamindb-ci"
        delete_collaborator(
            "laminlabs",
            "static-test-instance-private-sqlite",
            ln_setup.settings.user.uuid,
            admin_hub,
        )
        # make the instance private
        with pytest.raises(RuntimeError) as error:
            ln_setup.load(
                "https://lamin.ai/laminlabs/static-test-instance-private-sqlite",
                _test=True,
            )
        assert (
            error.exconly() == "RuntimeError: Instance"
            " laminlabs/static-test-instance-private-sqlite not"
            " loadable from hub with response: 'instance-not-reachable'.\nCheck"
            " whether instance exists and you have access:"
            " https://lamin.ai/laminlabs/static-test-instance-private-sqlite?tab=collaborators"  # noqa
        )


def test_load_after_private_public_switch():
    # can't currently test this on staging as I'm missing the accounts
    if os.getenv("LAMIN_ENV") == "prod":
        # this assumes that testuser1 is an admin of static-test-instance-private-sqlite
        ln_setup.login("testuser1@lamin.ai")
        ln_setup.load(
            "https://lamin.ai/laminlabs/static-test-instance-private-sqlite", _test=True
        )
        admin_hub = connect_hub_with_auth()
        # make the instance private
        sb_update_instance(
            instance_id=ln_setup.settings.instance.id,
            instance_fields={"public": False},
            client=admin_hub,
        )
        # attempt to load instance with non-collaborator user
        ln_setup.login("testuser2")
        with pytest.raises(RuntimeError):
            ln_setup.load(
                "https://lamin.ai/laminlabs/static-test-instance-private-sqlite",
                _test=True,
            )
        # make the instance public
        sb_update_instance(
            instance_id=ln_setup.settings.instance.id,
            instance_fields={"public": True},
            client=admin_hub,
        )
        # load instance with non-collaborator user, should work now
        ln_setup.load(
            "https://lamin.ai/laminlabs/static-test-instance-private-sqlite", _test=True
        )
        # make the instance private again
        sb_update_instance(
            instance_id=ln_setup.settings.instance.id,
            instance_fields={"public": False},
            client=admin_hub,
        )


def test_load_with_db_parameter():
    if os.getenv("LAMIN_ENV") == "prod":
        ln_setup.login("static-testuser1@lamin.ai", key="static-testuser1-password")
        # test load from hub
        ln_setup.load("laminlabs/lamindata", _test=True)
        assert "public-read" in ln_setup.settings.instance.db
        # test load from provided db argument
        db = "postgresql://testuser:testpwd@database2.cmyfs24wugc3.us-east-1.rds.amazonaws.com:5432/db1"  # noqa
        ln_setup.load("laminlabs/lamindata", db=db, _test=True)
        assert "testuser" in ln_setup.settings.instance.db
        # test load from cache (no db arg)
        ln_setup.load("laminlabs/lamindata", _test=True)
        assert "testuser" in ln_setup.settings.instance.db
        # test corrupted input
        db_corrupted = "postgresql://testuser:testpwd@wrongserver:5432/db1"
        with pytest.raises(ValueError) as error:
            ln_setup.load("laminlabs/lamindata", db=db_corrupted, _test=True)
        assert error.exconly().startswith(
            "ValueError: The local differs from the hub database information"
        )
