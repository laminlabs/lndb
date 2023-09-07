import os

import pytest
from laminhub_rest.routers.account import get_account_by_handle
from laminhub_rest.routers.collaborator import delete_collaborator
from laminhub_rest.routers.instance import add_collaborator

import lamindb_setup as ln_setup
from lamindb_setup.dev._hub_client import connect_hub_with_auth
from lamindb_setup.dev._hub_crud import sb_update_instance
from lamindb_setup.dev._settings_store import instance_settings_file


def test_load_remote_instance():
    ln_setup.login("testuser1")
    ln_setup.delete("lndb-setup-ci", force=True)
    ln_setup.init(storage="s3://lndb-setup-ci", _test=True)
    instance_settings_file("lndb-setup-ci", "testuser1").unlink()
    ln_setup.load("testuser1/lndb-setup-ci", _test=True)
    assert ln_setup.settings.instance._id is not None
    assert ln_setup.settings.instance.storage.is_cloud
    assert ln_setup.settings.instance.storage.root_as_str == "s3://lndb-setup-ci"
    assert (
        ln_setup.settings.instance._sqlite_file.as_posix()
        == "s3://lndb-setup-ci/lndb-setup-ci.lndb"
    )
    # ln_setup.delete("lndb-setup-ci")


def test_load_after_revoked_access():
    # can't currently test this on staging as I'm missing the accounts
    if os.getenv("LAMIN_ENV") == "prod":
        ln_setup.login(
            "static-testuser1@lamin.ai", password="static-testuser1-password"
        )
        admin_token = ln_setup.settings.user.access_token
        add_collaborator(
            "static-testuser2",
            "static-testuser1",
            "static-testinstance1",
            "write",
            f"Bearer {admin_token}",
        )
        ln_setup.login(
            "static-testuser2@lamin.ai", password="static-testuser2-password"
        )
        ln_setup.load(
            "https://lamin.ai/static-testuser1/static-testinstance1", _test=True
        )
        assert ln_setup.settings.instance.storage.root_as_str == "s3://lndb-setup-ci"
        account = get_account_by_handle("static-testuser2")
        delete_collaborator(
            "static-testuser1",
            "static-testinstance1",
            account["id"],
            f"Bearer {admin_token}",
        )
        with pytest.raises(RuntimeError) as error:
            ln_setup.load(
                "https://lamin.ai/static-testuser1/static-testinstance1", _test=True
            )
        assert (
            error.exconly()
            == "RuntimeError: Remote instance static-testuser1/static-testinstance1 not"
            " loadable from hub. The instance might have been deleted or you may have"
            " lost access."
        )


def test_load_after_private_public_switch():
    ln_setup.login("static-testuser1@lamin.ai", password="static-testuser1-password")
    ln_setup.load("https://lamin.ai/static-testuser1/static-testinstance1", _test=True)
    admin_hub = connect_hub_with_auth(access_token=ln_setup.settings.user.access_token)
    # make the instance public
    sb_update_instance(
        instance_id=ln_setup.settings.instance.id,
        instance_fields={"public": False},
        supabase_client=admin_hub,
    )
    # attempt to load instance with non-collaborator user
    ln_setup.login("testuser2")
    with pytest.raises(RuntimeError):
        ln_setup.load(
            "https://lamin.ai/static-testuser1/static-testinstance1", _test=True
        )
    # make the instance public
    sb_update_instance(
        instance_id=ln_setup.settings.instance.id,
        instance_fields={"public": True},
        supabase_client=admin_hub,
    )
    # load instance with non-collaborator user, should work now
    ln_setup.load("https://lamin.ai/static-testuser1/static-testinstance1", _test=True)
    # make the instance private again
    sb_update_instance(
        instance_id=ln_setup.settings.instance.id,
        instance_fields={"public": False},
        supabase_client=admin_hub,
    )
