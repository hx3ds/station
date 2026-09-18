import aiohttp
import argparse
import asyncio
import getpass
import json
import os
import re
import sys
import tomllib
from pathlib import Path

from .station import Station
from station.prototypes.boundary import ext_bool, ext_dict, ext_float, ext_int, ext_list, ext_require, ext_str

def _get_env(key: str) -> str | None:
    value = os.environ.get(key)
    if value is None:
        return None
    value = value.strip()
    return value if value else None

def _get_config_value(config: dict, key: str):
    if key in config:
        return config[key]
    return None

def _get_config_str(config: dict, key: str) -> str:
    value = _get_config_value(config, key)
    if value is None:
        return ""
    return ext_str(key, value)

def _load_toml_dict(path: str) -> dict:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return ext_dict("TOML root", data)

def _resolve_secret_path(*, config: dict, secret_path_override: str | None = None) -> str:
    path = (
        (secret_path_override or "").strip()
        or _get_env("STATION_SECRET_FILE")
        or _get_config_str(config, "STATION_SECRET_FILE")
    )
    return path

def _load_merged_config(*, config_path: str, secret_path_override: str | None = None) -> tuple[dict, str]:
    config = _load_toml_dict(config_path)
    secret_path = (
        (secret_path_override or "").strip()
        or _get_env("STATION_SECRET_FILE")
        or _get_config_str(config, "STATION_SECRET_FILE")
    )
    if not secret_path:
        return config, ""
    if not Path(secret_path).is_file():
        raise RuntimeError(f"STATION_SECRET_FILE not found: {secret_path}")
    secrets = _load_toml_dict(secret_path)
    merged = dict(config)
    merged.update(secrets)
    return merged, secret_path

def _resolve_admin_token(
    *,
    explicit: str | None = None,
    config_path: str | None = None,
    secret_path: str | None = None,
) -> str:
    token = (explicit or "").strip() or _get_env("STATION_ADMIN_TOKEN") or ""
    if token:
        return token
    path = (config_path or "").strip() or _get_env("STATION_CONFIG_FILE") or ""
    if not path:
        return ""
    config, _ = _load_merged_config(config_path=path, secret_path_override=secret_path)
    return _get_config_str(config, "STATION_ADMIN_TOKEN")

def _require_consul_credentials(config: dict) -> tuple[str, str]:
    username = _get_config_str(config, "CONSUL_USERNAME") or (_get_env("CONSUL_USERNAME") or "")
    password = _get_config_str(config, "CONSUL_PASSWORD") or (_get_env("CONSUL_PASSWORD") or "")
    if not username:
        username = input("Consul username: ").strip()
    if not password:
        password = getpass.getpass("Consul password: ")
    if not username or not password:
        raise RuntimeError("CONSUL_USERNAME and CONSUL_PASSWORD are required")
    return username, password

def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        raise argparse.ArgumentTypeError("Expected a boolean value")
    s = value.strip().lower()
    if s in ("true", "1", "yes", "y", "on"):
        return True
    if s in ("false", "0", "no", "n", "off"):
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")

