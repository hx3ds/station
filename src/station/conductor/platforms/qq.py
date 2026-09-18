import asyncio
import base64
import hashlib
import json
import os
import re
import tempfile
import time
import uuid

import aiohttp

from station.conductor.crypto import decrypt_if_encrypted
from station.conductor.platforms import guidance
from station.conductor.platforms.keyboard import join_text_and_keyboard, parse_keyboard, qq_keyboard
from station.conductor.platforms.policy import allows_media
from station.conductor.util import attachment_type_from_meta, ext_id, ext_int, ext_str
from station import logger
from station.prototypes.boundary import ext_dict, ext_float, ext_list, ext_require

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
DEFAULT_API_BASE = "https://api.sgroup.qq.com"
GATEWAY_PATH = "/gateway"

INTENTS = 0x46001000
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2
MSG_TYPE_INPUT_NOTIFY = 6
MSG_TYPE_MEDIA = 7
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_VOICE = 3
MEDIA_TYPE_FILE = 4

INLINE_UPLOAD_MAX = 9 * 1024 * 1024
DEDUP_WINDOW_SECONDS = 300
DEDUP_MAX_SIZE = 1000
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
RATE_LIMIT_DELAY = 60
TYPING_INPUT_SECONDS = 60
TYPING_DEBOUNCE_SECONDS = 50
FILE_UPLOAD_TIMEOUT = 120.0
MD5_10M_SIZE = 10_002_432
BIZ_CODE_DAILY_LIMIT = 40093002
BIZ_CODE_PART_RETRYABLE = 40093001

