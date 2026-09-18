import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import shutil
import tempfile
from collections import OrderedDict

import aiohttp
from aiohttp import web

from station.conductor.crypto import decrypt_if_encrypted
from station.conductor.platforms.base import LocalPlatformAdapter
from station.conductor.platforms import guidance
from station.conductor.platforms.policy import media_ref_action
from station.conductor.util import constant_time_equal, err, ext_str, ext_id, ext_int
from station import logger
from station.prototypes.boundary import ext_dict, ext_float, ext_list, ext_require

DEFAULT_API_VERSION = "v21.0"
GRAPH_API_BASE = "https://graph.facebook.com"
WEBHOOK_MAX_BODY_BYTES = 3 * 1024 * 1024
WAMID_DEDUP_CACHE_SIZE = 5000
_FFMPEG_PATH = shutil.which("ffmpeg")

_MEDIA_SIZE_LIMITS = {
    "image": guidance.MAX_WHATSAPP_IMAGE_BYTES,
    "video": guidance.MAX_WHATSAPP_VIDEO_BYTES,
    "audio": guidance.MAX_WHATSAPP_AUDIO_BYTES,
    "document": guidance.MAX_WHATSAPP_DOCUMENT_BYTES,
    "sticker": guidance.MAX_WHATSAPP_STICKER_BYTES,
}

_DEFAULT_MIME = {
    "image": "image/jpeg",
    "video": "video/mp4",
    "audio": "audio/mpeg",
    "document": "application/octet-stream",
    "sticker": "image/webp",
}

_METHOD_MEDIA_TYPE = {
    "send_photo": "image",
    "send_video": "video",
    "send_audio": "audio",
    "send_voice": "audio",
    "send_document": "document",
    "send_animation": "video",
    "send_sticker": "sticker",
}

_MIME_EXT = {
    "audio/ogg": ".ogg",
    "audio/x-opus+ogg": ".ogg",
    "audio/opus": ".ogg",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "image/jpeg": ".jpg",
}

def verify_token_for_acct_id(acct_id):
    raw = (acct_id or "").strip().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def guess_mime(media_type, filename="", explicit=""):
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    filename = (filename or "").strip()
    if filename:
        guessed, _ = mimetypes.guess_type(filename)
        if guessed:
            return guessed
    return _DEFAULT_MIME.get(media_type, "application/octet-stream")

def ext_for_mime(mime):
    mime = (mime or "").split(";")[0].strip().lower()
    if not mime:
        return None
    if mime in _MIME_EXT:
        return _MIME_EXT[mime]
    return mimetypes.guess_extension(mime) or None

async def _read_limited_request_body(request, max_bytes):
    try:
        body = await request.content.readexactly(max_bytes + 1)
    except asyncio.IncompleteReadError as exc:
        body = exc.partial
    if len(body) > max_bytes:
        raise ValueError("payload too large")
    return body