def _toml_literal(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value:
            raise TypeError("float NaN is not a valid TOML literal")
        return format(value, "g")
    if isinstance(value, str):
        return json.dumps(value)
    if value is None:
        return json.dumps("")
    raise TypeError("unsupported TOML literal type: %s" % type(value).__name__)

def _find_prototypes_table_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped == "[[prototypes]]" or stripped.startswith("[[prototypes]]"):
            start = i
            j = i + 1
            while j < len(lines):
                s = lines[j].strip()
                if s.startswith("[[") or (s.startswith("[") and not s.startswith("[[")):
                    break
                j += 1
            ranges.append((start, j))
            i = j
            continue
        i += 1
    return ranges

def _table_name(lines: list[str], start: int, end: int) -> str:
    pattern = re.compile(r"^\s*name\s*=\s*(.*?)\s*(#.*)?$")
    for i in range(start + 1, end):
        match = pattern.match(lines[i])
        if not match:
            continue
        raw = match.group(1).strip()
        if not raw:
            return ""
        value = tomllib.loads("v = %s" % raw)["v"]
        return ext_str("table name", value)
    return ""

def _apply_kv_updates_to_table_lines(
    lines: list[str],
    start: int,
    end: int,
    updates: dict[str, object],
) -> tuple[list[str], int]:
    found = {k: False for k in updates}
    for i in range(start + 1, end):
        stripped = lines[i].lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        for key, value in updates.items():
            if found[key]:
                continue
            if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i]):
                ending = "\r\n" if lines[i].endswith("\r\n") else "\n"
                lines[i] = f"{key} = {_toml_literal(value)}{ending}"
                found[key] = True

    missing = [k for k, ok in found.items() if not ok]
    if missing:
        insert_at = end
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        for key in missing:
            lines.insert(insert_at, f"{key} = {_toml_literal(updates[key])}\n")
            insert_at += 1
            end += 1
    return lines, end

def _apply_kv_updates_to_prototypes_table_by_name(
    text: str,
    name: str,
    updates: dict[str, object],
) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("prototype table name is required")
    if not updates:
        return text
    lines = text.splitlines(keepends=True)
    ranges = _find_prototypes_table_ranges(lines)
    for start, end in ranges:
        if _table_name(lines, start, end) != name:
            continue
        lines, _new_end = _apply_kv_updates_to_table_lines(lines, start, end, updates)
        return "".join(lines)
    raise RuntimeError(f"[[prototypes]] table with name={name!r} not found")

def _normalize_base_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "http://" + url
    return url.rstrip("/")

async def _post_station_admin(
    url: str,
    *,
    payload: dict,
    headers: dict,
) -> tuple[int, dict | None, str]:
    async with aiohttp.ClientSession(trust_env=False) as direct:
        async with direct.post(url, json=payload, headers=headers) as resp:
            raw = await resp.text()
            body: dict | None = None
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
                try:
                    body = ext_dict("body", parsed)
                except TypeError:
                    body = None
            return resp.status, body, raw

def _raise_admin_error(route: str, status: int, body: dict | None, raw: str) -> None:
    if body is None:
        suffix = f", body={raw!r}" if raw else ""
        raise RuntimeError(f"{route} failed (status={status}{suffix})")
    raise RuntimeError(body.get("msg") or f"{route} failed (status={status})")

async def _admin_update_station_token(
    *,
    session: aiohttp.ClientSession,
    station_url: str,
    admin_token: str,
    new_token: str,
    grace_seconds: int = 300,
    prototype_id: int | None = None,
) -> None:
    del session
    station_url = _normalize_base_url(station_url)
    if not station_url:
        raise ValueError("station_url is required")
    admin_token = (admin_token or "").strip()
    if not admin_token:
        raise ValueError("admin_token is required")
    new_token = (new_token or "").strip()
    if not new_token:
        raise ValueError("new_token is required")
    if grace_seconds < 0:
        raise ValueError("grace_seconds must be an int >= 0")

    url = f"{station_url}/admin/prototype_token"
    headers = {"X-Admin-Token": admin_token}
    payload: dict[str, object] = {"token": new_token, "grace_seconds": grace_seconds}
    if prototype_id is not None:
        payload["prototype_id"] = prototype_id
    status, body, raw = await _post_station_admin(url, payload=payload, headers=headers)
    try:
        body = ext_dict("body", body)
    except TypeError:
        body = None
    if status != 200 or body is None or body.get("result") != 0:
        _raise_admin_error("admin/prototype_token", status, body, raw)

