import asyncio
import json
import os
import uuid
from typing import Any
from urllib.parse import quote

import aiohttp

from station.conductor.crypto import decrypt_if_encrypted
from station.conductor.platforms import guidance
from station.conductor.util import attachment_type_from_meta, normalize_http_url, ext_str, ext_id
from station import logger
from station.prototypes.boundary import ext_dict, ext_list

_MATRIX_CLIENT_API_PREFIX = "/_matrix/client/v3"
_MATRIX_MEDIA_API_PREFIX = "/_matrix/media/v3"
_MATRIX_CLIENT_MEDIA_PREFIX = "/_matrix/client/v1/media"

class MatrixIO:
    def __init__(self, conductor):
        self.conductor = conductor
        self._e2ee_clients: dict[str, Any] = {}
        self._e2ee_api_sessions: dict[str, Any] = {}
        self._e2ee_crypto_dbs: dict[str, Any] = {}
        self._e2ee_lock = asyncio.Lock()
        self._e2ee_delivery_failed: dict[str, bool] = {}

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.conductor.session

    async def _get_state(self, acct_id: str) -> dict:
        state = await self.conductor.db.get_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="matrix",
            state_key="sync",
        )
        if state is None:
            return {}
        state = ext_dict('matrix runtime state', state)
        return state

    async def _set_state(self, acct_id: str, **updates: str) -> None:
        state = await self._get_state(acct_id)
        next_state = dict(state)
        for key, value in updates.items():
            normalized = (value or "").strip()
            if normalized:
                next_state[key] = normalized
            else:
                next_state.pop(key, None)
        await self.conductor.db.set_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="matrix",
            state_key="sync",
            state=next_state,
        )

    async def _request(
        self,
        *,
        homeserver: str,
        access_token: str,
        method: str,
        path: str,
        params: dict | None = None,
        json_payload: dict | None = None,
        data: Any = None,
        headers: dict | None = None,
        expect_json: bool = True,
    ) -> tuple[int, dict | None, bytes | None]:
        hs = normalize_http_url(homeserver)
        if not hs:
            return 0, None, None
        url = hs + path
        req_headers = {"Authorization": f"Bearer {access_token}"}
        if headers:
            req_headers.update(headers)
        if json_payload is not None:
            req_headers.setdefault("Content-Type", "application/json")
        try:
            async with self.session.request(method, url, params=params, headers=req_headers, json=json_payload, data=data) as resp:
                if expect_json:
                    try:
                        body = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                        body = None
                    if body is not None:
                        body = ext_dict('matrix api json', body)
                    return resp.status, body, None
                raw = await resp.read()
                return resp.status, None, raw
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return 0, None, None

    async def whoami(self, *, acct_id: str, homeserver: str, access_token: str) -> str | None:
        state = await self._get_state(acct_id)
        if (state.get("homeserver") or "").strip() and (state.get("user_id") or "").strip():
            return ext_id(state.get("user_id"), "user_id").strip()
        status, body, _raw = await self._request(
            homeserver=homeserver,
            access_token=access_token,
            method="GET",
            path=_MATRIX_CLIENT_API_PREFIX + "/account/whoami",
        )
        if status != 200 or not body:
            return None
        user_id = ext_id(body.get("user_id"), "user_id").strip()
        if user_id:
            await self._set_state(
                acct_id,
                homeserver=normalize_http_url(homeserver),
                user_id=user_id,
            )
        return user_id

    async def get_turn_server(self, *, homeserver: str, access_token: str) -> dict | None:
        status, body, _raw = await self._request(
            homeserver=homeserver,
            access_token=access_token,
            method="GET",
            path=_MATRIX_CLIENT_API_PREFIX + "/voip/turnServer",
        )
        if status != 200 or not body:
            return None
        uris = body.get("uris")
        username = body.get("username")
        password = body.get("password")
        uris = ext_list('matrix turn uris', uris)
        if not uris:
            return None
        username = ext_str(username, 'matrix turn username')
        if not username.strip():
            return None
        password = ext_str(password, 'matrix turn password')
        if not password.strip():
            return None
        return {"uris": uris, "username": username, "password": password}

    async def sync(self, *, homeserver: str, access_token: str, since: str | None) -> dict | None:
        params: dict[str, Any] = {"timeout": "30000"}
        if since:
            params["since"] = since
        status, body, _raw = await self._request(
            homeserver=homeserver,
            access_token=access_token,
            method="GET",
            path=_MATRIX_CLIENT_API_PREFIX + "/sync",
            params=params,
        )
        if status != 200 or not body:
            return None
        return body

    @staticmethod
    def _is_replace_event(content: dict) -> bool:
        relates = content.get("m.relates_to")
        if relates is None:
            return False
        relates = ext_dict('matrix m.relates_to', relates)
        return ext_str(relates.get("rel_type"), "rel_type").strip() == "m.replace"

    @staticmethod
    def _location_text(content, body=""):
        uri = ext_str(content.get("geo_uri"), "geo_uri").strip()
        if not uri:
            loc = content.get("location")
            if loc is not None:
                loc = ext_dict('matrix location', loc)
                uri = ext_str(loc.get("uri"), "uri").strip()
        if not uri:
            uri = (body or "").strip()
        parsed = guidance.parse_geo_uri(uri)
        if not parsed:
            return ""
        lat, lon = parsed
        return guidance.location_injection(lat, lon, "", "")

    def _event_to_body(self, evt: dict) -> tuple[dict | None, str | None, str | None]:
        evt = ext_dict('matrix event', evt)
        ev_type = ext_str(evt.get("type"), "type").strip()
        sender = ext_str(evt.get("sender"), "sender").strip() or None
        event_id = ext_id(evt.get("event_id"), "event_id").strip() or None
        content = evt.get("content")
        if content is None:
            content = {}
        else:
            content = ext_dict('matrix event content', content)

        if ev_type.startswith("m.call.") or ev_type.startswith("org.matrix.msc3401.call"):
            return {"webrtc": {"type": ev_type, "content": content}}, sender, event_id

        if ev_type == "m.reaction":
            relates = content.get("m.relates_to")
            key = ""
            target = ""
            if relates is not None:
                relates = ext_dict('matrix m.relates_to', relates)
                key = ext_str(relates.get("key"), "key").strip()
                target = ext_id(relates.get("event_id"), "event_id").strip()
            text = guidance.format_reaction_text(None, None)
            if key:
                text = guidance.format_reaction_text([key], None)
            body = {
                "method": "send_message",
                "params": {"text": text},
                "text": text,
                "caption": "",
                "attachments": [],
                "msg_id": f"reaction:{event_id}",
            }
            if target:
                body["reply_to"] = target
            return body, sender, event_id

        is_sticker = ev_type == "m.sticker"
        if ev_type != "m.room.message" and not is_sticker:
            return None, sender, event_id

        if self._is_replace_event(content):
            return None, sender, event_id

        msgtype = ext_str(content.get("msgtype"), "msgtype").strip()
        if is_sticker:
            msgtype = "m.sticker"
        if self._is_notice_msgtype(msgtype):
            return None, sender, event_id

        text = ext_str(content.get("body"), "body").strip()
        attachments: list[dict] = []
        reply_to = ""
        relates = content.get("m.relates_to")
        if relates is not None:
            relates = ext_dict('matrix m.relates_to', relates)
            in_reply = relates.get("m.in_reply_to")
            if in_reply is not None:
                in_reply = ext_dict('matrix m.in_reply_to', in_reply)
                reply_to = ext_id(in_reply.get("event_id"), "event_id").strip()
        if msgtype in ("m.image", "m.file", "m.audio", "m.video", "m.sticker"):
            mxc = ext_str(content.get("url"), "url").strip()
            if not mxc:
                file_obj = content.get("file")
                if file_obj is not None:
                    file_obj = ext_dict('matrix encrypted file', file_obj)
                    mxc = ext_str(file_obj.get("url"), "url").strip()
            info = content.get("info")
            if info is None:
                info = {}
            else:
                info = ext_dict('matrix media info', info)
            mimetype = ext_str(info.get("mimetype"), "mimetype").strip()
            filename = ext_str(content.get("body"), "body").strip()
            if msgtype == "m.sticker":
                att_type = "sticker"
            else:
                att_type = attachment_type_from_meta(filename=filename, content_type=mimetype)
            if content.get("org.matrix.msc3245.voice") is not None:
                att_type = "voice"
            if mxc:
                att = {
                    "type": att_type,
                    "file_id": mxc,
                    "file_name": filename,
                    "content_type": mimetype,
                }
                size = info.get("size")
                if size is not None and size != "":
                    att["file_size"] = int(size)
                attachments.append(att)
        attachments, size_notes = guidance.filter_oversized_attachments("matrix", attachments)
        if size_notes:
            note = "\n".join(size_notes)
            text = f"{text}\n{note}".strip() if text else note
        from station.conductor.platforms.matrix_quote import apply_reply_quote, clear_matrix_filename_body

        text = clear_matrix_filename_body(msgtype, text)
        if msgtype == "m.location" or (not text and ext_str(content.get("msgtype"), "msgtype").strip() == "m.location"):
            loc_text = self._location_text(content, text)
            if loc_text:
                text = loc_text
        if any(a.get("type") == "sticker" for a in attachments):
            name = (text or "").strip() or "a sticker"
            text = guidance.sticker_injection(name, "", "")
        text, reply_to_text = apply_reply_quote(text, reply_to)
        body = {
            "method": "send_message",
            "params": {"text": text},
            "text": text,
            "caption": "",
            "attachments": attachments,
            "msg_id": event_id,
        }
        if reply_to:
            body["reply_to"] = reply_to
        if reply_to_text:
            body["reply_to_text"] = reply_to_text
        if not text and not attachments:
            body["raw"] = evt
        return body, sender, event_id

    @property
    def encryption_enabled(self) -> bool:
        return (os.environ.get("MATRIX_ENCRYPTION") or "").lower() in {"true", "1", "yes", "on"}

    def _matrix_store_dir(self) -> str:
        db_path = self.conductor.config.database.path.strip()
        base = os.path.dirname(os.path.abspath(db_path)) if db_path else os.getcwd()
        return os.path.join(base, "local_conductor_matrix")

    async def _ensure_e2ee_client(self, *, acct_id: str, homeserver: str, access_token: str) -> Any | None:
        acct_id = (acct_id or "").strip()
        if not acct_id:
            return None
        async with self._e2ee_lock:
            existing = self._e2ee_clients.get(acct_id)
            if existing is not None:
                return existing
            from mautrix.api import HTTPAPI
            from mautrix.client import Client
            from mautrix.client.state_store.memory import MemoryStateStore
            from mautrix.client.sync_store.memory import MemorySyncStore
            from mautrix.types import EventType, RoomID, UserID

            hs = normalize_http_url(homeserver)
            if not hs:
                return None

            client_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60), trust_env=True)
            api = HTTPAPI(base_url=hs, token=access_token or "", client_session=client_session)
            crypto_db = None
            try:
                state_store = MemoryStateStore()
                sync_store = MemorySyncStore()
                device_id_env = (os.environ.get("MATRIX_DEVICE_ID") or "").strip() or None
                client = Client(mxid=UserID(""), device_id=device_id_env, api=api, state_store=state_store, sync_store=sync_store)
                resp = await client.whoami()
                resolved_user_id = ext_id(resp.user_id, "user_id").strip()
                resolved_device_id = ext_id(resp.device_id, "device_id").strip() if resp.device_id else ""
                if resolved_user_id:
                    client.mxid = UserID(resolved_user_id)
                if device_id_env:
                    client.device_id = device_id_env
                elif resolved_device_id:
                    client.device_id = resolved_device_id

                from mautrix.crypto import OlmMachine
                from mautrix.crypto.store.asyncpg import PgCryptoStore
                from mautrix.util.async_db import Database

                store_dir = os.path.join(self._matrix_store_dir(), acct_id)
                os.makedirs(store_dir, exist_ok=True)
                crypto_db_path = os.path.join(store_dir, "crypto.db")
                crypto_db = Database.create(f"sqlite:///{crypto_db_path}")
                await crypto_db.start()
                crypto_store = PgCryptoStore(account_id=client.mxid, device_id=client.device_id or "", db=crypto_db)
                await crypto_store.open()
            except Exception as setup_e:
                logger.error("unexpected where=matrix_e2ee_setup acct_id=%s error=%s", acct_id, setup_e, exc_info=setup_e)
                try:
                    await api.session.close()
                except Exception as e:
                    logger.error("unexpected where=matrix_e2ee_setup_close acct_id=%s error=%s", acct_id, e, exc_info=e)
                if crypto_db is not None:
                    try:
                        await crypto_db.stop()
                    except Exception as e:
                        logger.error("unexpected where=matrix_e2ee_setup_db_stop acct_id=%s error=%s", acct_id, e, exc_info=e)
                raise

            class _CryptoStateStore:
                def __init__(self, ss: Any):
                    self._ss = ss

                async def is_encrypted(self, room_id: str) -> bool:
                    return (await self.get_encryption_info(room_id)) is not None

                async def get_encryption_info(self, room_id: str):
                    return await self._ss.get_encryption_info(RoomID(room_id))

                async def find_shared_rooms(self, user_id: str) -> list:
                    return []

            crypto_state = _CryptoStateStore(state_store)
            olm = OlmMachine(client, crypto_store, crypto_state)
            client.crypto = olm

            async def _on_room_message(event: Any) -> None:
                room_id = (event.room_id or "")
                sender = (event.sender or "")
                event_id = (event.event_id or "")
                if self._sender_is_self(client.mxid, sender) or self._sender_is_system(sender):
                    return
                content = event.content
                if not isinstance(content, dict):
                    content = event.content.serialize()
                content = ext_dict("matrix event content", content)
                evt = {
                    "type": (event.type or "m.room.message"),
                    "room_id": room_id,
                    "sender": sender,
                    "event_id": event_id,
                    "content": content,
                }
                body, _, _ = self._event_to_body(evt)
                if body is None:
                    return
                file_content = content.get("file")
                if file_content is not None:
                    file_content = ext_dict('matrix encrypted file', file_content)
                mxc = ext_str(content.get("url"), "url").strip()
                if not mxc and file_content:
                    mxc = ext_str(file_content.get("url"), "url").strip()
                if mxc and file_content is not None and body["attachments"]:
                    from mautrix.types import ContentURI
                    from mautrix.crypto.attachments import decrypt_attachment
                    import tempfile
                    import uuid as _uuid
                    file_bytes = await client.download_media(ContentURI(mxc))
                    if file_bytes is not None:
                        hashes_raw = file_content.get("hashes")
                        if hashes_raw is None:
                            hashes_value = {}
                        else:
                            hashes_raw = ext_dict('matrix encrypted file hashes', hashes_raw)
                            hashes_value = hashes_raw
                        hash_value = hashes_value.get("sha256")
                        key_obj = file_content.get("key")
                        if key_obj is not None:
                            key_obj = ext_dict('matrix encrypted file key', key_obj)
                        key_value = key_obj.get("k") if key_obj is not None else None
                        iv_value = file_content.get("iv")
                        if key_value and hash_value and iv_value:
                            file_bytes = decrypt_attachment(file_bytes, key_value, hash_value, iv_value)
                        else:
                            file_bytes = None
                    if file_bytes is not None:
                        filename = ext_str(content.get("body"), "body").strip()
                        _, ext = os.path.splitext(filename or "")
                        local_path = os.path.join(tempfile.gettempdir(), f"matrix_e2ee_{_uuid.uuid4().hex}{ext}")
                        with open(local_path, "wb") as f:
                            f.write(file_bytes)
                        body["attachments"][0]["local_path"] = local_path
                if not room_id:
                    return
                model_id = await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=room_id)
                if not model_id:
                    model_id = await self.conductor.ensure_chat_mapping(acct_id=acct_id, chat_id=room_id, chat_type="matrix", carrier_user_id=sender or None)
                if not model_id:
                    self._e2ee_delivery_failed[acct_id] = True
                    return
                ok = await self.conductor.deliver_inbound(model_id=model_id, acct_id=acct_id, chat_id=room_id, request_id=event_id, body=body)
                if not ok:
                    self._e2ee_delivery_failed[acct_id] = True

            async def _on_call_event(event: Any) -> None:
                room_id = (event.room_id or "")
                sender = (event.sender or "")
                event_id = (event.event_id or "")
                if self._sender_is_self(client.mxid, sender) or self._sender_is_system(sender):
                    return
                ev_type = (event.type or "")
                content = event.content
                if not isinstance(content, dict):
                    content = event.content.serialize()
                content = ext_dict('matrix call content', content)
                if not room_id or not ev_type:
                    return
                model_id = await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=room_id)
                if not model_id:
                    model_id = await self.conductor.ensure_chat_mapping(acct_id=acct_id, chat_id=room_id, chat_type="matrix", carrier_user_id=sender or None)
                if not model_id:
                    self._e2ee_delivery_failed[acct_id] = True
                    return
                webrtc = {"type": ev_type, "content": content}
                if ev_type == "m.call.invite" or ev_type == "org.matrix.msc3401.call":
                    turn = await self.get_turn_server(homeserver=homeserver, access_token=access_token)
                    if turn is not None:
                        webrtc["turn"] = turn
                ok = await self.conductor.deliver_inbound(
                    model_id=model_id,
                    acct_id=acct_id,
                    chat_id=room_id,
                    request_id=event_id,
                    body={"webrtc": webrtc},
                )
                if not ok:
                    self._e2ee_delivery_failed[acct_id] = True

            client.add_event_handler(EventType.ROOM_MESSAGE, _on_room_message)
            client.add_event_handler(EventType("m.sticker", EventType.Class.MESSAGE), _on_room_message)
            for t in ("m.call.invite", "m.call.answer", "m.call.hangup", "m.call.candidates", "m.call.negotiate", "org.matrix.msc3401.call", "org.matrix.msc3401.call.member"):
                client.add_event_handler(EventType(t), _on_call_event)

            self._e2ee_clients[acct_id] = client
            self._e2ee_api_sessions[acct_id] = api.session
            self._e2ee_crypto_dbs[acct_id] = crypto_db
            return client

    async def send_event(self, *, acct_id: str, homeserver: str, access_token: str, room_id: str, event_type: str, content: dict) -> bool:
        room_id = (room_id or "").strip()
        event_type = (event_type or "").strip()
        if not (room_id and event_type):
            return False
        if self.encryption_enabled:
            client = await self._ensure_e2ee_client(acct_id=acct_id, homeserver=homeserver, access_token=access_token)
            if client is not None:
                from mautrix.types import EventType as MEventType, RoomID
                event_id = await client.send_message_event(RoomID(room_id), MEventType(event_type), content)
                return bool(event_id)
        txn_id = uuid.uuid4().hex
        path = (
            _MATRIX_CLIENT_API_PREFIX
            + "/rooms/"
            + quote(room_id, safe="")
            + "/send/"
            + quote(event_type, safe=".")
            + "/"
            + txn_id
        )
        status, body, _raw = await self._request(
            homeserver=homeserver,
            access_token=access_token,
            method="PUT",
            path=path,
            json_payload=content,
        )
        return status in (200, 201) and bool(body and body.get("event_id"))

    async def send_message(self, *, acct_id: str, homeserver: str, access_token: str, room_id: str, text: str, reply_to: str | None = None, keyboard=None) -> bool:
        from station.conductor.platforms.keyboard import join_text_and_keyboard, parse_keyboard

        if parse_keyboard(keyboard):
            text = join_text_and_keyboard(text, keyboard)
        chunks = guidance.prepare_outbound_text("matrix", (text or ""))
        if not chunks:
            return False
        last_ok = False
        for idx, chunk in enumerate(chunks):
            html = guidance.matrix_html(chunk)
            content: dict[str, Any] = {
                "msgtype": "m.text",
                "body": chunk,
            }
            if html and html != chunk:
                content["format"] = "org.matrix.custom.html"
                content["formatted_body"] = html
            if idx == 0 and reply_to:
                content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to.strip()}}
            ok = await self.send_event(
                acct_id=acct_id,
                homeserver=homeserver,
                access_token=access_token,
                room_id=room_id,
                event_type="m.room.message",
                content=content,
            )
            if not ok:
                return False
            last_ok = True
        return last_ok

    async def upload(self, *, acct_id: str, homeserver: str, access_token: str, file_bytes: bytes, filename: str | None, content_type: str | None) -> str | None:
        try:
            guidance.check_media_size("matrix", len(file_bytes or b""), "document")
        except guidance.MediaTooLargeError:
            return None
        if self.encryption_enabled:
            client = await self._ensure_e2ee_client(acct_id=acct_id, homeserver=homeserver, access_token=access_token)
            if client is not None:
                mxc_url = await client.upload_media(file_bytes, mime_type=content_type, filename=filename, size=len(file_bytes))
                return mxc_url
        hs = normalize_http_url(homeserver)
        if not hs:
            return None
        params = {}
        if filename:
            params["filename"] = filename
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        status, body, _raw = await self._request(
            homeserver=hs,
            access_token=access_token,
            method="POST",
            path=_MATRIX_MEDIA_API_PREFIX + "/upload",
            params=params,
            data=file_bytes,
            headers=headers,
        )
        if status not in (200, 201) or not body:
            return None
        return ext_str(body.get("content_uri"), "content_uri").strip() or None

    async def download_mxc(self, *, acct_id: str, homeserver: str, access_token: str, mxc: str) -> bytes | None:
        if self.encryption_enabled:
            client = await self._ensure_e2ee_client(acct_id=acct_id, homeserver=homeserver, access_token=access_token)
            if client is not None:
                from mautrix.types import ContentURI
                file_bytes = await client.download_media(ContentURI(mxc))
                return file_bytes
        mxc = (mxc or "").strip()
        if not mxc.startswith("mxc://"):
            return None
        parts = mxc[len("mxc://") :].split("/", 1)
        if len(parts) != 2:
            return None
        server_name, media_id = parts[0], parts[1]
        path = _MATRIX_CLIENT_MEDIA_PREFIX + "/download/" + quote(server_name, safe="") + "/" + quote(media_id, safe="")
        status, _body, raw = await self._request(
            homeserver=homeserver,
            access_token=access_token,
            method="GET",
            path=path,
            expect_json=False,
        )
        if status != 200:
            return None
        return raw

    async def send_media_message(
        self,
        *,
        acct_id: str,
        homeserver: str,
        access_token: str,
        room_id: str,
        msgtype: str,
        file_bytes: bytes,
        filename: str,
        content_type: str | None,
        caption: str | None,
        is_voice: bool = False,
        reply_to: str | None = None,
    ) -> bool:
        room_id = (room_id or "").strip()
        if not room_id:
            return False
        filename = (filename or "").strip() or "file"
        msgtype = (msgtype or "").strip() or "m.file"
        content_type = (content_type or "").strip() or "application/octet-stream"
        caption_text = (caption or "").strip() or filename

        if self.encryption_enabled:
            client = await self._ensure_e2ee_client(acct_id=acct_id, homeserver=homeserver, access_token=access_token)
            if client is None:
                return False
            upload_data = file_bytes
            encrypted_file = None
            from mautrix.types import RoomID
            state_store = client.state_store
            room_encrypted = bool(await state_store.is_encrypted(RoomID(room_id)))
            if room_encrypted:
                from mautrix.crypto.attachments import encrypt_attachment
                upload_data, encrypted_file = encrypt_attachment(file_bytes)
            mxc_url = await client.upload_media(upload_data, mime_type=content_type, filename=filename, size=len(upload_data))
            msg_content: dict[str, Any] = {
                "msgtype": msgtype,
                "body": caption_text,
                "info": {"mimetype": content_type, "size": len(file_bytes)},
            }
            if reply_to:
                msg_content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to.strip()}}
            if encrypted_file is not None:
                file_payload = encrypted_file.serialize()
                file_payload["url"] = mxc_url
                msg_content["file"] = file_payload
            else:
                msg_content["url"] = mxc_url
            if is_voice:
                msg_content["org.matrix.msc3245.voice"] = {}
            return await self.send_event(acct_id=acct_id, homeserver=homeserver, access_token=access_token, room_id=room_id, event_type="m.room.message", content=msg_content)

        uri = await self.upload(acct_id=acct_id, homeserver=homeserver, access_token=access_token, file_bytes=file_bytes, filename=filename, content_type=content_type)
        if not uri:
            return False
        msg_content = {
            "msgtype": msgtype,
            "body": caption_text,
            "info": {"mimetype": content_type, "size": len(file_bytes)},
            "url": uri,
        }
        if reply_to:
            msg_content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to.strip()}}
        if is_voice:
            msg_content["org.matrix.msc3245.voice"] = {}
        return await self.send_event(acct_id=acct_id, homeserver=homeserver, access_token=access_token, room_id=room_id, event_type="m.room.message", content=msg_content)

    async def send_typing(self, *, acct_id: str, homeserver: str, access_token: str, room_id: str) -> bool:
        user_id = await self.whoami(acct_id=acct_id, homeserver=homeserver, access_token=access_token)
        if not user_id:
            return False
        status, body, _raw = await self._request(
            homeserver=homeserver,
            access_token=access_token,
            method="PUT",
            path=_MATRIX_CLIENT_API_PREFIX + "/rooms/" + quote(room_id, safe="") + "/typing/" + quote(user_id, safe=""),
            json_payload={"typing": True, "timeout": 30000},
        )
        return status in (200, 201) and (body is not None)

    def _sender_is_self(self, user_id, sender):
        own = (user_id or "").strip().lower()
        if not own:
            return True
        return (sender or "").strip().lower() == own

    def _sender_is_system(self, sender):
        s = (sender or "").strip()
        if not s:
            return True
        s = s.lstrip("@")
        local = s.split(":", 1)[0].strip()
        if not local or local.startswith("_"):
            return True
        lower = local.lower()
        for p in (
            "bridge_",
            "slack_",
            "discord_",
            "telegram_",
            "whatsapp_",
            "irc_",
            "hookshot_",
            "facebook_",
            "signal_",
        ):
            if lower.startswith(p):
                return True
        return False

    def _is_notice_msgtype(self, msgtype: str) -> bool:
        mt = (msgtype or "").strip().lower()
        return mt == "m.notice" or mt == "m.server_notice" or mt.endswith(".notice")

    async def join_room(self, *, homeserver: str, access_token: str, room_id: str) -> bool:
        room_id = (room_id or "").strip()
        if not room_id:
            return False
        status, _body, _raw = await self._request(
            homeserver=homeserver,
            access_token=access_token,
            method="POST",
            path=_MATRIX_CLIENT_API_PREFIX + "/join/" + quote(room_id, safe=""),
            json_payload={},
        )
        return status in (200, 201)

    async def _join_invites(self, *, homeserver: str, access_token: str, invite: dict) -> None:
        room_ids = [ext_str(rid, "room_id") for rid in (invite or {}) if ext_str(rid, "room_id").strip()]
        if not room_ids:
            return
        sem = asyncio.Semaphore(4)

        async def _one(rid: str) -> None:
            async with sem:
                await self.join_room(homeserver=homeserver, access_token=access_token, room_id=rid)

        await asyncio.gather(*(_one(rid) for rid in room_ids), return_exceptions=True)

    async def poller_loop(self, acct_id: str) -> None:
        backoff = 1.0
        while not self.conductor.poller_stop.is_set():
            try:
                acct = await self.conductor.db.get_local_account(acct_id)
                if not acct:
                    return
                enc = (acct.get("encrypted_token") or "").strip()
                token, _ = decrypt_if_encrypted(self.conductor.private_key, enc)
                if not token:
                    await asyncio.sleep(2)
                    continue
                server_plain, _ = decrypt_if_encrypted(
                    self.conductor.private_key,
                    ext_str(acct.get("server"), "server").strip(),
                )
                homeserver = normalize_http_url((server_plain or "").strip())
                if not homeserver:
                    await asyncio.sleep(2)
                    continue

                if self.encryption_enabled:
                    await self._e2ee_poller_loop(acct_id=acct_id, homeserver=homeserver, access_token=token)
                    return

                user_id = await self.whoami(acct_id=acct_id, homeserver=homeserver, access_token=token)
                if not user_id:
                    await asyncio.sleep(min(backoff, 10.0))
                    backoff = min(backoff * 1.5, 30.0)
                    continue
                state = await self._get_state(acct_id)
                next_batch = (state.get("next_batch") or "").strip() or None
                sync = await self.sync(homeserver=homeserver, access_token=token, since=next_batch)
                if not sync:
                    await asyncio.sleep(min(backoff, 10.0))
                    backoff = min(backoff * 1.5, 30.0)
                    continue
                backoff = 1.0
                nb = ext_str(sync.get("next_batch"), "next_batch").strip()

                rooms = sync.get("rooms")
                if rooms is None:
                    rooms = {}
                else:
                    rooms = ext_dict('matrix sync rooms', rooms)
                invite = rooms.get("invite")
                if invite is None:
                    invite = {}
                else:
                    invite = ext_dict('matrix sync invite', invite)
                await self._join_invites(homeserver=homeserver, access_token=token, invite=invite)
                join = rooms.get("join")
                if join is None:
                    join = {}
                else:
                    join = ext_dict('matrix sync join', join)
                delivery_failed = False
                for room_id, room_data in join.items():
                    if delivery_failed:
                        break
                    room_data = ext_dict('matrix room_data', room_data)
                    timeline = room_data.get("timeline")
                    if timeline is None:
                        continue
                    timeline = ext_dict('matrix timeline', timeline)
                    events = timeline.get("events")
                    if events is None:
                        continue
                    events = ext_list('matrix timeline events', events)
                    for evt in events:
                        evt = ext_dict('matrix event', evt)
                        body, sender, event_id = self._event_to_body(evt)
                        if body is None:
                            continue
                        if self._sender_is_self(user_id, sender) or self._sender_is_system(sender):
                            continue
                        carrier_user_id = sender
                        force = ext_str(body.get("text"), "text").strip().startswith("/start")
                        model_id = None if force else (await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=room_id))
                        if not model_id:
                            model_id = await self.conductor.ensure_chat_mapping(
                                acct_id=acct_id,
                                chat_id=room_id,
                                chat_type="matrix",
                                carrier_user_id=carrier_user_id,
                                force=force,
                            )
                        if not model_id:
                            delivery_failed = True
                            break
                        msg_body = {"webrtc": body.get("webrtc")} if "webrtc" in body else body
                        webrtc = msg_body.get("webrtc")
                        if webrtc is not None:
                            webrtc = ext_dict('matrix webrtc body', webrtc)
                            if webrtc.get("type") == "m.call.invite" and webrtc.get("turn") is None:
                                turn = await self.get_turn_server(homeserver=homeserver, access_token=token)
                                if turn is not None:
                                    webrtc["turn"] = turn
                            elif webrtc.get("turn") is not None:
                                webrtc["turn"] = ext_dict("matrix webrtc turn", webrtc.get("turn"))
                        ok = await self.conductor.deliver_inbound(
                            model_id=model_id,
                            acct_id=acct_id,
                            chat_id=room_id,
                            request_id=(event_id or ""),
                            body=msg_body,
                        )
                        if not ok:
                            delivery_failed = True
                            break
                if delivery_failed:
                    await asyncio.sleep(2)
                    continue
                if nb:
                    await self._set_state(acct_id, homeserver=homeserver, next_batch=nb)
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=matrix_poller acct_id=%s error=%s", acct_id, e, exc_info=e)
                await asyncio.sleep(min(backoff, 10.0))
                backoff = min(backoff * 1.5, 30.0)
                continue

    async def _e2ee_poller_loop(self, *, acct_id: str, homeserver: str, access_token: str) -> None:
        client = await self._ensure_e2ee_client(acct_id=acct_id, homeserver=homeserver, access_token=access_token)
        if client is None:
            await asyncio.sleep(2)
            return

        while not self.conductor.poller_stop.is_set():
            try:
                self._e2ee_delivery_failed[acct_id] = False
                sync_data = await client.sync(timeout=30000, full_state=False)
                if sync_data is None:
                    await asyncio.sleep(0.1)
                    continue
                sync_data = ext_dict('matrix e2ee sync', sync_data)
                tasks = client.handle_sync(sync_data)
                if tasks:
                    await asyncio.gather(*tasks)
                nb = sync_data.get("next_batch")
                if nb and not self._e2ee_delivery_failed.get(acct_id):
                    await self._set_state(acct_id, homeserver=homeserver, next_batch=nb)
                    await client.sync_store.put_next_batch(nb)
                elif self._e2ee_delivery_failed.get(acct_id):
                    await asyncio.sleep(2)
                    continue
                if client.crypto is not None:
                    await client.crypto.share_keys()
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=matrix_e2ee_poller acct_id=%s error=%s", acct_id, e, exc_info=e)
                await asyncio.sleep(2)
                continue

    async def close(self) -> None:
        async with self._e2ee_lock:
            sessions = list(self._e2ee_api_sessions.items())
            dbs = list(self._e2ee_crypto_dbs.items())
            self._e2ee_clients.clear()
            self._e2ee_api_sessions.clear()
            self._e2ee_crypto_dbs.clear()
        for _acct_id, sess in sessions:
            try:
                await sess.close()
            except Exception as e:
                logger.error("unexpected where=matrix_session_close acct_id=%s error=%s", _acct_id, e, exc_info=e)
        for _acct_id, db in dbs:
            try:
                await db.stop()
            except Exception as e:
                logger.error("unexpected where=matrix_crypto_db_stop acct_id=%s error=%s", _acct_id, e, exc_info=e)