_VOICE_EXTS = (".silk", ".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".speex", ".flac")
_AT_MENTION_RE = re.compile(r"^@\S+\s*")

def parse_qq_credentials(token):
    raw = (token or "").strip()
    if not raw:
        return "", ""
    if raw.startswith("{"):
        data = json.loads(raw)
        data = ext_dict('qq credentials json', data)
        app_id = ext_str(data.get("app_id"), "app_id").strip()
        secret = ext_str(data.get("client_secret"), "client_secret").strip()
        return app_id, secret
    if ":" in raw:
        app_id, secret = raw.split(":", 1)
        return app_id.strip(), secret.strip()
    return "", ""

def _normalize_keyboard(keyboard):
    if keyboard is None:
        return None
    native = qq_keyboard(keyboard)
    if native is None:
        raise TypeError("qq keyboard must contain content or rows")
    return native

def _format_size(size_bytes):
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

def _compute_file_hashes(file_path, file_size):
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    md5_10m = hashlib.md5()
    need_10m = file_size > MD5_10M_SIZE
    bytes_read = 0
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            md5.update(chunk)
            sha1.update(chunk)
            if need_10m:
                remaining = MD5_10M_SIZE - bytes_read
                if remaining > 0:
                    md5_10m.update(chunk[:remaining])
            bytes_read += len(chunk)
    full_md5 = md5.hexdigest()
    return {
        "md5": full_md5,
        "sha1": sha1.hexdigest(),
        "md5_10m": md5_10m.hexdigest() if need_10m else full_md5,
    }

def _read_file_chunk(file_path, offset, length):
    with open(file_path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(length)
        if len(data) != length:
            raise IOError(f"short read at offset {offset}")
        return data

def _parse_interaction(d):
    data_raw = d.get("data")
    if data_raw is None:
        data_raw = {}
    else:
        data_raw = ext_dict('qq interaction data', data_raw)
    resolved = data_raw.get("resolved")
    if resolved is None:
        resolved = {}
    else:
        resolved = ext_dict('qq interaction resolved', resolved)
    scene_code = ext_int(d.get("chat_type"), "chat_type")
    scene = {0: "guild", 1: "group", 2: "c2c"}.get(scene_code, "")
    return {
        "id": ext_id(d.get("id"), "id"),
        "scene": scene,
        "group_openid": ext_id(d.get("group_openid"), "group_openid"),
        "group_member_openid": ext_id(d.get("group_member_openid"), "group_member_openid"),
        "user_openid": ext_id(d.get("user_openid"), "user_openid"),
        "channel_id": ext_id(d.get("channel_id"), "channel_id"),
        "guild_id": ext_id(d.get("guild_id"), "guild_id"),
        "button_data": ext_str(resolved.get("button_data"), "button_data"),
        "button_id": ext_str(resolved.get("button_id"), "button_id"),
        "button_label": ext_str(resolved.get("button_label"), "button_label"),
    }

class QQIO:
    def __init__(self, conductor):
        self.conductor = conductor
        self._token_lock = asyncio.Lock()
        self._access_by_app: dict[str, dict] = {}
        self._chat_type_map: dict[str, dict[str, str]] = {}
        self._last_msg_id: dict[str, dict[str, str]] = {}
        self._typing_sent_at: dict[str, dict[str, float]] = {}
        self._seen_messages: dict[str, dict[str, float]] = {}
        self._ws_sessions: dict[str, dict] = {}

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.conductor.session

    def _api_base(self, server=None):
        raw = self.conductor.config.qq_api_url.strip()
        if not raw:
            for k in ("QQ_API_URL", "QQ_API_BASE"):
                v = (os.environ.get(k) or "").strip()
                if v:
                    raw = v
                    break
        if raw:
            return raw.rstrip("/")
        server = (server or "").strip().rstrip("/")
        if server.startswith("http://") or server.startswith("https://"):
            return server
        return DEFAULT_API_BASE

    def _token_url(self):
        raw = self.conductor.config.qq_token_url.strip()
        if not raw:
            raw = (os.environ.get("QQ_TOKEN_URL") or "").strip()
        return raw or TOKEN_URL

    def _chat_types(self, acct_id):
        cache = self._chat_type_map.get(acct_id)
        if cache is None:
            cache = {}
            self._chat_type_map[acct_id] = cache
        return cache

    def _last_msgs(self, acct_id):
        cache = self._last_msg_id.get(acct_id)
        if cache is None:
            cache = {}
            self._last_msg_id[acct_id] = cache
        return cache

    def _typing_times(self, acct_id):
        cache = self._typing_sent_at.get(acct_id)
        if cache is None:
            cache = {}
            self._typing_sent_at[acct_id] = cache
        return cache

    def _seen(self, acct_id):
        cache = self._seen_messages.get(acct_id)
        if cache is None:
            cache = {}
            self._seen_messages[acct_id] = cache
        return cache

    def _guess_chat_type(self, acct_id, chat_id):
        return self._chat_types(acct_id).get(chat_id) or "c2c"

    def _remember_chat_type(self, acct_id, chat_id, chat_type):
        chat_id = (chat_id or "").strip()
        chat_type = (chat_type or "").strip()
        if chat_id and chat_type:
            self._chat_types(acct_id)[chat_id] = chat_type

    def _is_seen(self, acct_id, msg_id):
        msg_id = (msg_id or "").strip()
        if not msg_id:
            return True
        seen = self._seen(acct_id)
        now = time.time()
        if len(seen) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages[acct_id] = {k: ts for k, ts in seen.items() if ts > cutoff}
            seen = self._seen(acct_id)
        return msg_id in seen

    def _mark_seen(self, acct_id, msg_id):
        msg_id = (msg_id or "").strip()
        if not msg_id:
            return
        seen = self._seen(acct_id)
        now = time.time()
        if len(seen) > DEDUP_MAX_SIZE:
            cutoff = now - DEDUP_WINDOW_SECONDS
            self._seen_messages[acct_id] = {k: ts for k, ts in seen.items() if ts > cutoff}
            seen = self._seen(acct_id)
        seen[msg_id] = now

    @staticmethod
    def _quoted_context(d):
        d = ext_dict('qq quoted message', d)
        msg_type = d.get("message_type")
        if msg_type is None:
            return "", "", []
        msg_type = ext_int(msg_type, "message_type")
        if msg_type != 103:
            return "", "", []
        elements = d.get("msg_elements")
        if elements is None:
            elements = []
        else:
            elements = ext_list('qq msg_elements', elements)
        lines = []
        reply_to = ""
        quote_atts = []
        for elem in elements:
            elem = ext_dict('qq msg_element', elem)
            if not reply_to:
                reply_to = ext_str(elem.get("id"), "id").strip()
            text = ext_str(elem.get("content"), "content").strip()
            if text:
                lines.append(text)
            eatts = elem.get("attachments")
            if eatts is not None:
                eatts = ext_list('qq quoted attachments', eatts)
                for a in eatts:
                    a = ext_dict('qq quoted attachment', a)
                    quote_atts.append(a)
        if not lines and not quote_atts:
            return "", reply_to, []
        if not lines:
            return "[Quoted message]: (attachment)", reply_to, quote_atts
        return "[Quoted message]:\n" + "\n".join(lines), reply_to, quote_atts

    @staticmethod
    def _strip_at_mention(content):
        return _AT_MENTION_RE.sub("", (content or "").strip()).strip()

    @staticmethod
    def _next_msg_seq(seed=""):
        time_part = int(time.time()) % 100000000
        rand = int(uuid.uuid4().hex[:4], 16)
        return (time_part ^ rand) % 65536

    async def _ensure_access_token(self, *, app_id, client_secret):
        app_id = (app_id or "").strip()
        client_secret = (client_secret or "").strip()
        if not app_id or not client_secret:
            raise RuntimeError("missing qq credentials")
        cached = self._access_by_app.get(app_id)
        if cached is not None and cached.get("token") and time.time() < cached["expires_at"] - 60:
            return cached["token"]
        async with self._token_lock:
            cached = self._access_by_app.get(app_id)
            if cached is not None and cached.get("token") and time.time() < cached["expires_at"] - 60:
                return cached["token"]
            async with self.session.post(
                self._token_url(),
                json={"appId": app_id, "clientSecret": client_secret},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json(content_type=None)
            data = ext_dict('qq token response', data)
            token = ext_str(data.get("access_token"), "access_token").strip()
            if not token:
                raise RuntimeError(f"token missing: {data}")
            expires_in = ext_int(data.get("expires_in"), "expires_in", default=7200)
            self._access_by_app[app_id] = {
                "token": token,
                "expires_at": time.time() + expires_in,
                "client_secret": client_secret,
            }
            return token

    async def _creds_from_token(self, token):
        app_id, client_secret = parse_qq_credentials(token)
        if not app_id or not client_secret:
            raise RuntimeError("invalid qq token")
        access = await self._ensure_access_token(app_id=app_id, client_secret=client_secret)
        return app_id, client_secret, access

    def _auth_headers(self, access_token):
        return {
            "Authorization": f"QQBot {access_token}",
            "Content-Type": "application/json",
            "User-Agent": "StationQQIO/1.0",
            "Accept": "application/json",
        }

    async def _api_request(self, *, token, method, path, body=None, timeout=30.0, server=None):
        app_id, client_secret, access = await self._creds_from_token(token)
        url = f"{self._api_base(server)}{path}"
        async with self.session.request(
            method.upper(),
            url,
            headers=self._auth_headers(access),
            json=body,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                raw = await resp.text()
                status = int(resp.status)
                if status >= 400:
                    if status in (401, 403):
                        app_id, _ = parse_qq_credentials(token)
                        self._access_by_app.pop(app_id, None)
                    raise RuntimeError(f"QQ API [{status}] {path}: {raw[:500]}")
                raise TypeError("qq api response must be json dict")
            data = ext_dict('qq api response', data)
            status = int(resp.status)
            if status >= 400:
                if status in (401, 403):
                    app_id, _ = parse_qq_credentials(token)
                    self._access_by_app.pop(app_id, None)
                msg = data.get("message", data)
                raise RuntimeError(f"QQ API [{status}] {path}: {msg}")
            return data

    async def _get_gateway_url(self, *, token, server=None):
        data = await self._api_request(token=token, method="GET", path=GATEWAY_PATH, server=server)
        url = ext_str(data.get("url"), "url").strip()
        if not url:
            raise RuntimeError("gateway url missing")
        return url

    async def download(self, *, token, url):
        url = (url or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return None
        _, _, access = await self._creds_from_token(token)
        headers = {"Authorization": f"QQBot {access}"} if access else {}
        async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if int(resp.status) != 200:
                return None
            return await resp.read()

    async def send_typing(self, *, token, chat_id, acct_id=""):
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return False
        if self._guess_chat_type(acct_id, chat_id) != "c2c":
            return False
        msg_id = self._last_msgs(acct_id).get(chat_id)
        if not msg_id:
            return False
        now = time.time()
        last = self._typing_times(acct_id).get(chat_id, 0.0)
        if now - last < TYPING_DEBOUNCE_SECONDS:
            return True
        body = {
            "msg_type": MSG_TYPE_INPUT_NOTIFY,
            "msg_id": msg_id,
            "input_notify": {"input_type": 1, "input_second": TYPING_INPUT_SECONDS},
            "msg_seq": self._next_msg_seq(chat_id),
        }
        await self._api_request(token=token, method="POST", path=f"/v2/users/{chat_id}/messages", body=body)
        self._typing_times(acct_id)[chat_id] = now
        return True

    def _build_text_body(self, content, reply_to=None, keyboard=None):
        msg_seq = self._next_msg_seq(reply_to or "default")
        markdown = guidance.qq_markdown_enabled()
        text = (content or "")
        if markdown:
            body = {"markdown": {"content": text}, "msg_type": MSG_TYPE_MARKDOWN, "msg_seq": msg_seq}
        else:
            body = {"content": text, "msg_type": MSG_TYPE_TEXT, "msg_seq": msg_seq}
            if reply_to:
                body["message_reference"] = {"message_id": reply_to}
        if reply_to:
            body["msg_id"] = reply_to
        kb = _normalize_keyboard(keyboard)
        if kb is not None:
            body["keyboard"] = kb
        return body

    async def send_message(
        self,
        *,
        token,
        chat_id,
        content,
        reply_to=None,
        keyboard=None,
        acct_id="",
        server=None,
    ):
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return False
        if not reply_to:
            reply_to = self._last_msgs(acct_id).get(chat_id)
        text = (content or "")
        if not text.strip() and keyboard is None:
            return True
        chunks = guidance.prepare_outbound_text("qq", text)
        if not chunks:
            chunks = [""] if keyboard is not None else []
        if not chunks:
            return True
        chat_type = self._guess_chat_type(acct_id, chat_id)
        for i, chunk in enumerate(chunks):
            use_reply = reply_to if i == 0 else None
            use_kb = keyboard if i == 0 else None
            body = self._build_text_body(chunk, reply_to=use_reply, keyboard=use_kb)
            if chat_type == "group":
                path = f"/v2/groups/{chat_id}/messages"
            elif chat_type == "guild":
                path = f"/channels/{chat_id}/messages"
                guild_chunk = chunk
                if use_kb is not None and parse_keyboard(use_kb):
                    guild_chunk = join_text_and_keyboard(chunk, use_kb)
                body = {"content": guild_chunk}
                if use_reply:
                    body["msg_id"] = use_reply
            elif chat_type == "dm":
                path = f"/dms/{chat_id}/messages"
                dm_chunk = chunk
                if use_kb is not None and parse_keyboard(use_kb):
                    dm_chunk = join_text_and_keyboard(chunk, use_kb)
                body = {"content": dm_chunk}
                if use_reply:
                    body["msg_id"] = use_reply
            else:
                path = f"/v2/users/{chat_id}/messages"
            await self._api_request(token=token, method="POST", path=path, body=body, server=server)
        return True

    def _media_type_for_method(self, method, filename=""):
        method = (method or "").strip()
        fn = (filename or "").lower()
        if method == "send_photo" or fn.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            return MEDIA_TYPE_IMAGE
        if method == "send_animation":
            return MEDIA_TYPE_IMAGE
        if method == "send_video" or fn.endswith((".mp4", ".webm", ".mov", ".mkv")):
            return MEDIA_TYPE_VIDEO
        if method in ("send_voice", "send_audio") or fn.endswith(_VOICE_EXTS):
            return MEDIA_TYPE_VOICE
        return MEDIA_TYPE_FILE

    async def _upload_media_simple(self, *, token, chat_type, chat_id, file_type, url=None, file_data=None, file_name=None, server=None):
        if chat_type == "group":
            path = f"/v2/groups/{chat_id}/files"
        else:
            path = f"/v2/users/{chat_id}/files"
        body = {"file_type": file_type, "srv_send_msg": False}
        if url:
            body["url"] = url
        elif file_data:
            body["file_data"] = file_data
        if file_type == MEDIA_TYPE_FILE and file_name:
            body["file_name"] = file_name
        return await self._api_request(token=token, method="POST", path=path, body=body, timeout=FILE_UPLOAD_TIMEOUT, server=server)

    async def _http_put(self, url, data, headers=None):
        async with self.session.put(url, data=data, headers=headers or {}, timeout=aiohttp.ClientTimeout(total=300)) as resp:
            class _Resp:
                def __init__(self, status, text):
                    self.status_code = status
                    self.text = text

            text = await resp.text()
            return _Resp(int(resp.status), text[:200])

    async def _chunked_upload(self, *, token, chat_type, chat_id, file_path, file_type, file_name, server=None):
        if chat_type not in ("c2c", "group"):
            raise ValueError(f"unsupported chat_type {chat_type}")
        file_size = os.path.getsize(file_path)
        hashes = await asyncio.get_running_loop().run_in_executor(None, _compute_file_hashes, file_path, file_size)
        base = "/v2/users" if chat_type == "c2c" else "/v2/groups"
        prepare_path = f"{base}/{chat_id}/upload_prepare"
        prepare_body = {
            "file_type": file_type,
            "file_name": file_name,
            "file_size": file_size,
            "md5": hashes["md5"],
            "sha1": hashes["sha1"],
            "md5_10m": hashes["md5_10m"],
        }
        try:
            raw = await self._api_request(
                token=token,
                method="POST",
                path=prepare_path,
                body=prepare_body,
                timeout=FILE_UPLOAD_TIMEOUT,
                server=server,
            )
        except RuntimeError as exc:
            if str(BIZ_CODE_DAILY_LIMIT) in str(exc):
                raise RuntimeError(
                    f"QQ daily upload limit exceeded for {file_name!r} ({_format_size(file_size)})"
                ) from exc
            raise
        src = raw.get("data")
        if src is None:
            src = raw
        else:
            src = ext_dict('qq upload_prepare data', src)
        upload_id = ext_id(src.get("upload_id"), "upload_id")
        if not upload_id:
            raise RuntimeError(f"upload_prepare missing upload_id: {raw}")
        block_size = ext_int(src.get("block_size"), "block_size")
        raw_parts = src.get("parts")
        if raw_parts is None:
            raw_parts = src.get("part_list")
        if raw_parts is None:
            raw_parts = []
        try:
            raw_parts = ext_list("qq upload parts", raw_parts)
        except TypeError as e:
            raise RuntimeError(f"upload_prepare missing parts: {raw}") from e
        if not raw_parts:
            raise RuntimeError(f"upload_prepare missing parts: {raw}")
        for p in raw_parts:
            p = ext_dict('qq upload part', p)
            part_index = p.get("part_index")
            if part_index is None:
                part_index = p.get("index")
            part_index = ext_int(part_index, "part_index")
            presigned = p.get("presigned_url")
            if presigned is None:
                presigned = p.get("url")
            presigned = ext_str(presigned, "presigned_url")
            part_block = ext_int(p.get("block_size"), "block_size")
            if not part_block:
                part_block = block_size
            offset = (part_index - 1) * block_size
            length = min(part_block, file_size - offset)
            data = await asyncio.get_running_loop().run_in_executor(None, _read_file_chunk, file_path, offset, length)
            md5_hex = hashlib.md5(data).hexdigest()
            put_ok = False
            last_err = None
            for attempt in range(3):
                try:
                    resp = await self._http_put(presigned, data, headers={"Content-Length": str(len(data))})
                    if 200 <= int(resp.status_code) < 300:
                        put_ok = True
                        break
                    last_err = RuntimeError(f"COS PUT {resp.status_code}: {resp.text}")
                except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                    last_err = exc
                await asyncio.sleep(1.0 * (2 ** attempt))
            if not put_ok:
                raise RuntimeError(f"part {part_index} upload failed: {last_err}")
            finish_path = f"{base}/{chat_id}/upload_part_finish"
            finish_body = {
                "upload_id": upload_id,
                "part_index": part_index,
                "block_size": length,
                "md5": md5_hex,
            }
            start = time.time()
            while True:
                try:
                    await self._api_request(
                        token=token,
                        method="POST",
                        path=finish_path,
                        body=finish_body,
                        timeout=FILE_UPLOAD_TIMEOUT,
                        server=server,
                    )
                    break
                except RuntimeError as exc:
                    if str(BIZ_CODE_PART_RETRYABLE) not in str(exc):
                        raise
                    if time.time() - start > 120:
                        raise
                    await asyncio.sleep(1.0)
        files_path = f"{base}/{chat_id}/files"
        last_exc = None
        for attempt in range(3):
            try:
                return await self._api_request(
                    token=token,
                    method="POST",
                    path=files_path,
                    body={"upload_id": upload_id},
                    timeout=FILE_UPLOAD_TIMEOUT,
                    server=server,
                )
            except RuntimeError as exc:
                last_exc = exc
                await asyncio.sleep(2.0 * (2 ** attempt))
        raise RuntimeError(f"complete_upload failed: {last_exc}")

    async def send_media(
        self,
        *,
        token,
        chat_id,
        method,
        caption="",
        file_bytes=None,
        media_ref=None,
        filename=None,
        reply_to=None,
        acct_id="",
        server=None,
    ):
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return False
        chat_type = self._guess_chat_type(acct_id, chat_id)
        if not allows_media("qq", chat_type):
            return False
        if chat_type not in ("c2c", "group"):
            return False
        if not reply_to:
            reply_to = self._last_msgs(acct_id).get(chat_id)
        name = (filename or "file").strip() or "file"
        file_type = self._media_type_for_method(method, name)
        if file_bytes is not None:
            kind = guidance.media_kind_from_method(method)
            try:
                guidance.check_media_size("qq", len(file_bytes), kind)
            except guidance.MediaTooLargeError:
                return False
        tmp_path = None
        try:
            if media_ref and (media_ref.startswith("http://") or media_ref.startswith("https://")):
                upload = await self._upload_media_simple(
                    token=token,
                    chat_type=chat_type,
                    chat_id=chat_id,
                    file_type=file_type,
                    url=media_ref,
                    file_name=name if file_type == MEDIA_TYPE_FILE else None,
                    server=server,
                )
            elif file_bytes is not None:
                if len(file_bytes) <= INLINE_UPLOAD_MAX:
                    b64 = base64.b64encode(file_bytes).decode("ascii")
                    upload = await self._upload_media_simple(
                        token=token,
                        chat_type=chat_type,
                        chat_id=chat_id,
                        file_type=file_type,
                        file_data=b64,
                        file_name=name if file_type == MEDIA_TYPE_FILE else None,
                        server=server,
                    )
                else:
                    suffix = os.path.splitext(name)[1] or ".bin"
                    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
                    os.close(fd)
                    with open(tmp_path, "wb") as fh:
                        fh.write(file_bytes)
                    upload = await self._chunked_upload(
                        token=token,
                        chat_type=chat_type,
                        chat_id=chat_id,
                        file_path=tmp_path,
                        file_type=file_type,
                        file_name=name,
                        server=server,
                    )
            else:
                return False
            file_info = upload.get("file_info")
            if file_info is None:
                data = upload.get("data")
                if data is not None:
                    data = ext_dict('qq upload data', data)
                    file_info = data.get("file_info")
            if not file_info:
                return False
            body = {
                "msg_type": MSG_TYPE_MEDIA,
                "media": {"file_info": file_info},
                "msg_seq": self._next_msg_seq(chat_id),
            }
            if caption:
                body["content"] = guidance.prepare_outbound_caption("qq", caption)
            if reply_to:
                body["msg_id"] = reply_to
            path = f"/v2/users/{chat_id}/messages" if chat_type == "c2c" else f"/v2/groups/{chat_id}/messages"
            await self._api_request(token=token, method="POST", path=path, body=body, server=server)
            return True
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError as e:
                    logger.error("unexpected where=qq_temp_unlink path=%s error=%s", tmp_path, e, exc_info=e)

    def _is_voice(self, content_type, filename):
        ct = (content_type or "").strip().lower()
        fn = (filename or "").strip().lower()
        if ct == "voice" or ct.startswith("audio/"):
            return True
        return any(fn.endswith(ext) for ext in _VOICE_EXTS)

    @staticmethod
    def _looks_like_silk(data):
        return data[:6] == b"#!SILK" or data[:2] == b"\x02!" or data[:9] == b"#!SILK_V3"

    async def _convert_to_wav(self, audio_data, filename=""):
        ext = os.path.splitext((filename or ""))[1].lower()
        if not ext:
            if self._looks_like_silk(audio_data):
                ext = ".silk"
            elif audio_data[:4] == b"RIFF":
                ext = ".wav"
            else:
                ext = ".amr"
        fd, src_path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        wav_path = src_path.rsplit(".", 1)[0] + ".wav"
        try:
            with open(src_path, "wb") as fh:
                fh.write(audio_data)
            if ext == ".silk" or self._looks_like_silk(audio_data):
                import pilk

                try:
                    await asyncio.to_thread(pilk.silk_to_wav, src_path, wav_path, rate=16000)
                    if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
                        return wav_path
                except (OSError, RuntimeError, ValueError) as e:
                    logger.warning("qq silk_to_wav primary failed error=%s", e)
                    silk_path = src_path.rsplit(".", 1)[0] + ".silk"
                    import shutil

                    shutil.copy2(src_path, silk_path)
                    try:
                        await asyncio.to_thread(pilk.silk_to_wav, silk_path, wav_path, rate=16000)
                        if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
                            return wav_path
                    finally:
                        try:
                            os.unlink(silk_path)
                        except OSError as e2:
                            logger.error("unexpected where=qq_silk_unlink error=%s", e2, exc_info=e2)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                src_path,
                "-ar",
                "16000",
                "-ac",
                "1",
                wav_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if os.path.isfile(wav_path) and os.path.getsize(wav_path) > 44:
                return wav_path
            return None
        finally:
            try:
                os.unlink(src_path)
            except OSError as e:
                logger.error("unexpected where=qq_wav_src_unlink error=%s", e, exc_info=e)

    def _stt_config(self):
        key = (os.environ.get("QQ_STT_API_KEY") or "").strip()
        if not key:
            return None
        base = (os.environ.get("QQ_STT_BASE_URL") or "https://open.bigmodel.cn/api/coding/paas/v4").strip().rstrip("/")
        model = (os.environ.get("QQ_STT_MODEL") or "glm-asr").strip() or "glm-asr"
        return {"base_url": base, "api_key": key, "model": model}

    async def _call_stt(self, wav_path):
        cfg = self._stt_config()
        if not cfg:
            return None
        form = aiohttp.FormData()
        form.add_field("model", cfg["model"])
        with open(wav_path, "rb") as fh:
            form.add_field("file", fh, filename=os.path.basename(wav_path), content_type="audio/wav")
            async with self.session.post(
                f"{cfg['base_url']}/audio/transcriptions",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                data=form,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if int(resp.status) >= 400:
                    return None
                result = await resp.json(content_type=None)
        result = ext_dict('qq stt response', result)
        choices = result.get("choices")
        if choices is None:
            choices = []
        else:
            choices = ext_list('qq stt choices', choices)
        if choices:
            choice0 = ext_dict("qq stt choice", choices[0])
            message = choice0.get("message")
            if message is None:
                content = ""
            else:
                message = ext_dict("qq stt message", message)
                content = ext_str(message.get("content"), "content").strip()
            if content:
                return content
        text = ext_str(result.get("text"), "text").strip()
        return text or None

    async def _voice_transcript(self, *, token, att):
        asr = ext_str(att.get("asr_refer_text"), "asr_refer_text").strip()
        if asr:
            return asr
        url = ext_str(att.get("voice_wav_url") or att.get("url"), "voice_wav_url").strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url:
            return None
        data = await self.download(token=token, url=url)
        if not data or len(data) < 10:
            return None
        is_wav = bool(ext_str(att.get("voice_wav_url"), "voice_wav_url").strip()) or data[:4] == b"RIFF"
        wav_path = None
        try:
            if is_wav:
                fd, wav_path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)
                with open(wav_path, "wb") as fh:
                    fh.write(data)
            else:
                wav_path = await self._convert_to_wav(data, ext_str(att.get("filename"), "filename"))
            if not wav_path:
                return None
            return await self._call_stt(wav_path)
        finally:
            if wav_path:
                try:
                    os.unlink(wav_path)
                except OSError as e:
                    logger.error("unexpected where=qq_voice_wav_unlink error=%s", e, exc_info=e)

    def _attachments_from_event(self, attachments):
        out = []
        for a in attachments or []:
            a = ext_dict('qq attachment', a)
            url = ext_str(a.get("url"), "url").strip()
            if url.startswith("//"):
                url = "https:" + url
            if not url:
                continue
            filename = ext_str(a.get("filename"), "filename").strip()
            content_type = ext_str(a.get("content_type"), "content_type").strip()
            ct_lower = content_type.lower()
            if ct_lower.startswith("image/webp") or filename.lower().endswith(".webp"):
                att_type = "sticker"
            elif self._is_voice(content_type, filename):
                att_type = "voice"
            else:
                att_type = attachment_type_from_meta(
                    filename=filename, content_type=content_type
                )
            out.append(
                {
                    "type": att_type,
                    "file_id": url,
                    "file_name": filename,
                    "content_type": content_type,
                }
            )
        return out

    async def _deliver(
        self,
        *,
        acct_id,
        chat_id,
        chat_kind,
        user_id,
        msg_id,
        text,
        attachments,
        token,
        raw_attachments=None,
        raw=None,
        reply_to="",
        reply_to_text="",
    ):
        chat_id = (chat_id or "").strip()
        if not chat_id:
            return False
        self._remember_chat_type(acct_id, chat_id, chat_kind)
        if msg_id:
            self._last_msgs(acct_id)[chat_id] = msg_id
        content = (text or "").strip()
        atts = list(attachments or [])
        if raw_attachments:
            for att in raw_attachments:
                att = ext_dict('qq raw attachment', att)
                if not self._is_voice(ext_str(att.get("content_type"), "content_type"), ext_str(att.get("filename"), "filename")):
                    continue
                transcript = await self._voice_transcript(token=token, att=att)
                if transcript:
                    block = f"[Voice] {transcript}"
                    content = f"{content}\n\n{block}".strip() if content else block
        if not content:
            for a in atts:
                if a.get("type") == "sticker":
                    name = ext_str(a.get("file_name"), "file_name").strip() or "a sticker"
                    content = guidance.sticker_injection(name, "", "")
                    break
        force = content.startswith("/start")
        model_id = None if force else (await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=chat_id))
        if not model_id:
            model_id = await self.conductor.ensure_chat_mapping(
                acct_id=acct_id,
                chat_id=chat_id,
                chat_type="qq",
                carrier_user_id=(user_id or "") or None,
                force=force,
            )
        if not model_id:
            return False
        body = {
            "method": "send_message",
            "params": {"text": content},
            "text": content,
            "caption": "",
            "attachments": atts,
            "msg_id": (msg_id or ""),
            "platform": "qq",
            "chat_type": (chat_kind or ""),
        }
        if reply_to:
            body["reply_to"] = reply_to
        if reply_to_text:
            body["reply_to_text"] = reply_to_text
        if raw is not None:
            body["raw"] = raw
        return await self.conductor.deliver_inbound(
            model_id=model_id,
            acct_id=acct_id,
            chat_id=chat_id,
            request_id=(msg_id or ""),
            body=body,
        )

    async def _on_message(self, *, acct_id, token, event_type, d):
        d = ext_dict('qq message event', d)
        msg_id = ext_id(d.get("id"), "id").strip()
        if self._is_seen(acct_id, msg_id):
            return
        content = ext_str(d.get("content"), "content").strip()
        author = d.get("author")
        if author is None:
            author = {}
        else:
            author = ext_dict('qq message author', author)
        raw_atts = d.get("attachments")
        if raw_atts is None:
            raw_atts = []
        else:
            raw_atts = ext_list('qq message attachments', raw_atts)
        atts = self._attachments_from_event(raw_atts)

        if event_type == "C2C_MESSAGE_CREATE":
            user_openid = ext_str(
                author.get("user_openid") or author.get("id"),
                "user_openid",
            ).strip()
            chat_id = user_openid
            text = content
            chat_kind = "c2c"
            user_id = user_openid
        elif event_type == "GROUP_AT_MESSAGE_CREATE":
            chat_id = ext_id(d.get("group_openid"), "group_openid").strip()
            user_id = ext_str(author.get("member_openid") or author.get("id"), "member_openid").strip()
            text = self._strip_at_mention(content)
            chat_kind = "group"
        elif event_type in ("GUILD_MESSAGE_CREATE", "GUILD_AT_MESSAGE_CREATE"):
            chat_id = ext_id(d.get("channel_id"), "channel_id").strip()
            user_id = ext_id(author.get("id"), "id").strip()
            text = self._strip_at_mention(content) if event_type == "GUILD_AT_MESSAGE_CREATE" else content
            chat_kind = "guild"
        elif event_type == "DIRECT_MESSAGE_CREATE":
            chat_id = ext_id(d.get("guild_id"), "guild_id").strip()
            user_id = ext_id(author.get("id"), "id").strip()
            text = content
            chat_kind = "dm"
        else:
            return
        if not chat_id:
            return
        quote_block, quote_reply, quote_atts = self._quoted_context(d)
        if quote_atts:
            for raw_q in quote_atts:
                parsed_list = self._attachments_from_event([raw_q])
                if not parsed_list:
                    continue
                att = parsed_list[0]
                if att.get("type") == "voice" or self._is_voice(
                    ext_str(raw_q.get("content_type"), "content_type"), ext_str(raw_q.get("filename"), "filename")
                ):
                    transcript = await self._voice_transcript(token=token, att=raw_q)
                    if transcript:
                        if not transcript.startswith("[Voice]"):
                            transcript = f"[Voice] {transcript}"
                        if not quote_block or quote_block == "[Quoted message]: (attachment)":
                            quote_block = f"[Quoted message]:\n{transcript}"
                        else:
                            quote_block = f"{quote_block}\n{transcript}"
                    else:
                        atts.append(att)
                        raw_atts = list(raw_atts or []) + [raw_q]
                else:
                    atts.append(att)
                    raw_atts = list(raw_atts or []) + [raw_q]
        reply_to_text = ""
        if quote_block:
            text = f"{quote_block}\n\n{text}".strip() if text else quote_block
            if quote_block.startswith("[Quoted message]:\n"):
                reply_to_text = quote_block.split("\n", 1)[1].strip()
            elif quote_block == "[Quoted message]: (attachment)":
                reply_to_text = "(attachment)"
        location = d.get("location")
        if location is not None:
            location = ext_dict('qq message location', location)
        if location is not None and not text:
            lat = location.get("latitude")
            lon = location.get("longitude")
            if lat is not None and lon is not None:
                lat = ext_float('qq latitude', lat)
                lon = ext_float('qq longitude', lon)
                text = guidance.location_injection(
                    float(lat),
                    float(lon),
                    ext_str(location.get("name"), "name"),
                    ext_str(location.get("address"), "address"),
                )
        if not text and not atts and not raw_atts and location is None:
            return
        raw = d if (not text and not atts and not raw_atts) else None
        ok = await self._deliver(
            acct_id=acct_id,
            chat_id=chat_id,
            chat_kind=chat_kind if chat_kind != "dm" else "dm",
            user_id=user_id,
            msg_id=msg_id,
            text=text,
            attachments=atts,
            token=token,
            raw_attachments=raw_atts,
            raw=raw,
            reply_to=quote_reply,
            reply_to_text=reply_to_text,
        )
        if ok:
            self._mark_seen(acct_id, msg_id)

    async def _ack_interaction(self, *, token, interaction_id, server=None):
        await self._api_request(
            token=token,
            method="PUT",
            path=f"/interactions/{interaction_id}",
            body={"code": 0},
            server=server,
        )

    async def _on_interaction(self, *, acct_id, token, d, server=None):
        d = ext_dict('qq interaction event', d)
        event = _parse_interaction(d)
        interaction_id = event["id"]
        if not interaction_id:
            return
        await self._ack_interaction(token=token, interaction_id=interaction_id, server=server)
        scene = event["scene"]
        if scene == "c2c":
            chat_id = event["user_openid"]
            chat_kind = "c2c"
            user_id = event["user_openid"]
        elif scene == "group":
            chat_id = event["group_openid"]
            chat_kind = "group"
            user_id = event["group_member_openid"] or event["user_openid"]
        else:
            chat_id = event["channel_id"] or event["guild_id"]
            chat_kind = "guild" if event["channel_id"] else "dm"
            user_id = event["user_openid"] or event["group_member_openid"]
        text = event["button_data"] or event["button_id"] or event["button_label"]
        if not chat_id or not text:
            return
        msg_id = f"interaction:{interaction_id}"
        if self._is_seen(acct_id, msg_id):
            return
        ok = await self._deliver(
            acct_id=acct_id,
            chat_id=chat_id,
            chat_kind=chat_kind,
            user_id=user_id,
            msg_id=msg_id,
            text=text,
            attachments=[],
            token=token,
        )
        if ok:
            self._mark_seen(acct_id, msg_id)

    async def _load_runtime_state(self, acct_id):
        state = await self.conductor.db.get_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="qq",
            state_key="poller",
        )
        if state is None:
            return {}
        state = ext_dict('qq runtime state', state)
        chat_types = state.get("chat_type_map")
        if chat_types is None:
            chat_types = {}
        else:
            chat_types = ext_dict('qq chat_type_map', chat_types)
        self._chat_type_map[acct_id] = {
            ext_str(k, "chat_type_key"): ext_str(v, "chat_type_val") for k, v in chat_types.items() if ext_str(k, "chat_type_key").strip() and ext_str(v, "chat_type_val").strip()
        }
        session_id = ext_str(state.get("session_id"), "session_id").strip()
        last_seq = state.get("last_seq")
        if last_seq is not None:
            last_seq = ext_int(last_seq, 'qq last_seq')
        self._ws_sessions[acct_id] = {
            "session_id": session_id or None,
            "last_seq": last_seq,
        }
        return state

    async def _save_runtime_state(self, acct_id):
        sess = self._ws_sessions.get(acct_id) or {}
        await self.conductor.db.set_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="qq",
            state_key="poller",
            state={
                "mode": "qq",
                "chat_type_map": self._chat_types(acct_id),
                "session_id": sess.get("session_id") or "",
                "last_seq": sess.get("last_seq"),
                "updated_at": time.time(),
            },
        )

    async def _send_identify(self, *, ws, token, server=None):
        _, _, access = await self._creds_from_token(token)
        await ws.send_json(
            {
                "op": 2,
                "d": {
                    "token": f"QQBot {access}",
                    "intents": INTENTS,
                    "shard": [0, 1],
                    "properties": {"$os": "linux", "$browser": "station", "$device": "station"},
                },
            }
        )

    async def _send_resume(self, *, ws, token, session_id, last_seq):
        _, _, access = await self._creds_from_token(token)
        await ws.send_json(
            {
                "op": 6,
                "d": {
                    "token": f"QQBot {access}",
                    "session_id": session_id,
                    "seq": last_seq,
                },
            }
        )

    async def poller_loop(self, acct_id):
        acct_id = (acct_id or "").strip()
        if not acct_id:
            return
        acct = await self.conductor.db.get_local_account(acct_id)
        if not acct:
            return
        await self._load_runtime_state(acct_id)
        enc = (acct.get("encrypted_token") or "").strip()
        token, _ = decrypt_if_encrypted(self.conductor.private_key, enc)
        token = (token or "").strip()
        if not token:
            return
        server_raw = ext_str(acct.get("server"), "server").strip()
        if server_raw:
            server_plain, _ = decrypt_if_encrypted(self.conductor.private_key, server_raw)
        else:
            server_plain = ""
        server = (server_plain or "").strip() or None
        sess = self._ws_sessions.setdefault(acct_id, {"session_id": None, "last_seq": None})
        backoff_idx = 0
        quick_disconnects = 0
        quick_disconnect_window = 5.0
        quick_disconnect_limit = 3
        quick_disconnect_hold = 60.0
        while not self.conductor.poller_stop.is_set():
            ws_session = None
            ws = None
            heartbeat_task = None
            stop_hb = asyncio.Event()
            session_started = time.time()
            try:
                app_id, client_secret = parse_qq_credentials(token)
                if not app_id or not client_secret:
                    return
                await self._ensure_access_token(app_id=app_id, client_secret=client_secret)
                gateway_url = await self._get_gateway_url(token=token, server=server)
                ws_session = aiohttp.ClientSession(trust_env=True)
                ws = await ws_session.ws_connect(
                    gateway_url,
                    headers={"User-Agent": "StationQQIO/1.0"},
                    heartbeat=None,
                    timeout=20,
                )
                heartbeat_interval = {"v": 30.0}

                async def _heartbeat():
                    while not stop_hb.is_set() and not self.conductor.poller_stop.is_set():
                        try:
                            await asyncio.wait_for(stop_hb.wait(), timeout=heartbeat_interval["v"])
                            break
                        except asyncio.TimeoutError:
                            pass
                        if ws.closed:
                            break
                        await ws.send_json({"op": 1, "d": sess.get("last_seq")})

                heartbeat_task = asyncio.create_task(_heartbeat())
                while not self.conductor.poller_stop.is_set() and not ws.closed:
                    msg = await ws.receive()
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(msg.data)
                        except json.JSONDecodeError:
                            continue
                        payload = ext_dict('qq gateway payload', payload)
                        op = payload.get("op")
                        t = payload.get("t")
                        s = payload.get("s")
                        d = payload.get("d")
                        if isinstance(s, int) and not isinstance(s, bool):
                            last = sess.get("last_seq")
                            if last is None or s > last:
                                sess["last_seq"] = s
                        if op == 10:
                            d = ext_dict('qq hello d', d)
                            d_data = d
                            interval_ms = ext_int(d_data.get("heartbeat_interval"), "heartbeat_interval", default=30000)
                            heartbeat_interval["v"] = interval_ms / 1000.0 * 0.8
                            if sess.get("session_id") and sess.get("last_seq") is not None:
                                await self._send_resume(
                                    ws=ws,
                                    token=token,
                                    session_id=sess["session_id"],
                                    last_seq=sess["last_seq"],
                                )
                            else:
                                await self._send_identify(ws=ws, token=token, server=server)
                        elif op == 0 and t:
                            if t == "READY" and isinstance(d, dict):
                                sess["session_id"] = d.get("session_id")
                                await self._save_runtime_state(acct_id)
                                backoff_idx = 0
                            elif t == "READY":
                                raise TypeError("qq READY d must be dict")
                            elif t == "RESUMED":
                                backoff_idx = 0
                            elif t in {
                                "C2C_MESSAGE_CREATE",
                                "GROUP_AT_MESSAGE_CREATE",
                                "GUILD_MESSAGE_CREATE",
                                "GUILD_AT_MESSAGE_CREATE",
                                "DIRECT_MESSAGE_CREATE",
                            }:
                                await self._on_message(acct_id=acct_id, token=token, event_type=t, d=d)
                            elif t == "INTERACTION_CREATE":
                                await self._on_interaction(acct_id=acct_id, token=token, d=d, server=server)
                        elif op == 7:
                            await ws.close()
                            break
                        elif op == 9:
                            if not d:
                                sess["session_id"] = None
                                sess["last_seq"] = None
                            await ws.close()
                            break
                    elif msg.type == aiohttp.WSMsgType.CLOSE:
                        code = msg.data
                        if code == 4004:
                            app_id, _ = parse_qq_credentials(token)
                            self._access_by_app.pop(app_id, None)
                        if code in (4006, 4007) or (isinstance(code, int) and 4900 <= code <= 4913):
                            sess["session_id"] = None
                            sess["last_seq"] = None
                        if code in (4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915):
                            await self._save_runtime_state(acct_id)
                            return
                        if code == 4008:
                            await asyncio.sleep(RATE_LIMIT_DELAY)
                        break
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=qq_poller acct_id=%s error=%s", acct_id, e, exc_info=e)
            finally:
                stop_hb.set()
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    try:
                        await heartbeat_task
                    except Exception as e:
                        logger.error("unexpected where=qq_heartbeat_cancel acct_id=%s error=%s", acct_id, e, exc_info=e)
                if ws is not None:
                    try:
                        await ws.close()
                    except Exception as e:
                        logger.error("unexpected where=qq_ws_close acct_id=%s error=%s", acct_id, e, exc_info=e)
                if ws_session is not None:
                    try:
                        await ws_session.close()
                    except Exception as e:
                        logger.error("unexpected where=qq_ws_session_close acct_id=%s error=%s", acct_id, e, exc_info=e)
                await self._save_runtime_state(acct_id)
            if self.conductor.poller_stop.is_set():
                break
            lived = time.time() - session_started
            if lived < quick_disconnect_window:
                quick_disconnects += 1
            else:
                quick_disconnects = 0
                backoff_idx = 0
            if quick_disconnects >= quick_disconnect_limit:
                logger.warning(
                    "qq quick-disconnect circuit open acct_id=%s count=%s hold=%.0fs",
                    acct_id,
                    quick_disconnects,
                    quick_disconnect_hold,
                )
                delay = quick_disconnect_hold
                quick_disconnects = 0
                backoff_idx = len(RECONNECT_BACKOFF) - 1
            else:
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
            if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
                break
            try:
                await asyncio.wait_for(self.conductor.poller_stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass

    async def close(self):
        self._access_by_app.clear()