async def _admin_attach_prototype(
    *,
    session: aiohttp.ClientSession,
    station_url: str,
    admin_token: str,
    prototype_id: int,
    token: str,
    kind: str = "station",
    name: str | None = None,
    config_file: str | None = None,
    secret_file: str | None = None,
    persist: bool = False,
    ava: bool = False,
    reply_to: bool = False,
) -> dict:
    del session
    station_url = _normalize_base_url(station_url)
    if not station_url:
        raise ValueError("station_url is required")
    admin_token = (admin_token or "").strip()
    if not admin_token:
        raise ValueError("admin_token is required")
    url = f"{station_url}/admin/prototype"
    headers = {"X-Admin-Token": admin_token}
    kind = (kind or "station").strip() or "station"
    name = (name or "").strip() or None
    token = (token or "").strip()
    payload = {
        "prototype_id": prototype_id,
        "token": token,
        "kind": kind,
        "name": name,
        "config_file": config_file,
        "secret_file": secret_file,
        "persist": persist,
        "ava": ava,
        "reply_to": reply_to,
    }
    status, body, raw = await _post_station_admin(url, payload=payload, headers=headers)
    try:
        body = ext_dict("body", body)
    except TypeError:
        body = None
    if status != 200 or body is None or body.get("result") != 0:
        _raise_admin_error("admin/prototype", status, body, raw)
    data = body.get("data")
    if data is None:
        return {}
    return ext_dict("admin/prototype data", data)

async def _sign_in(*, session: aiohttp.ClientSession, consul_url: str, username: str, password: str) -> None:
    url = f"{consul_url.rstrip('/')}/api/sign_in"
    async with session.post(url, json={
        "telegram_data": {},
        "username": username or "",
        "email": "",
        "password": password or "",
    }) as resp:
        body = ext_dict("sign_in body", await resp.json(content_type=None))
        if resp.status != 200 or body.get("result") != 0:
            raise RuntimeError(body.get("msg") or f"sign_in failed (status={resp.status})")

def _optional_str_field(src: dict, key: str, default: str) -> str:
    if key not in src or src[key] is None:
        return default
    return ext_str(key, src[key], default=default, strip=False)

def _optional_int_field(src: dict, key: str, default: int) -> int:
    if key not in src or src[key] is None:
        return default
    return ext_int(key, src[key])

def _optional_float_field(src: dict, key: str, default: float) -> float:
    if key not in src or src[key] is None:
        return default
    return float(ext_float(key, src[key]))

def _complete_ensure_prototype_payload(payload: dict) -> dict:
    src = dict(payload)
    raw_type = src.get("type")
    if raw_type is None:
        p_type = "token"
    else:
        p_type = ext_str("type", raw_type).strip() or "token"
    raw_local = src.get("is_local")
    if raw_local is None:
        is_local = False
    else:
        is_local = ext_bool("is_local", raw_local)
    billing_interval = src.get("billing_interval")
    if billing_interval is None:
        billing_interval = "monthly" if p_type == "subscription" else ""
    elif p_type != "subscription":
        billing_interval = ""
    else:
        billing_interval = ext_str("billing_interval", billing_interval, strip=False)
    qr_platforms = src.get("qr_platforms")
    if qr_platforms is None:
        qr_platforms = []
    else:
        qr_platforms = ext_list("qr_platforms", qr_platforms)
    private = src.get("private")
    if private is None:
        private = False
    else:
        private = ext_bool("private", private)
    call_support = src.get("call_support")
    if call_support is None:
        call_support = False
    else:
        call_support = ext_bool("call_support", call_support)
    return {
        "name": _optional_str_field(src, "name", ""),
        "description": _optional_str_field(src, "description", ""),
        "access_point": _optional_str_field(src, "access_point", ""),
        "path": _optional_str_field(src, "path", ""),
        "status": _optional_str_field(src, "status", "active") or "active",
        "private": True if is_local else private,
        "max_chats": _optional_int_field(src, "max_chats", 1),
        "charge": 0 if is_local else _optional_float_field(src, "charge", 0),
        "max_charge_per_message": 0 if is_local else _optional_float_field(src, "max_charge_per_message", 0),
        "type": p_type,
        "billing_interval": billing_interval,
        "reply_window": _optional_int_field(src, "reply_window", 600),
        "is_local": is_local,
        "qr_platforms": qr_platforms,
        "terms_of_use": _optional_str_field(src, "terms_of_use", ""),
        "privacy_policy": _optional_str_field(src, "privacy_policy", ""),
        "call_support": call_support if p_type == "subscription" else False,
    }

