import hashlib
import hmac
import os
import shutil
from pathlib import Path

from aiohttp import web

from station.prototypes.boundary import ext_bool as _b_bool
from station.prototypes.boundary import ext_int as _b_int
from station.prototypes.boundary import ext_optional_id
from station.prototypes.boundary import ext_require
from station.prototypes.boundary import ext_str as _b_str

def inbound_request_id(*parts) -> str:
    raw = ":".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def constant_time_equal(a: str, b: str) -> bool:
    a = ext_require("a", a, (str,))
    b = ext_require("b", b, (str,))
    if len(a) != len(b):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

def ext_str(value, name, *, default="", allow_none=True):
    if value is None:
        if allow_none:
            return default
        raise TypeError(f"{name} is required")
    return _b_str(name, value, default=default, strip=False)

def ext_id(value, name, *, default="", allow_none=True):
    if value is None:
        if allow_none:
            return default
        raise TypeError(f"{name} is required")
    v = ext_optional_id(name, value)
    if isinstance(v, int):
        return str(v)
    return v

def ext_bool(value, name, *, default=False, allow_none=True):
    if value is None:
        if allow_none:
            return default
        raise TypeError(f"{name} is required")
    return _b_bool(name, value, default=default)

def ext_int(value, name, *, default=0, allow_none=True):
    if value is None:
        if allow_none:
            return default
        raise TypeError(f"{name} is required")
    return _b_int(name, value, default=default)

def ok(data=None, *, status=200, msg=None):
    payload = {"ok": True}
    if msg is not None:
        payload["msg"] = msg
    if data is not None:
        payload["data"] = data
    return web.json_response(payload, status=status)

def err(msg, *, status=400, data=None):
    payload = {"ok": False, "msg": msg}
    if data is not None:
        payload["data"] = data
    return web.json_response(payload, status=status)

def normalize_http_url(raw):
    if raw is None:
        return ""
    raw = _b_str("url", raw, default="", strip=True)
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return ("https://" + raw).rstrip("/")

def attachment_type_from_meta(*, filename=None, content_type=None, default_type="document"):
    if filename is None:
        fn = ""
    else:
        fn = _b_str("filename", filename, default="", strip=False).lower()
    if content_type is None:
        ct = ""
    else:
        ct = _b_str("content_type", content_type, default="", strip=False).lower()
    if ct.startswith("image/") or fn.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
        return "photo"
    if ct.startswith("video/") or fn.endswith((".mp4", ".webm", ".mov", ".mkv", ".avi")):
        return "video"
    if ct.startswith("audio/") or fn.endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")):
        return "audio"
    return default_type

def whatsapp_bridge_dir(*, need_pair=False):
    env = os.environ.get("LOCAL_CONDUCTOR_WHATSAPP_BRIDGE_DIR")
    if env is None:
        env = ""
    env = env.strip()
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "scripts" / "whatsapp-bridge",
        here.parents[2] / "scripts" / "whatsapp-bridge",
        Path.cwd() / "scripts" / "whatsapp-bridge",
        Path.cwd() / "station" / "scripts" / "whatsapp-bridge",
    ]
    for c in candidates:
        if need_pair:
            if (c / "pair.js").is_file():
                return c
        elif (c / "serve.js").is_file() or (c / "pair.js").is_file():
            return c
    return candidates[0]

def node_bin():
    env = os.environ.get("LOCAL_CONDUCTOR_NODE")
    if env is not None and env.strip():
        return env.strip()
    which = shutil.which("node")
    if which is not None and which.strip():
        return which.strip()
    return "node"
