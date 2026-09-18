import json
import os
from datetime import datetime, timezone
from pathlib import Path

from station.prototypes.boundary import ext_dict, ext_str
from station.prototypes.fs_paths import write_atomic
from station.prototypes.grok.oauth import (
    DEFAULT_DEVICE_AUTHORIZATION_ENDPOINT,
    DEFAULT_TOKEN_ENDPOINT,
    OAuthCredentials,
)

SELECTED_PROVIDER_FILE = "device_login.json"
XAI_OAUTH_PROVIDER = "xai-oauth"


def selected_provider_path(storage_dir):
    return os.path.join(storage_dir, SELECTED_PROVIDER_FILE)


def load_selected_provider(storage_dir):
    path = selected_provider_path(storage_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data = ext_dict("hermes device login selection", data)
    provider = ext_str("provider", data.get("provider")).strip()
    if not provider:
        return None
    return {
        "provider": provider,
        "model": ext_str("model", data.get("model")).strip(),
    }


def save_selected_provider(storage_dir, *, provider, model=""):
    payload = {"provider": provider, "model": model or ""}
    write_atomic(selected_provider_path(storage_dir), json.dumps(payload, indent=2), mode=0o600)


def delete_selected_provider(storage_dir):
    path = selected_provider_path(storage_dir)
    if os.path.isfile(path):
        os.remove(path)


def auth_store_path(hermes_home):
    return str(Path(hermes_home) / "auth.json")


def _load_auth_store(hermes_home):
    path = auth_store_path(hermes_home)
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return ext_dict("hermes auth.json", data)


def _save_auth_store(hermes_home, store):
    write_atomic(auth_store_path(hermes_home), json.dumps(store, indent=2), mode=0o600)


def load_xai_credentials(hermes_home):
    store = _load_auth_store(hermes_home)
    providers = store.get("providers")
    if not isinstance(providers, dict):
        return None
    state = providers.get(XAI_OAUTH_PROVIDER)
    if not isinstance(state, dict):
        return None
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        return None
    access = ext_str("access_token", tokens.get("access_token")).strip()
    refresh = ext_str("refresh_token", tokens.get("refresh_token")).strip()
    if not access or not refresh:
        return None
    discovery = state.get("discovery")
    token_endpoint = ""
    if isinstance(discovery, dict):
        token_endpoint = ext_str("token_endpoint", discovery.get("token_endpoint")).strip()
    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=0,
        token_endpoint=token_endpoint,
        id_token=ext_str("id_token", tokens.get("id_token")).strip(),
        token_type=ext_str("token_type", tokens.get("token_type"), default="Bearer") or "Bearer",
    )


def save_xai_credentials(hermes_home, credentials):
    Path(hermes_home).mkdir(parents=True, exist_ok=True)
    store = _load_auth_store(hermes_home)
    providers = store.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        store["providers"] = providers
    last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    providers[XAI_OAUTH_PROVIDER] = {
        "tokens": {
            "access_token": credentials.access,
            "refresh_token": credentials.refresh,
            "id_token": credentials.id_token,
            "token_type": credentials.token_type or "Bearer",
        },
        "last_refresh": last_refresh,
        "auth_mode": "oauth_device_code",
        "discovery": {
            "token_endpoint": credentials.token_endpoint or DEFAULT_TOKEN_ENDPOINT,
            "device_authorization_endpoint": DEFAULT_DEVICE_AUTHORIZATION_ENDPOINT,
        },
    }
    store["active_provider"] = XAI_OAUTH_PROVIDER
    _save_auth_store(hermes_home, store)


def delete_device_login_credentials(hermes_home):
    path = auth_store_path(hermes_home)
    if os.path.isfile(path):
        os.remove(path)