async def _ensure_prototype(*, session: aiohttp.ClientSession, consul_url: str, payload: dict) -> dict:
    url = f"{consul_url.rstrip('/')}/api/ensure_prototype"
    payload = _complete_ensure_prototype_payload(payload)
    async with session.post(url, json=payload) as resp:
        body = ext_dict("ensure_prototype body", await resp.json(content_type=None))
        if resp.status != 200 or body.get("result") != 0:
            raise RuntimeError(body.get("msg") or f"ensure_prototype failed (status={resp.status})")
        return ext_dict("ensure_prototype data", body.get("data"))

async def _verify_station_token(*, session: aiohttp.ClientSession, consul_url: str, token: str) -> bool:
    url = f"{consul_url.rstrip('/')}/api/station/get_prototype"
    headers = {"X-Prototype-Token": token}
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            return False
        body = ext_dict("get_prototype body", await resp.json(content_type=None))
    return body.get("result") == 0

def _payload_from_prototype_entry(entry: dict) -> dict[str, object]:
    payload: dict[str, object] = {}
    mapping = {
        "description": "description",
        "type": "type",
        "access_point": "access_point",
        "path": "path",
        "status": "status",
        "private": "private",
        "max_chats": "max_chats",
        "charge": "charge",
        "max_charge_per_message": "max_charge_per_message",
        "reply_window": "reply_window",
        "is_local": "is_local",
        "billing_interval": "billing_interval",
        "terms_of_use": "terms_of_use",
        "privacy_policy": "privacy_policy",
        "call_support": "call_support",
        "qr_platforms": "qr_platforms",
    }
    for src, dst in mapping.items():
        if src not in entry:
            continue
        val = entry[src]
        if isinstance(val, str) and not val.strip() and src not in ("private", "is_local", "call_support"):
            continue
        payload[dst] = val
    return payload

def _collect_named_prototypes(config: dict) -> list[dict]:
    raw_prototypes = config.get("prototypes")
    named: list[dict] = []
    seen_names: set[str] = set()
    if raw_prototypes is None:
        return named
    raw_prototypes = ext_list("prototypes", raw_prototypes)
    for i, entry in enumerate(raw_prototypes):
        entry = ext_dict("prototypes[%d]" % i, entry)
        name_raw = entry.get("name")
        if name_raw is None:
            continue
        name = ext_str("prototypes[%d].name" % i, name_raw)
        if not name:
            continue
        if name in seen_names:
            raise RuntimeError(f"Duplicate [[prototypes]] name: {name}")
        seen_names.add(name)
        named.append(entry)
    return named

