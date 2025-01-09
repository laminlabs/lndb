from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from lamin_utils import logger
from supabase.client import Client  # noqa


def select_instance_by_owner_name(
    owner: str,
    name: str,
    client: Client,
) -> dict | None:
    # this won't find an instance without the default storage
    data = (
        client.table("instance")
        .select(
            "*, account!inner!instance_account_id_28936e8f_fk_account_id(*),"
            " storage!inner!storage_instance_id_359fca71_fk_instance_id(*)"
        )
        .eq("name", name)
        .eq("account.handle", owner)
        .eq("storage.is_default", True)
        .execute()
        .data
    )
    if len(data) == 0:
        return None
    result = data[0]
    # this is a list
    # assume only one default storage
    result["storage"] = result["storage"][0]
    return result


# --------------- ACCOUNT ----------------------
def select_account_by_handle(
    handle: str,
    client: Client,
):
    data = client.table("account").select("*").eq("handle", handle).execute().data
    if len(data) == 0:
        return None
    return data[0]


def select_account_handle_name_by_lnid(lnid: str, client: Client):
    data = (
        client.table("account").select("handle, name").eq("lnid", lnid).execute().data
    )
    if not data:
        return None
    return data[0]


# --------------- INSTANCE ----------------------
def select_instance_by_name(
    account_id: str,
    name: str,
    client: Client,
):
    data = (
        client.table("instance")
        .select("*")
        .eq("account_id", account_id)
        .eq("name", name)
        .execute()
        .data
    )
    if len(data) == 0:
        return None
    return data[0]


def select_instance_by_id(
    instance_id: str,
    client: Client,
):
    response = client.table("instance").select("*").eq("id", instance_id).execute()
    if len(response.data) == 0:
        return None
    return response.data[0]


def select_instance_by_id_with_storage(
    instance_id: str,
    client: Client,
):
    # this won't find an instance without the default storage
    data = (
        client.table("instance")
        .select("*, storage!inner!storage_instance_id_359fca71_fk_instance_id(*)")
        .eq("id", instance_id)
        .eq("storage.is_default", True)
        .execute()
        .data
    )
    if len(data) == 0:
        return None
    result = data[0]
    # this is a list
    # assume only one default storage
    result["storage"] = result["storage"][0]
    return result


def update_instance(instance_id: str, instance_fields: dict, client: Client):
    response = (
        client.table("instance").update(instance_fields).eq("id", instance_id).execute()
    )
    if len(response.data) == 0:
        raise PermissionError(
            f"Update of instance with {instance_id} was not successful. Probably, you"
            " don't have sufficient permissions."
        )
    return response.data[0]


# --------------- COLLABORATOR ----------------------


def select_collaborator(
    instance_id: str,
    account_id: str,
    client: Client,
):
    data = (
        client.table("account_instance")
        .select("*")
        .eq("instance_id", instance_id)
        .eq("account_id", account_id)
        .execute()
        .data
    )
    if len(data) == 0:
        return None
    return data[0]


# --------------- STORAGE ----------------------


def select_default_storage_by_instance_id(
    instance_id: str, client: Client
) -> dict | None:
    data = (
        client.table("storage")
        .select("*")
        .eq("instance_id", instance_id)
        .eq("is_default", True)
        .execute()
        .data
    )
    if len(data) == 0:
        return None
    return data[0]


# --------------- DBUser ----------------------


def insert_db_user(
    *,
    name: str,
    db_user_name: str,
    db_user_password: str,
    instance_id: UUID,
    client: Client,
) -> None:
    fields = (
        {
            "id": uuid4().hex,
            "instance_id": instance_id.hex,
            "name": name,
            "db_user_name": db_user_name,
            "db_user_password": db_user_password,
        },
    )
    data = client.table("db_user").insert(fields).execute().data
    return data[0]


def select_db_user_by_instance(instance_id: str, client: Client):
    """Get db_user for which client has permission."""
    data = (
        client.table("db_user")
        .select("*")
        .eq("instance_id", instance_id)
        .execute()
        .data
    )
    if len(data) == 0:
        return None
    elif len(data) > 1:
        for item in data:
            if item["name"] == "write":
                return item
        logger.warning("found multiple db credentials, using the first one")
    return data[0]


def select_write_db_user_by_instance(instance_id: str, client: Client):
    """Get db_user for which client has permission."""
    data = (
        client.table("db_user")
        .select("*")
        .eq("instance_id", instance_id)
        .eq("name", "write")
        .execute()
        .data
    )
    if len(data) == 0:
        return None
    return data[0]


def _delete_instance_record(instance_id: UUID, client: Client) -> None:
    if not isinstance(instance_id, UUID):
        instance_id = UUID(instance_id)
    response = client.table("instance").delete().eq("id", instance_id.hex).execute()
    if response.data:
        logger.important(f"deleted instance record on hub {instance_id.hex}")
    else:
        raise PermissionError(
            f"Deleting of instance with {instance_id.hex} was not successful. Probably, you"
            " don't have sufficient permissions."
        )