class WhatsAppCloudIO:
    def __init__(self, conductor):
        self.conductor = conductor
        self._api_session = None
        self._seen_wamids = OrderedDict()
        self._last_inbound_wamid = OrderedDict()
        self._warned_no_ffmpeg = False

    def _api_base(self):
        raw = self.conductor.config.whatsapp_cloud_api_url.strip()
        if not raw:
            raw = (os.environ.get("WHATSAPP_CLOUD_API_URL") or "").strip()
        return raw.rstrip("/") if raw else GRAPH_API_BASE

    async def _ensure_session(self):
        if self._api_session is not None and not self._api_session.closed:
            return
        timeout = aiohttp.ClientTimeout(total=120)
        self._api_session = aiohttp.ClientSession(timeout=timeout, trust_env=True)

    @property
    def session(self):
        if self._api_session is None or self._api_session.closed:
            raise RuntimeError("whatsapp_cloud session not initialized")
        return self._api_session

    async def close(self):
        if self._api_session is not None and not self._api_session.closed:
            await self._api_session.close()
        self._api_session = None

    def _creds_for_acct(self, acct, token):
        phone_number_id = ext_str(
            acct.get("username") or acct.get("acct_username"),
            "username",
        ).strip()
        app_secret_raw = ext_str(acct.get("server"), "server").strip()
        if app_secret_raw:
            app_secret, _ = decrypt_if_encrypted(self.conductor.private_key, app_secret_raw)
        else:
            app_secret = ""
        app_secret = (app_secret or "").strip()
        acct_id = ext_str(
            acct.get("acct_id") or acct.get("id"),
            "acct_id",
        ).strip()
        return {
            "phone_number_id": phone_number_id,
            "access_token": (token or "").strip(),
            "app_secret": app_secret,
            "verify_token": verify_token_for_acct_id(acct_id),
            "api_version": DEFAULT_API_VERSION,
        }

    def _graph_url(self, *, phone_number_id, api_version, path):
        base = self._api_base()
        version = (api_version or DEFAULT_API_VERSION).strip() or DEFAULT_API_VERSION
        phone = (phone_number_id or "").strip()
        path = (path or "").strip().lstrip("/")
        return f"{base}/{version}/{phone}/{path}"

    def _remember_wamid(self, chat_id, wamid):
        chat_id = (chat_id or "").strip()
        wamid = (wamid or "").strip()
        if not chat_id or not wamid:
            return
        self._last_inbound_wamid[chat_id] = wamid
        while len(self._last_inbound_wamid) > WAMID_DEDUP_CACHE_SIZE:
            self._last_inbound_wamid.popitem(last=False)

    def _is_wamid_seen(self, wamid):
        wamid = (wamid or "").strip()
        if not wamid:
            return False
        return wamid in self._seen_wamids

    def _mark_wamid_seen(self, wamid):
        wamid = (wamid or "").strip()
        if not wamid:
            return
        if wamid in self._seen_wamids:
            self._seen_wamids.move_to_end(wamid)
            return
        self._seen_wamids[wamid] = True
        while len(self._seen_wamids) > WAMID_DEDUP_CACHE_SIZE:
            self._seen_wamids.popitem(last=False)

    def verify_signature(self, *, app_secret, raw_body, header):
        secret = (app_secret or "").strip()
        header = (header or "").strip()
        if not secret or not header.startswith("sha256="):
            return False
        expected_hex = header[len("sha256=") :].strip()
        if not expected_hex:
            return False
        computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed.lower().encode(), expected_hex.lower().encode())

    async def _token_for_acct(self, acct_id):
        acct = await self.conductor.db.get_local_account(acct_id)
        if not acct:
            return None, None
        enc = (acct.get("encrypted_token") or "").strip()
        token, _ = decrypt_if_encrypted(self.conductor.private_key, enc)
        return acct, (token or "").strip()

    async def _post_messages(self, *, creds, payload):
        await self._ensure_session()
        url = self._graph_url(
            phone_number_id=creds["phone_number_id"],
            api_version=creds["api_version"],
            path="messages",
        )
        headers = {
            "Authorization": f"Bearer {creds['access_token']}",
            "Content-Type": "application/json",
        }
        async with self.session.post(url, headers=headers, json=payload) as resp:
            try:
                body = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                raw = await resp.text()
                body = {"raw": (raw or "")[:500]}
            return resp.status, body

    async def send_typing(self, *, acct, token, chat_id):
        creds = self._creds_for_acct(acct, token)
        if not creds["phone_number_id"] or not creds["access_token"]:
            return False
        wamid = self._last_inbound_wamid.get((chat_id or "").strip())
        if not wamid:
            return True
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": wamid,
            "typing_indicator": {"type": "text"},
        }
        status, _ = await self._post_messages(creds=creds, payload=payload)
        return status == 200

    async def send_message(self, *, acct, token, chat_id, text, reply_to=None, keyboard=None):
        from station.conductor.platforms.keyboard import join_text_and_keyboard, parse_keyboard, whatsapp_interactive

        text = (text or "").strip()
        kb = parse_keyboard(keyboard)
        creds = self._creds_for_acct(acct, token)
        if not creds["phone_number_id"] or not creds["access_token"]:
            return False
        chunks = guidance.prepare_outbound_text("whatsapp_cloud", text)
        if not chunks:
            if not kb:
                return False
            chunks = [""]
        last_ok = False
        for idx, chunk in enumerate(chunks):
            payload = {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": chat_id.strip(),
                "type": "text",
                "text": {"body": chunk, "preview_url": True},
            }
            if idx == 0 and kb:
                interactive = whatsapp_interactive(chunk, keyboard)
                if interactive is not None:
                    payload = {
                        "messaging_product": "whatsapp",
                        "recipient_type": "individual",
                        "to": chat_id.strip(),
                        "type": "interactive",
                        "interactive": interactive,
                    }
                else:
                    joined = join_text_and_keyboard(chunk, keyboard)
                    payload["text"] = {"body": joined, "preview_url": True}
            if reply_to and idx == 0:
                payload["context"] = {"message_id": reply_to.strip()}
            status, body = await self._post_messages(creds=creds, payload=payload)
            if status != 200:
                return False
            last_ok = True
            wamid = ""
            body = ext_dict('whatsapp_cloud send body', body)
            msgs = body.get("messages")
            if msgs is not None:
                msgs = ext_list('whatsapp_cloud messages', msgs)
                if msgs:
                    first = ext_dict("whatsapp_cloud message item", msgs[0])
                    wamid = ext_id(first.get("id"), "id").strip()
            if wamid:
                from station.conductor.platforms import whatsapp_replyindex

                whatsapp_replyindex.record_reply_text(chat_id, wamid, chunk)
        return last_ok

    async def _convert_to_opus(self, src_path):
        if not _FFMPEG_PATH:
            if not self._warned_no_ffmpeg:
                self._warned_no_ffmpeg = True
            return None
        out_path = src_path.rsplit(".", 1)[0] + ".ogg"
        try:
            proc = await asyncio.create_subprocess_exec(
                _FFMPEG_PATH,
                "-y",
                "-i",
                src_path,
                "-c:a",
                "libopus",
                "-b:a",
                "32k",
                "-vbr",
                "on",
                "-application",
                "voip",
                out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.communicate()
            if proc.returncode != 0 or not os.path.exists(out_path):
                return None
            return out_path
        except OSError:
            return None

    async def _upload_media(self, *, creds, file_bytes, mime_type, media_type, filename=""):
        await self._ensure_session()
        url = self._graph_url(
            phone_number_id=creds["phone_number_id"],
            api_version=creds["api_version"],
            path="media",
        )
        filename = (filename or "").strip() or f"file{ext_for_mime(mime_type) or ''}"
        form = aiohttp.FormData()
        form.add_field("messaging_product", "whatsapp")
        form.add_field("type", mime_type)
        form.add_field(
            "file",
            file_bytes,
            filename=filename,
            content_type=mime_type,
        )
        headers = {"Authorization": f"Bearer {creds['access_token']}"}
        async with self.session.post(url, headers=headers, data=form) as resp:
            if resp.status != 200:
                return None
            body = await resp.json(content_type=None)
        try:
            body = ext_dict("whatsapp_cloud media body", body)
        except TypeError:
            return None
        return ext_id(body.get("id"), "id").strip() or None

    async def send_media(
        self,
        *,
        acct,
        token,
        chat_id,
        method,
        payload,
        file_bytes,
        caption,
        reply_to,
    ):
        media_type = _METHOD_MEDIA_TYPE.get(method)
        if not media_type:
            return False
        creds = self._creds_for_acct(acct, token)
        if not creds["phone_number_id"] or not creds["access_token"]:
            return False
        limit = _MEDIA_SIZE_LIMITS.get(media_type, 16 * 1024 * 1024)
        media_id = None
        media_link = None
        filename = ext_str(payload.get("file_name"), "file_name").strip()
        mime = guess_mime(
            media_type,
            filename=filename,
            explicit=ext_str(payload.get("content_type"), "content_type").strip(),
        )
        ref = ext_str(
            payload.get("file_id")
            or payload.get(media_type)
            or payload.get("photo")
            or payload.get("video")
            or payload.get("audio")
            or payload.get("document")
            or payload.get("voice")
            or payload.get("sticker"),
            "media_ref",
        ).strip()

        send_bytes = file_bytes
        if method == "send_voice" and send_bytes is not None:
            src_path = None
            opus_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
                    tmp.write(send_bytes)
                    src_path = tmp.name
                opus_path = await self._convert_to_opus(src_path)
                if opus_path:
                    with open(opus_path, "rb") as fh:
                        send_bytes = fh.read()
                    mime = "audio/ogg; codecs=opus"
                    filename = (filename.rsplit(".", 1)[0] if filename else "voice") + ".ogg"
            finally:
                for path in (src_path, opus_path):
                    if path:
                        try:
                            os.unlink(path)
                        except OSError as e:
                            logger.error("unexpected where=whatsapp_cloud_unlink path=%s error=%s", path, e, exc_info=e)

        if send_bytes is not None:
            if len(send_bytes) > limit:
                raise guidance.MediaTooLargeError(
                    "whatsapp_cloud",
                    media_type,
                    len(send_bytes),
                    limit,
                )
            media_id = await self._upload_media(
                creds=creds,
                file_bytes=send_bytes,
                mime_type=mime,
                media_type=media_type,
                filename=filename,
            )
            if not media_id:
                return False
        else:
            action = media_ref_action("whatsapp_cloud", ref)
            if action == "reuse" and ref.startswith(("http://", "https://")):
                media_link = ref
            elif action == "reuse":
                media_id = ref
            else:
                return False

        media_obj = {}
        if media_id:
            media_obj["id"] = media_id
        else:
            media_obj["link"] = media_link
        if caption and media_type in ("image", "video", "document"):
            media_obj["caption"] = guidance.prepare_outbound_caption("whatsapp_cloud", caption)
        if media_type == "document" and filename:
            media_obj["filename"] = filename
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": chat_id.strip(),
            "type": media_type,
            media_type: media_obj,
        }
        if reply_to:
            body["context"] = {"message_id": reply_to.strip()}
        status, _ = await self._post_messages(creds=creds, payload=body)
        return status == 200

    async def download_file(self, *, acct, token, file_id):
        creds = self._creds_for_acct(acct, token)
        if not creds["access_token"] or not file_id:
            return None
        await self._ensure_session()
        base = self._api_base()
        version = creds["api_version"]
        meta_url = f"{base}/{version}/{file_id.strip()}"
        headers = {"Authorization": f"Bearer {creds['access_token']}"}
        async with self.session.get(meta_url, headers=headers) as resp:
            if resp.status != 200:
                return None
            meta = await resp.json(content_type=None)
        meta = ext_dict('whatsapp_cloud media meta', meta)
        download_url = ext_str(meta.get("url"), "url").strip()
        if not download_url:
            return None
        async with self.session.get(download_url, headers=headers) as resp:
            if resp.status != 200:
                return None
            return await resp.read()

    def parse_cloud_message(self, raw_message, contacts_by_waid=None):
        raw_message = ext_dict('whatsapp_cloud message', raw_message)
        if contacts_by_waid is None:
            contacts_by_waid = {}
        else:
            contacts_by_waid = ext_dict('whatsapp_cloud contacts_by_waid', contacts_by_waid)
        msg_type = ext_str(raw_message.get("type"), "type", default="text").lower()
        sender_id = ext_id(raw_message.get("from"), "from").strip()
        if not sender_id:
            return None
        group_id = ext_id(raw_message.get("group_id"), "group_id").strip()
        chat_id = group_id or sender_id
        wamid = ext_id(raw_message.get("id"), "id").strip()
        text = ""
        caption = ""
        attachments = []
        raw = None
        if msg_type == "text":
            text_obj = raw_message.get("text")
            text_obj = ext_dict('whatsapp_cloud text', text_obj)
            text = ext_str(text_obj.get("body"), "body").strip()
        elif msg_type in ("button", "interactive"):
            if msg_type == "button":
                button_obj = raw_message.get("button")
                button_obj = ext_dict('whatsapp_cloud button', button_obj)
                text = ext_str(button_obj.get("payload"), "payload").strip() or ext_str(button_obj.get("text"), "text").strip()
            else:
                inter = raw_message.get("interactive")
                inter = ext_dict('whatsapp_cloud interactive', inter)
                inner = inter.get("button_reply") or inter.get("list_reply")
                inner = ext_dict('whatsapp_cloud interactive reply', inner)
                text = ext_str(inner.get("id"), "id").strip() or ext_str(inner.get("title"), "title").strip()
        elif msg_type in ("image", "video", "audio", "voice", "document", "sticker"):
            inner = raw_message.get(msg_type)
            inner = ext_dict('inner', inner)
            caption = ext_str(inner.get("caption"), "caption").strip()
            media_id = ext_id(inner.get("id"), "id").strip()
            att_type = {
                "image": "photo",
                "video": "video",
                "audio": "audio",
                "voice": "voice",
                "document": "document",
                "sticker": "sticker",
            }.get(msg_type, "document")
            if msg_type == "audio" and bool(inner.get("voice")):
                att_type = "voice"
            if media_id:
                att = {
                    "type": att_type,
                    "file_id": media_id,
                    "file_name": ext_str(inner.get("filename"), "filename"),
                    "content_type": ext_str(inner.get("mime_type"), "mime_type"),
                }
                fs = inner.get("file_size")
                if fs is not None:
                    att["file_size"] = ext_int(fs, "file_size")
                attachments.append(att)
            if msg_type == "document" and not caption:
                fname = ext_str(inner.get("filename"), "filename").strip()
                if fname:
                    caption = f"[Document: {fname}]"
            if msg_type == "sticker" and not text and not caption:
                text = guidance.sticker_injection("a sticker", "", "")
        elif msg_type == "location":
            inner = raw_message.get("location")
            inner = ext_dict('whatsapp_cloud location', inner)
            lat = inner.get("latitude")
            lon = inner.get("longitude")
            if lat is not None and lon is not None:
                lat = ext_float('whatsapp_cloud latitude', lat)
                lon = ext_float('whatsapp_cloud longitude', lon)
                text = guidance.location_injection(
                    float(lat),
                    float(lon),
                    ext_str(inner.get("name"), "name"),
                    ext_str(inner.get("address"), "address"),
                )
            else:
                raw = raw_message
        elif msg_type == "contacts":
            contacts = raw_message.get("contacts")
            if contacts is None:
                contacts = []
            else:
                contacts = ext_list('whatsapp_cloud contacts', contacts)
            parts = []
            for c in contacts:
                c = ext_dict('whatsapp_cloud contact', c)
                name = ""
                nm = c.get("name")
                if nm is not None:
                    nm = ext_dict('whatsapp_cloud contact name', nm)
                    name = ext_str(nm.get("formatted_name"), "formatted_name").strip()
                    if not name:
                        name = f"{ext_str(nm.get('first_name'), 'first_name').strip()} {ext_str(nm.get('last_name'), 'last_name').strip()}".strip()
                phones = []
                plist = c.get("phones")
                if plist is None:
                    plist = []
                else:
                    plist = ext_list('whatsapp_cloud contact phones', plist)
                for p in plist:
                    p = ext_dict('whatsapp_cloud contact phone', p)
                    ph = ext_str(p.get("phone"), "phone").strip()
                    if ph:
                        phones.append(ph)
                inj = guidance.contact_injection(name, phones)
                if inj:
                    parts.append(inj)
            text = "\n\n".join(parts)
            if not text:
                raw = raw_message
        elif msg_type == "unsupported":
            raw = raw_message
        elif msg_type == "reaction":
            reaction = raw_message.get("reaction")
            if reaction is None:
                raise TypeError("whatsapp_cloud reaction required")
            reaction = ext_dict('whatsapp_cloud reaction', reaction)
            emoji = ext_str(reaction.get("emoji"), "emoji").strip()
            react_to = ext_id(reaction.get("message_id"), "message_id").strip()
            text = guidance.format_reaction_text(None, None)
            if emoji:
                text = guidance.format_reaction_text([emoji], None)
            return {
                "msg_id": f"reaction:{wamid}",
                "chat_id": chat_id,
                "carrier_user_id": sender_id,
                "text": text,
                "caption": "",
                "attachments": [],
                "reply_to": react_to,
                "sender_name": ext_str(contacts_by_waid.get(sender_id), "sender_name"),
                "msg_type": msg_type,
                "raw": None,
            }
        else:
            raw = raw_message
        reply_to = ""
        context = raw_message.get("context")
        if context is not None:
            context = ext_dict('whatsapp_cloud context', context)
            reply_to = ext_id(context.get("id"), "id").strip()
        from station.conductor.platforms import whatsapp_replyindex

        record_text = text or caption
        if wamid and record_text:
            whatsapp_replyindex.record_reply_text(chat_id, wamid, record_text)
        reply_to_text = ""
        if reply_to:
            reply_to_text = whatsapp_replyindex.lookup_reply_text(chat_id, reply_to)
            if reply_to_text:
                text = guidance.inject_reply_context(text, reply_to_text)
        if attachments:
            attachments, notes = guidance.filter_oversized_attachments("whatsapp_cloud", attachments)
            if notes:
                joined = "\n".join(notes)
                text = f"{text}\n{joined}".strip() if text else joined
        return {
            "msg_id": wamid,
            "chat_id": chat_id,
            "carrier_user_id": sender_id,
            "text": text,
            "caption": caption,
            "attachments": attachments,
            "reply_to": reply_to,
            "reply_to_text": reply_to_text,
            "sender_name": ext_str(contacts_by_waid.get(sender_id), "sender_name"),
            "msg_type": msg_type,
            "raw": raw,
        }

    async def process_payload(self, *, acct_id, payload):
        payload = ext_dict('whatsapp_cloud webhook payload', payload)
        if payload.get("object") != "whatsapp_business_account":
            return True
        delivery_failed = False
        entry_list = payload.get("entry")
        if entry_list is None:
            entry_list = []
        else:
            entry_list = ext_list('whatsapp_cloud entry', entry_list)
        for entry in entry_list:
            entry = ext_dict('whatsapp_cloud entry item', entry)
            changes = entry.get("changes")
            if changes is None:
                continue
            changes = ext_list('whatsapp_cloud changes', changes)
            for change in changes:
                change = ext_dict('whatsapp_cloud change', change)
                if change.get("field") != "messages":
                    continue
                value = change.get("value")
                value = ext_dict('whatsapp_cloud change value', value)
                contacts_by_waid = {}
                contacts = value.get("contacts")
                if contacts is None:
                    contacts = []
                else:
                    contacts = ext_list('whatsapp_cloud contacts', contacts)
                for contact in contacts:
                    contact = ext_dict('whatsapp_cloud contact', contact)
                    wa_id = ext_id(contact.get("wa_id"), "wa_id").strip()
                    profile = contact.get("profile")
                    name = ""
                    if profile is not None:
                        profile = ext_dict('whatsapp_cloud contact profile', profile)
                        name = ext_str(profile.get("name"), "name").strip()
                    if wa_id:
                        contacts_by_waid[wa_id] = name
                messages = value.get("messages")
                if messages is None:
                    continue
                messages = ext_list('whatsapp_cloud messages', messages)
                for raw_message in messages:
                    raw_message = ext_dict('whatsapp_cloud message', raw_message)
                    wamid = ext_id(raw_message.get("id"), "id").strip()
                    if self._is_wamid_seen(wamid):
                        continue
                    parsed = self.parse_cloud_message(raw_message, contacts_by_waid)
                    if not parsed:
                        continue
                    remember_id = wamid if wamid else ext_id(parsed.get("msg_id"), "msg_id")
                    if remember_id.startswith("reaction:"):
                        remember_id = wamid
                    if remember_id:
                        self._remember_wamid(parsed["chat_id"], remember_id)
                    text = parsed["text"]
                    caption = parsed["caption"]
                    force = (text or "").strip().startswith("/start")
                    model_id = None if force else (
                        await self.conductor.db.lookup_model_for_chat(
                            acct_id=acct_id, chat_id=parsed["chat_id"]
                        )
                    )
                    if not model_id:
                        model_id = await self.conductor.ensure_chat_mapping(
                            acct_id=acct_id,
                            chat_id=parsed["chat_id"],
                            chat_type="whatsapp_cloud",
                            carrier_user_id=parsed["carrier_user_id"],
                            force=force,
                        )
                    if not model_id:
                        delivery_failed = True
                        continue
                    body = {
                        "method": "send_message",
                        "params": {"text": text},
                        "text": text,
                        "caption": caption,
                        "attachments": parsed["attachments"],
                        "msg_id": parsed["msg_id"],
                        "platform": "whatsapp_cloud",
                        "chat_id": parsed["chat_id"],
                        "user_id": parsed["carrier_user_id"],
                    }
                    if parsed.get("reply_to"):
                        body["reply_to"] = parsed["reply_to"]
                    if parsed.get("reply_to_text"):
                        body["reply_to_text"] = parsed["reply_to_text"]
                    if parsed.get("raw") is not None:
                        body["raw"] = parsed["raw"]
                    ok = await self.conductor.deliver_inbound(
                        model_id=model_id,
                        acct_id=acct_id,
                        chat_id=parsed["chat_id"],
                        request_id=parsed["msg_id"] or None,
                        body=body,
                    )
                    if ok:
                        self._mark_wamid_seen(wamid)
                    else:
                        delivery_failed = True
        return not delivery_failed

    async def poller_loop(self, acct_id):
        while not self.conductor.poller_stop.is_set():
            await asyncio.sleep(30)