async def sync_prototype(
    *,
    config_path: str | None = None,
    secret_path_override: str | None = None,
    consul_url_override: str | None = None,
    prototype_name_override: str | None = None,
    desired_overrides: dict[str, object] | None = None,
) -> int:
    config_path = (config_path or "").strip() or _get_env("STATION_CONFIG_FILE")
    if not config_path:
        raise RuntimeError("STATION_CONFIG_FILE is required")

    config_file = Path(config_path)
    raw = config_file.read_bytes()
    config, _secret_path = _load_merged_config(
        config_path=config_path,
        secret_path_override=secret_path_override,
    )

    consul_url = (
        (consul_url_override or "").strip()
        or _get_env("CONSUL_URL")
        or _get_config_str(config, "CONSUL_URL")
    )
    if not consul_url:
        raise RuntimeError("CONSUL_URL is required")

    named = _collect_named_prototypes(config)
    if not named:
        raise RuntimeError("At least one named [[prototypes]] entry is required for sync")
    override_name = (prototype_name_override or "").strip() or _get_env("PROTOTYPE_NAME") or ""
    if override_name and not any((e.get("name") or "").strip() == override_name for e in named):
        raise RuntimeError(f"[[prototypes]] entry with name={override_name!r} not found")
    overrides_target = override_name or (named[0].get("name") or "").strip()

    consul_username, consul_password = _require_consul_credentials(config)

    cookie_jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar, trust_env=True) as session:
        await _sign_in(session=session, consul_url=consul_url, username=consul_username, password=consul_password)

        old_text = raw.decode("utf-8")
        new_text = old_text
        verified_tokens: list[str] = []
        for entry in named:
            name = (entry.get("name") or "").strip()
            payload: dict[str, object] = {"name": name}
            payload.update(_payload_from_prototype_entry(entry))
            if desired_overrides and name == overrides_target:
                for k, v in desired_overrides.items():
                    if v is not None:
                        payload[k] = v
            ensured = await _ensure_prototype(session=session, consul_url=consul_url, payload=payload)
            ensured_id = ext_int("prototype_id", ensured["prototype_id"])
            ensured_token = ext_str("token", ensured["token"], strip=False)
            table_updates: dict[str, object] = {
                "id": ensured_id,
                "token": ensured_token,
                "name": name,
            }
            new_text = _apply_kv_updates_to_prototypes_table_by_name(new_text, name, table_updates)
            verified_tokens.append(ensured_token)

        if new_text != old_text:
            config_file.write_text(new_text, encoding="utf-8")

        for token in verified_tokens:
            ok = await _verify_station_token(session=session, consul_url=consul_url, token=token)
            if not ok:
                return 2

    return 0

