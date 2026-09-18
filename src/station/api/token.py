import os
import json
import re
from pathlib import Path
from aiohttp import web

from station.client.consul import fetch_prototype
from station.client.context import ClientContext, update_client_context_prototype
from station.config.config import PrototypeConfig
from station.conductor.handlers import ensure_local_conductor
from station.prototypes.registry import resolve_prototype_class
from station.tenants import Tenant, TokenState
from station.prototypes.boundary import ext_bool, ext_dict, ext_int, ext_str

def _is_localhost_request(request) -> bool:
    transport = request.transport
    if transport is None:
        return False
    peer = transport.get_extra_info("peername")
    if not peer:
        return False
    return peer[0] in ("127.0.0.1", "::1")

def _require_admin(request):
    if not _is_localhost_request(request):
        return web.json_response({"result": 1, "msg": "Forbidden", "data": None}, status=403)
    header_token = request.headers.get("X-Admin-Token") or ""
    admin_token = (os.environ.get("STATION_ADMIN_TOKEN") or "").strip()
    if not admin_token:
        admin_token = (request.app["config"].admin_token or "").strip()
    if not admin_token:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)
    if header_token.strip() != admin_token:
        return web.json_response({"result": 1, "msg": "Unauthorized", "data": None}, status=401)
    return None

def _toml_literal(value) -> str:
    return "true" if value else "false"

def _find_prototypes_table_ranges(lines: list[str]) -> list[tuple[int, int]]:
    ranges = []
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

def _persist_prototypes_array_token(config_file_path: str, prototype_id: int, token: str) -> bool:
    path = Path(config_file_path)
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    ranges = _find_prototypes_table_ranges(lines)
    id_pattern = re.compile(r"^\s*id\s*=\s*(\d+)\s*(#.*)?$")
    token_pattern = re.compile(r"^(\s*token\s*=\s*)(.*?)(\s*(#.*)?)\s*(\r?\n)?$")
    for start, end in ranges:
        has_id = False
        for i in range(start + 1, end):
            match = id_pattern.match(lines[i])
            if match and int(match.group(1)) == prototype_id:
                has_id = True
                break
        if not has_id:
            continue
        replaced = False
        for i in range(start + 1, end):
            match = token_pattern.match(lines[i])
            if not match:
                continue
            prefix = match.group(1)
            suffix = match.group(3) or ""
            ending = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = f"{prefix}{json.dumps(token)}{suffix}{ending}"
            replaced = True
            break
        if not replaced:
            lines.insert(end, f"token = {json.dumps(token)}\n")
        path.write_text("".join(lines), encoding="utf-8")
        return True
    return False

def _append_prototypes_table(
    config_file_path: str,
    *,
    prototype_id: int,
    token: str,
    kind: str,
    config_file: str | None,
    secret_file: str | None,
    ava: bool,
    reply_to: bool,
    name: str | None = None,
) -> None:
    path = Path(config_file_path)
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    if text and not text.endswith("\n\n"):
        text += "\n"
    block = ["[[prototypes]]"]
    if name:
        block.append(f"name = {json.dumps(name)}")
    block.extend(
        [
            f"id = {prototype_id}",
            f"token = {json.dumps(token)}",
            f"kind = {json.dumps(kind)}",
        ]
    )
    if config_file:
        block.append(f"config_file = {json.dumps(config_file)}")
    if secret_file:
        block.append(f"secret_file = {json.dumps(secret_file)}")
    block.append(f"ava = {_toml_literal(ava)}")
    block.append(f"reply_to = {_toml_literal(reply_to)}")
    block.append("")
    path.write_text(text + "\n".join(block) + "\n", encoding="utf-8")

async def handle_admin_update_prototype_token(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied

    app = request.app
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)
    try:
        body = ext_dict("body", body)
        new_token = ext_str("token", body.get("token"), default="")
        if not new_token:
            raise TypeError("Missing token")
        grace_seconds = body.get("grace_seconds", 300)
        grace_seconds = ext_int("grace_seconds", grace_seconds)
        if grace_seconds < 0:
            raise TypeError("Invalid grace_seconds")
        prototype_id = body.get("prototype_id")
        if prototype_id is not None:
            prototype_id = ext_int("prototype_id", prototype_id)
    except TypeError as e:
        msg = str(e)
        if "token" in msg.lower() and "must be str" in msg:
            msg = "Missing token"
        elif "grace_seconds" in msg:
            msg = "Invalid grace_seconds"
        elif "prototype_id" in msg:
            msg = "Invalid prototype_id"
        elif "body" in msg or "dict" in msg:
            msg = "Invalid JSON body"
        return web.json_response({"result": 1, "msg": msg, "data": None}, status=400)

    registry = app["tenants"]
    config = app["config"]

    try:
        tenant = await registry.rotate_token(prototype_id, new_token, grace_seconds=grace_seconds)
    except KeyError:
        return web.json_response({"result": 1, "msg": "Unknown prototype_id", "data": None}, status=404)
    for cfg in config.hosted_prototypes:
        if cfg.id is not None and cfg.id == tenant.id:
            cfg.token = new_token
            break
    config_file_path = config.config_file_path
    if not config_file_path:
        raise ValueError("Config file path not available")
    _persist_prototypes_array_token(config_file_path, tenant.id, new_token)
    return web.json_response(
        {"result": 0, "msg": "ok", "data": {"grace_seconds": grace_seconds, "prototype_id": tenant.id}},
        status=200,
    )