class WhatsAppCloudAdapter(LocalPlatformAdapter):
    acct_type = "whatsapp_cloud"
    capabilities = frozenset(
        {"send_typing", "download_file", "send_message", "send_media", "handle_webhook"}
    )

    def __init__(self, conductor):
        self.conductor = conductor
        self.io = WhatsAppCloudIO(conductor)

    async def send_typing(self, *, acct, token, chat_id):
        return await self.io.send_typing(acct=acct, token=token, chat_id=chat_id)

    async def download_file(self, *, acct, token, file_id):
        return await self.io.download_file(acct=acct, token=token, file_id=file_id)

    async def send_message(self, *, acct, token, chat_id, text, reply_to=None, keyboard=None):
        return await self.io.send_message(
            acct=acct,
            token=token,
            chat_id=chat_id,
            text=text,
            reply_to=reply_to,
            keyboard=keyboard,
        )

    async def send_media(
        self,
        *,
        acct,
        token,
        chat_id,
        method,
        payload,
        file_bytes,
        caption,
        reply_to,
    ):
        return await self.io.send_media(
            acct=acct,
            token=token,
            chat_id=chat_id,
            method=method,
            payload=payload,
            file_bytes=file_bytes,
            caption=caption,
            reply_to=reply_to,
        )

    async def poller_loop(self, acct_id):
        await self.io.poller_loop(acct_id)

    async def close(self):
        await self.io.close()

    async def handle_verify(self, request):
        acct_id = ext_str(request.match_info.get("acct_id"), "acct_id").strip()
        if not acct_id:
            return web.Response(status=400, text="missing acct_id")
        acct, token = await self.io._token_for_acct(acct_id)
        if not acct or not token:
            return web.Response(status=404, text="account not found")
        verify_token = verify_token_for_acct_id(acct_id)
        mode = request.rel_url.query.get("hub.mode", "")
        token_q = request.rel_url.query.get("hub.verify_token", "")
        challenge = request.rel_url.query.get("hub.challenge", "")
        if mode != "subscribe":
            return web.Response(status=400, text="bad mode")
        if not constant_time_equal(token_q, verify_token):
            return web.Response(status=403, text="verify_token mismatch")
        if not challenge:
            return web.Response(status=400, text="missing challenge")
        return web.Response(text=challenge, content_type="text/plain")

    async def handle_webhook(self, request):
        acct_id = ext_str(request.match_info.get("acct_id"), "acct_id").strip()
        if not acct_id:
            return err("Missing acct_id", status=400)
        acct, token = await self.io._token_for_acct(acct_id)
        if not acct or not token:
            return err("Account not found", status=404)
        creds = self.io._creds_for_acct(acct, token)
        app_secret = creds.get("app_secret") or ""
        if not app_secret:
            return web.Response(status=503, text="app_secret not configured")
        try:
            raw = await _read_limited_request_body(request, WEBHOOK_MAX_BODY_BYTES)
        except ValueError:
            return web.Response(status=413)
        signature = ext_str(request.headers.get("X-Hub-Signature-256"), "X-Hub-Signature-256")
        if not self.io.verify_signature(app_secret=app_secret, raw_body=raw, header=signature):
            return web.Response(status=401)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return web.Response(status=400)
        try:
            payload = ext_dict("payload", payload)
        except TypeError:
            return web.Response(status=400)
        ok = await self.io.process_payload(acct_id=acct_id, payload=payload)
        if not ok:
            return web.Response(status=500, text="delivery failed")
        return web.Response(status=200)

    def register_routes(self, app):
        app.router.add_get("/webhook/whatsapp_cloud/{acct_id}", self.handle_verify)
        app.router.add_post("/webhook/whatsapp_cloud/{acct_id}", self.handle_webhook)