def _build_update_token_parser(prog_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{prog_name} update-token")
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--secret", dest="secret_path", default=None)
    parser.add_argument("--station-url", dest="station_url", required=True)
    parser.add_argument("--admin-token", dest="admin_token", default=None)
    parser.add_argument("--prototype-token", dest="prototype_token", default=None)
    parser.add_argument("--prototype-id", dest="prototype_id", type=int, default=None)
    parser.add_argument("--grace-seconds", dest="grace_seconds", type=int, default=300)
    parser.add_argument("--consul-url", dest="consul_url", default=None)
    parser.add_argument("--prototype-name", dest="prototype_name", default=None)
    return parser

def _build_attach_prototype_parser(prog_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=f"{prog_name} attach-prototype")
    parser.add_argument("--station-url", dest="station_url", required=True)
    parser.add_argument("--admin-token", dest="admin_token", default=None)
    parser.add_argument("--prototype-id", dest="prototype_id", type=int, default=None)
    parser.add_argument("--prototype-token", dest="prototype_token", default=None)
    parser.add_argument("--kind", dest="kind", default="station")
    parser.add_argument("--name", "--prototype-name", dest="prototype_name", default=None)
    parser.add_argument("--config", dest="config_path", default=None)
    parser.add_argument("--secret", dest="secret_path", default=None)
    parser.add_argument("--consul-url", dest="consul_url", default=None)
    parser.add_argument("--signin", dest="do_signin", action="store_true")
    parser.add_argument("--config-file", dest="config_file", default=None)
    parser.add_argument("--secret-file", dest="secret_file", default=None)
    parser.add_argument("--persist", dest="persist", action="store_true")
    parser.add_argument("--ava", dest="ava", type=_parse_bool, default=False)
    parser.add_argument("--reply-to", dest="reply_to", type=_parse_bool, default=False)
    parser.add_argument("--description", dest="description", default=None)
    parser.add_argument("--type", dest="type", default=None)
    parser.add_argument("--access-point", dest="access_point", default=None)
    parser.add_argument("--path", dest="path", default=None)
    parser.add_argument("--status", dest="status", default=None)
    parser.add_argument("--private", dest="private", type=_parse_bool, default=None)
    parser.add_argument("--max-chats", dest="max_chats", type=int, default=None)
    parser.add_argument("--charge", dest="charge", type=float, default=None)
    parser.add_argument("--reply-window", dest="reply_window", type=int, default=None)
    parser.add_argument("--is-local", dest="is_local", type=_parse_bool, default=None)
    return parser

def _run_update_token(args: argparse.Namespace) -> int:
    config_path = (args.config_path or "").strip() or _get_env("STATION_CONFIG_FILE") or ""
    admin_token = _resolve_admin_token(
        explicit=args.admin_token,
        config_path=config_path,
        secret_path=args.secret_path,
    )
    if not admin_token:
        raise RuntimeError("STATION_ADMIN_TOKEN (or --admin-token / config) is required")

    async def run_update():
        cookie_jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=cookie_jar, trust_env=True) as session:
            new_token = (args.prototype_token or "").strip()

            if new_token:
                await _admin_update_station_token(
                    session=session,
                    station_url=args.station_url,
                    admin_token=admin_token,
                    new_token=new_token,
                    grace_seconds=args.grace_seconds,
                    prototype_id=args.prototype_id,
                )
                if config_path and args.prototype_id is not None:
                    config_file = Path(config_path)
                    old_text = config_file.read_text(encoding="utf-8")
                    lines = old_text.splitlines(keepends=True)
                    ranges = _find_prototypes_table_ranges(lines)
                    id_pattern = re.compile(r"^\s*id\s*=\s*(\d+)\s*(#.*)?$")
                    for start, end in ranges:
                        has_id = False
                        for i in range(start + 1, end):
                            match = id_pattern.match(lines[i])
                            if match and int(match.group(1)) == args.prototype_id:
                                has_id = True
                                break
                        if not has_id:
                            continue
                        name = _table_name(lines, start, end)
                        if not name:
                            raise RuntimeError(
                                f"[[prototypes]] id={args.prototype_id} needs a name to update token on disk"
                            )
                        new_text = _apply_kv_updates_to_prototypes_table_by_name(
                            old_text, name, {"token": new_token}
                        )
                        if new_text != old_text:
                            config_file.write_text(new_text, encoding="utf-8")
                        break
                return

            if not config_path:
                raise RuntimeError("STATION_CONFIG_FILE is required when --prototype-token is not provided")
            config, _secret_path = _load_merged_config(
                config_path=config_path,
                secret_path_override=args.secret_path,
            )
            consul_url = (
                (args.consul_url or "").strip()
                or _get_env("CONSUL_URL")
                or _get_config_str(config, "CONSUL_URL")
            )
            if not consul_url:
                raise RuntimeError("CONSUL_URL is required when --prototype-token is not provided")

            named = _collect_named_prototypes(config)
            if args.prototype_id is not None:
                want_id = args.prototype_id
                filtered = []
                for e in named:
                    raw_id = e.get("id")
                    try:
                        if raw_id is not None and ext_int("id", raw_id) == want_id:
                            filtered.append(e)
                    except TypeError:
                        pass
                named = filtered
                if not named:
                    raise RuntimeError(
                        f"prototype_id={want_id} not found in named [[prototypes]]"
                    )
            if not named:
                raise RuntimeError("At least one named [[prototypes]] entry is required")

            consul_username, consul_password = _require_consul_credentials(config)
            await _sign_in(
                session=session,
                consul_url=consul_url,
                username=consul_username,
                password=consul_password,
            )

            config_file = Path(config_path)
            old_text = config_file.read_text(encoding="utf-8")
            new_text = old_text

            for entry in named:
                name = (entry.get("name") or "").strip()
                payload: dict[str, object] = {"name": name}
                payload.update(_payload_from_prototype_entry(entry))
                ensured = await _ensure_prototype(
                    session=session,
                    consul_url=consul_url,
                    payload=payload,
                )
                token = ext_str("token", ensured.get("token"), default="")
                if not token:
                    raise RuntimeError(f"ensure_prototype returned empty token for name={name!r}")
                ensured_id = ext_int("prototype_id", ensured["prototype_id"])
                await _admin_update_station_token(
                    session=session,
                    station_url=args.station_url,
                    admin_token=admin_token,
                    new_token=token,
                    grace_seconds=args.grace_seconds,
                    prototype_id=ensured_id,
                )
                new_text = _apply_kv_updates_to_prototypes_table_by_name(
                    new_text,
                    name,
                    {"id": ensured_id, "token": token, "name": name},
                )

            if new_text != old_text:
                config_file.write_text(new_text, encoding="utf-8")

    asyncio.run(run_update())
    return 0

