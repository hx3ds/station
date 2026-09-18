import json
import os

from station.prototypes.boundary import ext_dict, ext_int, ext_str
from station.prototypes.fs_paths import sanitize_path_component, write_atomic

from .oauth import OAuthCredentials, XAI_OAUTH_CLIENT_ID, XAI_OAUTH_ISSUER

def credentials_path(storage_dir, acct_id):
    root = os.path.join(storage_dir, "oauth")
    os.makedirs(root, exist_ok=True)
    return os.path.join(root, "%s.json" % sanitize_path_component(acct_id, empty="unknown"))

def _credentials_from_dict(data, *, label):

    data = ext_dict(label, data)
    access = ext_str("%s.access" % label, data.get("access"))
    refresh = ext_str("%s.refresh" % label, data.get("refresh"))
    if not access:
        raise TypeError("%s.access must be non-empty str" % label)
    if not refresh:
        raise TypeError("%s.refresh must be non-empty str" % label)
    expires = data.get("expires", 0)
    expires = ext_int("%s.expires" % label, expires, default=0, allow_none=True)
    if expires is None:
        expires = 0
    token_endpoint = ext_str("%s.token_endpoint" % label, data.get("token_endpoint"))
    id_token = ext_str("%s.id_token" % label, data.get("id_token"))
    token_type = ext_str("%s.token_type" % label, data.get("token_type"), default="Bearer") or "Bearer"
    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=expires,
        token_endpoint=token_endpoint,
        id_token=id_token,
        token_type=token_type,
    )

def load_credentials(storage_dir, acct_id):
    path = credentials_path(storage_dir, acct_id)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return _credentials_from_dict(data, label="oauth credentials")

def save_credentials(storage_dir, acct_id, credentials):
    path = credentials_path(storage_dir, acct_id)
    payload = {
        "access": credentials.access,
        "refresh": credentials.refresh,
        "expires": credentials.expires,
        "token_endpoint": credentials.token_endpoint,
        "id_token": credentials.id_token,
        "token_type": credentials.token_type,
    }
    write_atomic(path, json.dumps(payload), mode=0o600)
    return path

def delete_credentials(storage_dir, acct_id):
    path = credentials_path(storage_dir, acct_id)
    if os.path.isfile(path):
        os.remove(path)

def load_grok_cli_credentials():

    auth_path = os.path.join(os.path.expanduser("~"), ".grok", "auth.json")
    if not os.path.isfile(auth_path):
        return None
    with open(auth_path, "r", encoding="utf-8") as f:
        data = ext_dict("grok cli auth.json", json.load(f))
    scope_key = "%s::%s" % (XAI_OAUTH_ISSUER, XAI_OAUTH_CLIENT_ID)
    entry = data.get(scope_key)
    if entry is None:
        return None
    entry = ext_dict("grok cli auth entry", entry)
    access = ext_str("grok cli access_token", entry.get("access_token"))
    refresh = ext_str("grok cli refresh_token", entry.get("refresh_token"))
    if not access:
        raise TypeError("grok cli access_token must be non-empty str")
    if not refresh:
        raise TypeError("grok cli refresh_token must be non-empty str")
    expires_at = entry.get("expires_at", 0)
    if type(expires_at) is bool:
        raise TypeError("grok cli expires_at must be int or float")
    if type(expires_at) not in (int, float):
        raise TypeError("grok cli expires_at must be int or float")
    expires = int(expires_at * 1000) if expires_at < 10_000_000_000 else int(expires_at)
    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=expires,
        token_endpoint=ext_str("grok cli token_endpoint", entry.get("token_endpoint")),
        id_token=ext_str("grok cli id_token", entry.get("id_token")),
        token_type=ext_str("grok cli token_type", entry.get("token_type"), default="Bearer") or "Bearer",
    )