async def handle_admin_add_prototype(request):
    denied = _require_admin(request)
    if denied is not None:
        return denied

    app = request.app
    registry = app["tenants"]
    config = app["config"]

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"result": 1, "msg": "Invalid JSON body", "data": None}, status=400)
    try:
        body = ext_dict("body", body)
        prototype_id = ext_int("prototype_id", body.get("prototype_id"))
        token = ext_str("token", body.get("token"), default="")
        if not token:
            raise TypeError("Missing token")
        kind = body.get("kind", "station")
        if kind is None:
            kind = "station"
        kind = ext_str("kind", kind, default="").strip()
        if not kind:
            raise TypeError("Invalid kind")
        name = body.get("name")
        if name is not None:
            name = ext_str("name", name, default="").strip() or None
        config_file = body.get("config_file")
        if config_file is not None:
            config_file = ext_str("config_file", config_file, default="").strip() or None
        secret_file = body.get("secret_file")
        if secret_file is not None:
            secret_file = ext_str("secret_file", secret_file, default="").strip() or None
        persist = ext_bool("persist", body.get("persist", False))
        ava = ext_bool("ava", body.get("ava", False))
        reply_to = ext_bool("reply_to", body.get("reply_to", False))
    except TypeError as e:
        msg = str(e)
        if "prototype_id" in msg:
            msg = "Missing prototype_id"
        elif msg == "Missing token" or ("token" in msg and "str" in msg):
            msg = "Missing token"
        elif "kind" in msg:
            msg = "Invalid kind"
        elif "name" in msg:
            msg = "Invalid name"
        elif "config_file" in msg:
            msg = "Invalid config_file"
        elif "secret_file" in msg:
            msg = "Invalid secret_file"
        elif "persist" in msg:
            msg = "Invalid persist"
        elif "ava" in msg:
            msg = "Invalid ava"
        elif "reply_to" in msg:
            msg = "Invalid reply_to"
        else:
            msg = "Invalid JSON body"
        return web.json_response({"result": 1, "msg": msg, "data": None}, status=400)

    if registry.hosts(prototype_id):
        return web.json_response({"result": 1, "msg": "prototype_id already attached", "data": None}, status=409)
    if registry.authorize(token) is not None:
        return web.json_response({"result": 1, "msg": "token already in use", "data": None}, status=409)

    session = app["session"]
    consul_url = config.consul_url
    fetched = await fetch_prototype(session=session, consul_url=consul_url, token=token)
    is_local = False
    if fetched:
        fetched_id = fetched.get("prototype_id")
        if fetched_id is not None:
            try:
                prototype_id = ext_int("prototype_id", fetched_id)
            except TypeError:
                return web.json_response({"result": 1, "msg": "Invalid prototype_id from consul", "data": None}, status=400)
        try:
            is_local = ext_bool("is_local", fetched.get("is_local", False))
        except TypeError:
            return web.json_response({"result": 1, "msg": "Invalid is_local from consul", "data": None}, status=400)
        await app["db"].put_prototype(fetched)

    try:
        resolve_prototype_class(kind)
    except ValueError as e:
        return web.json_response({"result": 1, "msg": str(e), "data": None}, status=400)

    token_state = TokenState(token)
    client_context = ClientContext(
        session=session,
        consul_url=consul_url,
        env=config.env,
        prototype_id=prototype_id,
        token=token,
        prototype_version=0,
        prototype_type="",
        db=app["db"],
        app=app,
    )
    if fetched:
        update_client_context_prototype(client_context, fetched)

    try:
        registry.add(
            Tenant(
                id=prototype_id,
                kind=kind,
                token_state=token_state,
                client_context=client_context,
                config_file=config_file,
                secret_file=secret_file,
                ava=ava,
                reply_to=reply_to,
                is_local=is_local,
            ),
            primary=False,
        )
    except ValueError as e:
        return web.json_response({"result": 1, "msg": str(e), "data": None}, status=409)

    config.hosted_prototypes.append(
        PrototypeConfig(
            id=prototype_id,
            token=token,
            kind=kind,
            name=name,
            config_file=config_file,
            secret_file=secret_file,
            ava=ava,
            reply_to=reply_to,
        )
    )

    if is_local:
        ensure_local_conductor(app)

    if persist:
        config_file_path = config.config_file_path
        if not config_file_path:
            raise RuntimeError("Config file path not available")
        _append_prototypes_table(
            config_file_path,
            prototype_id=prototype_id,
            token=token,
            kind=kind,
            config_file=config_file,
            secret_file=secret_file,
            ava=ava,
            reply_to=reply_to,
            name=name,
        )

    return web.json_response(
        {
            "result": 0,
            "msg": "ok",
            "data": {
                "prototype_id": prototype_id,
                "kind": kind,
                "name": name,
                "is_local": is_local,
                "persisted": persist,
                "prototype_ids": registry.prototype_ids,
            },
        },
        status=200,
    )