def _run_attach_prototype(args: argparse.Namespace) -> int:
    config_path = (args.config_path or "").strip() or _get_env("STATION_CONFIG_FILE") or ""
    admin_token = _resolve_admin_token(
        explicit=args.admin_token,
        config_path=config_path,
        secret_path=args.secret_path,
    )
    if not admin_token:
        raise RuntimeError("STATION_ADMIN_TOKEN (or --admin-token / config) is required")

    async def run_attach():
        cookie_jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(cookie_jar=cookie_jar, trust_env=True) as session:
            prototype_id = args.prototype_id
            token = (args.prototype_token or "").strip()
            name = (args.prototype_name or "").strip() or None

            if args.do_signin:
                if prototype_id is not None or token:
                    raise RuntimeError("do not pass --prototype-id/--prototype-token with --signin")
                config_path = (args.config_path or "").strip() or _get_env("STATION_CONFIG_FILE") or ""
                if not config_path:
                    raise RuntimeError("STATION_CONFIG_FILE (or --config) is required with --signin")
                config, _secret_path = _load_merged_config(
                    config_path=config_path,
                    secret_path_override=args.secret_path,
                )
                consul_url = (
                    (args.consul_url or "").strip()
                    or _get_env("CONSUL_URL")
                    or _get_config_str(config, "CONSUL_URL")
                )
                if not consul_url:
                    raise RuntimeError("CONSUL_URL is required with --signin")
                if not name:
                    raise RuntimeError("--name/--prototype-name is required with --signin")

                payload: dict[str, object] = {"name": name}
                for key, value in {
                    "description": args.description,
                    "type": args.type,
                    "access_point": args.access_point,
                    "path": args.path,
                    "status": args.status,
                    "private": args.private,
                    "max_chats": args.max_chats,
                    "charge": args.charge,
                    "reply_window": args.reply_window,
                    "is_local": args.is_local,
                }.items():
                    if value is not None:
                        payload[key] = value

                consul_username, consul_password = _require_consul_credentials(config)
                await _sign_in(
                    session=session,
                    consul_url=consul_url,
                    username=consul_username,
                    password=consul_password,
                )
                ensured = await _ensure_prototype(
                    session=session,
                    consul_url=consul_url,
                    payload=payload,
                )
                prototype_id = ext_int("prototype_id", ensured["prototype_id"])
                token = ext_str("token", ensured.get("token"), default="")
                if not token:
                    raise RuntimeError("ensure_prototype returned empty token")
            else:
                if prototype_id is None or not token:
                    raise RuntimeError(
                        "--prototype-id and --prototype-token are required unless --signin"
                    )

            data = await _admin_attach_prototype(
                session=session,
                station_url=args.station_url,
                admin_token=admin_token,
                prototype_id=prototype_id,
                token=token,
                kind=args.kind,
                name=name,
                config_file=args.config_file,
                secret_file=args.secret_file,
                persist=args.persist,
                ava=args.ava,
                reply_to=args.reply_to,
            )
            print(json.dumps({"ok": True, "data": data}, sort_keys=True))

    asyncio.run(run_attach())
    return 0

