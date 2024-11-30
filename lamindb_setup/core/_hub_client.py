from __future__ import annotations

import json
import os
from urllib.request import urlretrieve

from gotrue.errors import AuthUnknownError
from lamin_utils import logger
from pydantic_settings import BaseSettings
from supabase import Client, create_client  # type: ignore
from supabase.lib.client_options import ClientOptions

from ._settings_save import save_user_settings


class Connector(BaseSettings):
    url: str
    key: str


def load_fallback_connector() -> Connector:
    url = "https://lamin-site-assets.s3.amazonaws.com/connector.env"
    connector_file, _ = urlretrieve(url)
    connector = Connector(_env_file=connector_file)
    return connector


PROD_URL = "https://hub.lamin.ai"
PROD_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImxhZXNhdW1tZHlkbGxwcGdmY2h1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE2NTY4NDA1NTEsImV4cCI6MTk3MjQxNjU1MX0.WUeCRiun0ExUxKIv5-CtjF6878H8u26t0JmCWx3_2-c"


class Environment:
    def __init__(self, fallback: bool = False):
        lamin_env = os.getenv("LAMIN_ENV")
        if lamin_env is None:
            lamin_env = "prod"
        # set public key
        if lamin_env == "prod":
            if not fallback:
                url = PROD_URL
                key = PROD_ANON_KEY
            else:
                connector = load_fallback_connector()
                url = connector.url
                key = connector.key
        elif lamin_env == "staging":
            url = "https://amvrvdwndlqdzgedrqdv.supabase.co"
            key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFtdnJ2ZHduZGxxZHpnZWRycWR2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE2NzcxNTcxMzMsImV4cCI6MTk5MjczMzEzM30.Gelt3dQEi8tT4j-JA36RbaZuUvxRnczvRr3iyRtzjY0"
        elif lamin_env == "staging-test":
            url = "https://iugyyajllqftbpidapak.supabase.co"
            key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Iml1Z3l5YWpsbHFmdGJwaWRhcGFrIiwicm9sZSI6ImFub24iLCJpYXQiOjE2OTQyMjYyODMsImV4cCI6MjAwOTgwMjI4M30.s7B0gMogFhUatMSwlfuPJ95kWhdCZMn1ROhZ3t6Og90"
        elif lamin_env == "prod-test":
            url = "https://xtdacpwiqwpbxsatoyrv.supabase.co"
            key = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inh0ZGFjcHdpcXdwYnhzYXRveXJ2Iiwicm9sZSI6ImFub24iLCJpYXQiOjE2OTQyMjYxNDIsImV4cCI6MjAwOTgwMjE0Mn0.Dbi27qujTt8Ei9gfp9KnEWTYptE5KUbZzEK6boL46k4"
        else:
            url = os.environ["SUPABASE_API_URL"]
            key = os.environ["SUPABASE_ANON_KEY"]
        self.lamin_env: str = lamin_env
        self.supabase_api_url: str = url
        self.supabase_anon_key: str = key


# runs ~0.5s
def connect_hub(
    fallback_env: bool = False, client_options: ClientOptions | None = None
) -> Client:
    env = Environment(fallback=fallback_env)
    if client_options is None:
        client_options = ClientOptions(auto_refresh_token=False)
    return create_client(env.supabase_api_url, env.supabase_anon_key, client_options)


def connect_hub_with_auth(
    fallback_env: bool = False,
    renew_token: bool = False,
    access_token: str | None = None,
) -> Client:
    hub = connect_hub(fallback_env=fallback_env)
    if access_token is None:
        from lamindb_setup import settings

        if renew_token:
            settings.user.access_token = get_access_token(
                settings.user.email, settings.user.password, settings.user.api_key
            )
        access_token = settings.user.access_token
    hub.auth.set_auth(access_token=access_token)
    return hub


# runs ~0.5s
def get_access_token(
    email: str | None = None, password: str | None = None, api_key: str | None = None
):
    hub = connect_hub()
    try:
        if api_key is not None:
            auth_response = hub.functions.invoke(
                "get-jwt-v1",
                invoke_options={"body": {"api_key": api_key}},
            )
            return json.loads(auth_response)["accessToken"]
        auth_response = hub.auth.sign_in_with_password(
            {
                "email": email,
                "password": password,
            }
        )
        return auth_response.session.access_token
    finally:
        hub.auth.sign_out(options={"scope": "local"})


def call_with_fallback_auth(
    callable,
    **kwargs,
):
    access_token = kwargs.pop("access_token", None)

    if access_token is not None:
        try:
            client = connect_hub_with_auth(access_token=access_token)
            result = callable(**kwargs, client=client)
        finally:
            try:
                client.auth.sign_out(options={"scope": "local"})
            except NameError:
                pass
        return result

    for renew_token, fallback_env in [(False, False), (True, False), (False, True)]:
        try:
            client = connect_hub_with_auth(
                renew_token=renew_token, fallback_env=fallback_env
            )
            result = callable(**kwargs, client=client)
            # we update access_token here
            # because at this point the call has been successfully resolved
            if renew_token:
                from lamindb_setup import settings

                # here settings.user contains an updated access_token
                save_user_settings(settings.user)
            break
        # we use Exception here as the ways in which the client fails upon 401
        # are not consistent and keep changing
        # because we ultimately raise the error, it's OK I'd say
        except Exception as e:
            if fallback_env:
                raise e
        finally:
            try:
                client.auth.sign_out(options={"scope": "local"})
            except NameError:
                pass
    return result


def call_with_fallback(
    callable,
    **kwargs,
):
    for fallback_env in [False, True]:
        try:
            client = connect_hub(fallback_env=fallback_env)
            result = callable(**kwargs, client=client)
            break
        except AuthUnknownError as e:
            if fallback_env:
                raise e
        finally:
            try:
                # in case there was sign in
                client.auth.sign_out(options={"scope": "local"})
            except NameError:
                pass
    return result