def _build_run_parser(prog_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog_name)
    parser.add_argument("--config", dest="config_path")
    parser.add_argument("--secret", dest="secret_path", default=None)
    parser.add_argument("--host", dest="host", default=None)
    parser.add_argument("--port", dest="port", type=int, default=None)
    parser.add_argument("--env", dest="env", default=None)
    parser.add_argument("--consul-url", dest="consul_url")
    parser.add_argument("--prototype-name", dest="prototype_name")
    signin = parser.add_mutually_exclusive_group()
    signin.add_argument("--signin", dest="do_signin", action="store_true")
    signin.add_argument("--no-signin", dest="do_signin", action="store_false")
    parser.set_defaults(do_signin=False)
    parser.add_argument("--db-backend", dest="db_backend")
    parser.add_argument("--db-dsn", dest="db_dsn")
    parser.add_argument("--db-path", dest="db_path", default=None)
    parser.add_argument("--fs-root", dest="fs_root", default=None)
    parser.add_argument("--prototype-id", dest="prototype_id", type=int, default=None)
    parser.add_argument("--token", dest="token", default=None)
    parser.add_argument("--prototype-config", dest="prototype_config_file", default=None)
    parser.add_argument("--prototype-secret", dest="prototype_secret_file", default=None)
    parser.add_argument("--ava", dest="ava", type=_parse_bool, default=None)
    parser.add_argument("--description", dest="description")
    parser.add_argument("--type", dest="type")
    parser.add_argument("--access-point", dest="access_point")
    parser.add_argument("--path", dest="path")
    parser.add_argument("--status", dest="status")
    parser.add_argument("--private", dest="private", type=_parse_bool)
    parser.add_argument("--max-chats", dest="max_chats", type=int)
    parser.add_argument("--charge", dest="charge", type=float)
    parser.add_argument("--reply-window", dest="reply_window", type=int)
    parser.add_argument("--is-local", dest="is_local", type=_parse_bool)
    return parser

def _start_station(
    *,
    args: argparse.Namespace,
    prototype_class=None,
    prototype_kind=None,
) -> None:
    config_file = (args.config_path or "").strip() or os.environ.get("STATION_CONFIG_FILE")
    secret_file = (args.secret_path or "").strip() or os.environ.get("STATION_SECRET_FILE")
    Station(
        host=args.host,
        port=args.port,
        config_file=config_file,
        secret_file=secret_file,
        env=args.env,
        consul_url=args.consul_url,
        db_backend=args.db_backend,
        db_dsn=args.db_dsn,
        db_path=args.db_path,
        fs_root=args.fs_root,
        prototype_id=args.prototype_id,
        token=args.token,
        prototype_config_file=args.prototype_config_file,
        prototype_secret_file=args.prototype_secret_file,
        ava=args.ava,
        prototype=prototype_class,
        prototype_kind=prototype_kind,
    ).start()

def run_cli(
    *,
    prog_name: str,
    default_type: str | None = None,
    argv: list[str] | None = None,
    prototype_class=None,
    prototype_kind=None,
) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in ("update-token", "update_token"):
        parser = _build_update_token_parser(prog_name)
        return _run_update_token(parser.parse_args(args_list[1:]))
    if args_list and args_list[0] in ("attach-prototype", "attach_prototype"):
        parser = _build_attach_prototype_parser(prog_name)
        return _run_attach_prototype(parser.parse_args(args_list[1:]))

    parser = _build_run_parser(prog_name)
    args = parser.parse_args(args_list)

    from station.recorder import bootstrap_logger

    logger = bootstrap_logger()

    if args.do_signin:
        desired_overrides = {
            "description": args.description,
            "type": args.type or default_type,
            "access_point": args.access_point,
            "path": args.path,
            "status": args.status,
            "private": args.private,
            "max_chats": args.max_chats,
            "charge": args.charge,
            "reply_window": args.reply_window,
            "is_local": args.is_local,
        }
        code = asyncio.run(
            sync_prototype(
                config_path=args.config_path,
                secret_path_override=args.secret_path,
                consul_url_override=args.consul_url,
                prototype_name_override=args.prototype_name,
                desired_overrides=desired_overrides,
            )
        )
        if code != 0:
            return code

    try:
        _start_station(
            args=args,
            prototype_class=prototype_class,
            prototype_kind=prototype_kind,
        )
    except SystemExit:
        raise
    except Exception as e:
        logger.error("startup failed error=%s", e)
        sys.exit(1)
    return 0
