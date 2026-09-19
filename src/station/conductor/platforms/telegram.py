import asyncio
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import aiohttp

from station.conductor.crypto import decrypt_if_encrypted
from station.conductor.platforms import guidance
from station.conductor.platforms.keyboard import parse_keyboard, telegram_reply_markup
from station.conductor.platforms.telegram_network import TelegramFallbackResolver, discover_fallback_ips, parse_fallback_ip_env
from station.conductor.util import inbound_request_id, ext_str, ext_id, ext_bool, ext_int
from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_float, ext_list

_TELEGRAM_API_BASE = "https://api.telegram.org"
_TELEGRAM_API_HOST = "api.telegram.org"
_GENERAL_TOPIC_THREAD_ID = "1"

_TELEGRAM_SEND_METHOD = {
    "send_message": "sendMessage",
    "send_photo": "sendPhoto",
    "send_video": "sendVideo",
    "send_audio": "sendAudio",
    "send_document": "sendDocument",
    "send_voice": "sendVoice",
    "send_animation": "sendAnimation",
    "send_sticker": "sendSticker",
}

_METHOD_MEDIA_FIELD = {
    "send_photo": "photo",
    "send_video": "video",
    "send_audio": "audio",
    "send_document": "document",
    "send_voice": "voice",
    "send_animation": "animation",
    "send_sticker": "sticker",
}

def effective_message_thread_id(msg: dict) -> str:
    msg = ext_dict('telegram message', msg)
    chat = msg.get("chat")
    if chat is None:
        raise ExternalError("telegram message chat required")
    chat = ext_dict('telegram message chat', chat)
    chat_type = ext_str(chat.get("type"), "type").strip().lower()
    raw = msg.get("message_thread_id")
    is_topic_message = ext_bool(msg.get("is_topic_message"), "is_topic_message")
    is_forum_group = chat_type in ("group", "supergroup") and ext_bool(chat.get("is_forum"), "is_forum")
    if raw is not None and raw != "":
        raw_str = ext_id(raw, "message_thread_id").strip()
        if is_forum_group or (chat_type in ("group", "supergroup") and is_topic_message):
            return raw_str
        if chat_type == "private" and is_topic_message:
            return raw_str
        return ""
    if is_forum_group:
        return _GENERAL_TOPIC_THREAD_ID
    return ""

def join_telegram_chat_id(chat_id: str, thread_id: str) -> str:
    chat_id = (chat_id or "").strip()
    thread_id = (thread_id or "").strip()
    if not chat_id or not thread_id:
        return chat_id
    return f"{chat_id}:{thread_id}"

def platform_chat_id_for_message(raw: str, thread: str) -> str:
    raw = (raw or "").strip()
    thread = (thread or "").strip()
    if message_thread_id_for_send(thread) is not None:
        return join_telegram_chat_id(raw, thread)
    return raw

def split_telegram_chat_id(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if not value:
        return "", ""
    idx = value.rfind(":")
    if idx <= 0 or idx == len(value) - 1:
        return value, ""
    thread = value[idx + 1 :]
    if not thread.isdigit():
        return value, ""
    chat = value[:idx].strip()
    if not chat:
        return value, ""
    return chat, thread

def open_telegram_chat_id(platform_chat_id: str) -> str:
    chat_id, _ = split_telegram_chat_id(platform_chat_id)
    return chat_id

def message_thread_id_for_send(thread_id: str | None) -> int | None:
    raw = (thread_id or "").strip()
    if not raw or raw == _GENERAL_TOPIC_THREAD_ID:
        return None
    if not raw.isdigit():
        return None
    return int(raw)

def message_thread_id_for_typing(thread_id: str | None) -> int | None:
    raw = (thread_id or "").strip()
    if not raw or not raw.isdigit():
        return None
    return int(raw)

def _thread_payload(*, thread_id: str | None, for_typing: bool = False) -> dict[str, Any]:
    if for_typing:
        tid = message_thread_id_for_typing(thread_id)
    else:
        tid = message_thread_id_for_send(thread_id)
    if tid is None:
        return {}
    return {"message_thread_id": tid}

_TELEGRAM_ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "channel_post",
    "edited_channel_post",
    "inline_query",
    "chosen_inline_result",
    "callback_query",
    "shipping_query",
    "pre_checkout_query",
    "poll",
    "poll_answer",
    "my_chat_member",
    "chat_join_request",
    "chat_boost",
    "removed_chat_boost",
    "message_reaction",
    "message_reaction_count",
]

def reply_quote_snippet(msg: dict) -> str:
    quote = msg.get("quote")
    if quote is not None:
        quote = ext_dict('telegram quote', quote)
        q = ext_str(quote.get("text"), "text").strip()
        if q:
            return guidance.telegram_reply_snippet(q)
    rt = msg.get("reply_to_message")
    if rt is None:
        return ""
    rt = ext_dict('telegram reply_to_message', rt)
    t = ext_str(rt.get("text"), "text").strip()
    if t:
        return guidance.telegram_reply_snippet(t)
    c = ext_str(rt.get("caption"), "caption").strip()
    if c:
        return guidance.telegram_reply_snippet(c)
    if _reply_to_has_media(rt):
        return guidance.telegram_reply_snippet("(attachment)")
    return ""

def _reply_to_has_media(rt: dict) -> bool:
    photo = rt.get("photo")
    if photo is not None:
        photo = ext_list('telegram photo', photo)
        if photo:
            return True
    for key in ("document", "video", "audio", "voice", "video_note", "sticker", "animation"):
        obj = rt.get(key)
        if obj is None:
            continue
        obj = ext_dict('obj', obj)
        if obj.get("file_id"):
            return True
    return False

def reaction_type_parts(reactions) -> tuple[list[str], list[str]]:
    emojis = []
    custom_ids = []
    if reactions is None:
        return emojis, custom_ids
    reactions = ext_list('telegram reactions', reactions)
    for r in reactions:
        r = ext_dict('telegram reaction', r)
        typ = ext_str(r.get("type"), "type").strip()
        if typ == "custom_emoji":
            cid = ext_id(r.get("custom_emoji_id"), "custom_emoji_id").strip()
            if cid:
                custom_ids.append(cid)
            continue
        e = ext_str(r.get("emoji"), "emoji").strip()
        if e:
            emojis.append(e)
        elif typ != "emoji":
            cid = ext_id(r.get("custom_emoji_id"), "custom_emoji_id").strip()
            if cid:
                custom_ids.append(cid)
    return emojis, custom_ids

@dataclass
class TelegramUpdate:
    update_id: int
    msg_id: str
    chat_id: str
    carrier_user_id: str | None
    text: str
    caption: str
    attachments: list[dict]
    is_edit: bool = False
    reply_to: str = ""
    reply_to_text: str = ""
    chat_type: str = ""
    callback_query_id: str = ""

class TelegramIO:
    def __init__(self, conductor):
        self.conductor = conductor
        self._api_session: aiohttp.ClientSession | None = None
        self._network_ready = False
        self._fallback_ips: list[str] = parse_fallback_ip_env(os.environ.get("TELEGRAM_FALLBACK_IPS"))
        self._webhook_configured: dict[str, float] = {}
        self._conflict_count = 0

    def _api_base(self) -> str:
        raw = self.conductor.config.telegram_api_url.strip()
        if not raw:
            raw = (
                os.environ.get("TELEGRAM_API_URL")
                or os.environ.get("TGB_API_URL")
                or os.environ.get("TELEGRAM_API_BASE")
                or ""
            ).strip()
        return raw.rstrip("/") if raw else _TELEGRAM_API_BASE

    def _api_host(self) -> str:
        base = self._api_base()
        parsed = urlparse(base)
        if parsed.hostname:
            return parsed.hostname
        return _TELEGRAM_API_HOST

    @property
    def webhook_enabled(self) -> bool:
        return self.conductor.config.telegram_webhook_mode

    @property
    def webhook_secret(self) -> str:
        secret = self.conductor.config.telegram_webhook_secret.strip()
        if secret:
            return secret
        return (os.environ.get("TELEGRAM_WEBHOOK_SECRET") or "").strip()

    @property
    def webhook_active(self) -> bool:
        return self.webhook_enabled and bool(self.webhook_secret)

    async def _ensure_network_ready(self) -> None:
        if self._network_ready and self._api_session and not self._api_session.closed:
            return
        host = self._api_host()
        if host != _TELEGRAM_API_HOST:
            timeout = aiohttp.ClientTimeout(total=60)
            self._api_session = aiohttp.ClientSession(timeout=timeout, trust_env=True)
            self._network_ready = True
            return
        auto = (os.environ.get("TELEGRAM_FALLBACK_AUTO_DISCOVER") or "").strip() not in ("", "0", "false", "False")
        if auto and not self._fallback_ips:
            self._fallback_ips = await discover_fallback_ips()
        resolver = TelegramFallbackResolver(None, self._fallback_ips)
        connector = aiohttp.TCPConnector(resolver=resolver, ttl_dns_cache=30)
        timeout = aiohttp.ClientTimeout(total=60)
        self._api_session = aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=True)
        self._network_ready = True

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._api_session is None or self._api_session.closed:
            raise RuntimeError("telegram session not initialized")
        return self._api_session

    def parse_update(self, update: dict) -> TelegramUpdate | None:
        update = ext_dict('telegram update', update)
        update_id = update.get("update_id")
        update_id = ext_int(update_id, 'telegram update_id')
        rxn = update.get("message_reaction")
        if rxn is not None:
            rxn = ext_dict('telegram message_reaction', rxn)
            return self._parse_reaction(update_id, rxn)
        cq = update.get("callback_query")
        if cq is not None:
            return self._parse_callback_query(update_id, ext_dict("telegram callback_query", cq))
        is_edit = False
        msg = update.get("message")
        if msg is None:
            msg = update.get("edited_message")
            if msg is not None:
                is_edit = True
        if msg is None:
            return None
        msg = ext_dict('telegram message', msg)
        msg_id = msg.get("message_id")
        if msg_id is None:
            return None
        chat = msg.get("chat")
        chat = ext_dict('telegram message chat', chat)
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        if ext_str(chat.get("type"), "type").strip().lower() == "channel":
            return None
        chat_id_str = platform_chat_id_for_message(ext_id(chat_id, "chat.id", allow_none=False), effective_message_thread_id(msg))
        chat_type = ext_str(chat.get("type"), "type").strip()
        sender = msg.get("from")
        carrier_user_id = None
        if sender is not None:
            sender = ext_dict('telegram message from', sender)
            if sender.get("id") is not None:
                carrier_user_id = ext_id(sender.get("id"), "sender.id", allow_none=False)
        text = ext_str(msg.get("text"), "text").strip()
        caption = ext_str(msg.get("caption"), "caption").strip()
        if not text and caption:
            text = caption
        reply_to = ""
        rt = msg.get("reply_to_message")
        if rt is not None:
            rt = ext_dict('telegram reply_to_message', rt)
            if rt.get("message_id") is not None:
                reply_to = ext_id(rt.get("message_id"), "reply.message_id", allow_none=False)

        attachments: list[dict] = []
        photo = msg.get("photo")
        if photo is not None:
            photo = ext_list('telegram photo', photo)
            if photo:
                best = photo[-1]
                best = ext_dict('telegram photo size', best)
                if best.get("file_id"):
                    att = {"type": "photo", "file_id": best.get("file_id"), "file_name": "", "content_type": ""}
                    fs = best.get("file_size")
                    if fs is not None:
                        att["file_size"] = ext_int(fs, "file_size")
                    attachments.append(att)
        for t_key, att_type in (
            ("document", "document"),
            ("video", "video"),
            ("video_note", "video_note"),
            ("audio", "audio"),
            ("voice", "voice"),
            ("animation", "animation"),
        ):
            obj = msg.get(t_key)
            if obj is None:
                continue
            obj = ext_dict('obj', obj)
            if obj.get("file_id"):
                att = {
                    "type": att_type,
                    "file_id": obj.get("file_id"),
                    "file_name": ext_str(obj.get("file_name"), "file_name"),
                    "content_type": ext_str(obj.get("mime_type"), "mime_type"),
                }
                fs = obj.get("file_size")
                if fs is not None:
                    att["file_size"] = ext_int(fs, "file_size")
                attachments.append(att)
        sticker = msg.get("sticker")
        sticker_meta = None
        if sticker is not None:
            sticker = ext_dict('telegram sticker', sticker)
            if sticker.get("file_id"):
                emoji = ext_str(sticker.get("emoji"), "emoji").strip()
                set_name = ext_str(sticker.get("set_name"), "set_name").strip()
                is_animated = bool(sticker.get("is_animated"))
                is_video = bool(sticker.get("is_video"))
                att = {
                    "type": "sticker",
                    "file_id": sticker.get("file_id"),
                    "file_name": "",
                    "content_type": ext_str(sticker.get("mime_type"), "mime_type"),
                }
                unique = ext_id(sticker.get("file_unique_id"), "file_unique_id").strip()
                if unique:
                    att["file_unique_id"] = unique
                fs = sticker.get("file_size")
                if fs is not None:
                    att["file_size"] = ext_int(fs, "file_size")
                if emoji:
                    att["emoji"] = emoji
                if set_name:
                    att["set_name"] = set_name
                if is_animated:
                    att["is_animated"] = True
                if is_video:
                    att["is_video"] = True
                attachments.append(att)
                sticker_meta = (emoji, set_name, is_animated, is_video)

        base = self._api_base()
        attachments, size_notes = guidance.filter_oversized_attachments(
            "telegram", attachments, telegram_base_url=base
        )
        if sticker_meta and any(a.get("type") == "sticker" for a in attachments) and not text:
            emoji, set_name, is_animated, is_video = sticker_meta
            if is_animated or is_video:
                text = guidance.animated_sticker_injection(emoji)
            else:
                desc = "a sticker"
                if emoji:
                    desc = f"a sticker with emoji {emoji}"
                text = guidance.sticker_injection(desc, emoji, set_name)
        if not text:
            loc_text = self._location_text(msg)
            if loc_text:
                text = loc_text
            else:
                contact_text = self._contact_text(msg)
                if contact_text:
                    text = contact_text
        if size_notes:
            note = "\n".join(size_notes)
            text = f"{text}\n{note}".strip() if text else note

        reply_snippet = reply_quote_snippet(msg)
        reply_to_text = reply_snippet
        text = guidance.inject_telegram_reply_context(text, reply_to_text)

        return TelegramUpdate(
            update_id=update_id,
            msg_id=ext_id(msg_id, "message_id"),
            chat_id=chat_id_str,
            carrier_user_id=carrier_user_id,
            text=text,
            caption=caption,
            attachments=attachments,
            is_edit=is_edit,
            reply_to=reply_to,
            reply_to_text=reply_to_text,
            chat_type=chat_type,
        )

    def _location_text(self, msg: dict) -> str:
        venue = msg.get("venue")
        location = msg.get("location")
        title = ""
        address = ""
        lat = None
        lon = None
        if venue is not None:
            venue = ext_dict('telegram venue', venue)
            title = ext_str(venue.get("title"), "title").strip()
            address = ext_str(venue.get("address"), "address").strip()
            vloc = venue.get("location")
            if vloc is not None:
                vloc = ext_dict('telegram venue location', vloc)
                if location is None:
                    location = vloc
        if location is not None:
            location = ext_dict('telegram location', location)
            if location.get("latitude") is not None and location.get("longitude") is not None:
                lat_raw = location.get("latitude")
                lon_raw = location.get("longitude")
                lat_raw = ext_float('telegram latitude', lat_raw)
                lon_raw = ext_float('telegram longitude', lon_raw)
                lat = float(lat_raw)
                lon = float(lon_raw)
        if lat is None or lon is None:
            return ""
        return guidance.location_injection(lat, lon, title, address)

    def _contact_text(self, msg: dict) -> str:
        contact = msg.get("contact")
        if contact is None:
            return ""
        contact = ext_dict('telegram contact', contact)
        first = ext_str(contact.get("first_name"), "first_name").strip()
        last = ext_str(contact.get("last_name"), "last_name").strip()
        name = f"{first} {last}".strip()
        phone = ext_str(contact.get("phone_number"), "phone_number").strip()
        phones = [phone] if phone else []
        if not name and not phones:
            return ""
        return guidance.contact_injection(name, phones)

    def _parse_reaction(self, update_id: int, rxn: dict) -> TelegramUpdate | None:
        chat = rxn.get("chat")
        if chat is None:
            return None
        chat = ext_dict('telegram reaction chat', chat)
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        if ext_str(chat.get("type"), "type").strip().lower() == "channel":
            return None
        message_id = rxn.get("message_id")
        if message_id is None:
            return None
        carrier_user_id = None
        user = rxn.get("user")
        if user is not None:
            user = ext_dict('telegram reaction user', user)
            if user.get("id") is not None:
                carrier_user_id = ext_id(user.get("id"), "user.id", allow_none=False)
        if carrier_user_id is None:
            actor_chat = rxn.get("actor_chat")
            if actor_chat is not None:
                actor_chat = ext_dict('telegram reaction actor_chat', actor_chat)
                if actor_chat.get("id") is not None:
                    carrier_user_id = ext_id(actor_chat.get("id"), "actor_chat.id", allow_none=False)
        emojis, custom_ids = reaction_type_parts(rxn.get("new_reaction"))
        text = guidance.format_telegram_reaction_text(emojis, custom_ids)
        chat_id_str = platform_chat_id_for_message(ext_id(chat_id, "chat.id", allow_none=False), effective_message_thread_id(rxn))
        return TelegramUpdate(
            update_id=update_id,
            msg_id=f"reaction:{update_id}",
            chat_id=chat_id_str,
            carrier_user_id=carrier_user_id,
            text=text,
            caption="",
            attachments=[],
            reply_to=ext_id(message_id, "message_id"),
            chat_type=ext_str(chat.get("type"), "type").strip(),
        )

    def _parse_callback_query(self, update_id: int, cq: dict) -> TelegramUpdate | None:
        data = ext_str(cq.get("data"), "data").strip()
        if not data:
            return None
        query_id = ext_id(cq.get("id"), "id").strip()
        sender = cq.get("from")
        carrier_user_id = None
        if sender is not None:
            sender = ext_dict("telegram callback from", sender)
            if sender.get("id") is not None:
                carrier_user_id = ext_id(sender.get("id"), "sender.id", allow_none=False)
        msg = cq.get("message")
        if msg is None:
            return None
        msg = ext_dict("telegram callback message", msg)
        chat = msg.get("chat")
        if chat is None:
            return None
        chat = ext_dict("telegram callback chat", chat)
        chat_id = chat.get("id")
        if chat_id is None:
            return None
        if ext_str(chat.get("type"), "type").strip().lower() == "channel":
            return None
        chat_id_str = platform_chat_id_for_message(ext_id(chat_id, "chat.id", allow_none=False), effective_message_thread_id(msg))
        reply_to = ""
        if msg.get("message_id") is not None:
            reply_to = ext_id(msg.get("message_id"), "message_id")
        msg_id = query_id or str(update_id)
        return TelegramUpdate(
            update_id=update_id,
            msg_id=f"callback:{msg_id}",
            chat_id=chat_id_str,
            carrier_user_id=carrier_user_id,
            text=data,
            caption="",
            attachments=[],
            reply_to=reply_to,
            chat_type=ext_str(chat.get("type"), "type").strip(),
            callback_query_id=query_id,
        )

    async def _get_updates(self, *, token: str, offset: int) -> list[dict] | None:
        await self._ensure_network_ready()
        url = f"{self._api_base()}/bot{token}/getUpdates"
        params = {"timeout": 25, "offset": offset, "allowed_updates": _TELEGRAM_ALLOWED_UPDATES}
        async with self.session.get(url, params=params) as resp:
            body = await resp.json(content_type=None)
            status = int(resp.status)
        body = ext_dict('telegram getUpdates body', body)
        delay = self._rate_limit_delay(status, body)
        if delay:
            await asyncio.sleep(delay)
            async with self.session.get(url, params=params) as resp:
                body = await resp.json(content_type=None)
                status = int(resp.status)
            body = ext_dict('telegram getUpdates body', body)
        if not body.get("ok"):
            desc = ext_str(body.get("description"), "description")
            if status == 409 or ("conflict" in desc.lower() and "getupdates" in desc.lower()):
                await self._recover_polling_conflict(token=token, description=desc)
            return None
        result = body.get("result")
        result = ext_list('telegram getUpdates result', result)
        self._conflict_count = 0
        out = []
        for u in result:
            u = ext_dict('telegram update', u)
            out.append(u)
        return out

    async def _recover_polling_conflict(self, *, token: str, description: str = "") -> None:
        self._conflict_count += 1
        count = self._conflict_count
        max_retries = 5
        delay = min(60.0, 10.0 + count * 10.0)
        logger.warning(
            "telegram polling conflict attempt=%s/%s delay=%.0fs desc=%s",
            count,
            max_retries,
            delay,
            (description or "")[:200],
        )
        await self._delete_webhook(token=token)
        try:
            sess = self._api_session
            if sess is not None and not sess.closed:
                await sess.close()
        except Exception as e:
            logger.error("unexpected where=telegram_conflict_session_close error=%s", e, exc_info=e)
        self._api_session = None
        self._network_ready = False
        await asyncio.sleep(delay)

    async def _delete_webhook(self, *, token: str) -> bool:
        await self._ensure_network_ready()
        url = f"{self._api_base()}/bot{token}/deleteWebhook"
        async with self.session.post(url, json={"drop_pending_updates": False}) as resp:
            body = await resp.json(content_type=None)
        body = ext_dict('telegram deleteWebhook body', body)
        return bool(body.get("ok"))

    async def _get_update_offset(self, acct_id: str) -> int:
        state = await self.conductor.db.get_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="telegram",
            state_key="poller",
        )
        if not state:
            return 0
        offset = state.get("offset")
        if offset is None:
            return 0
        return ext_int(offset, "offset")

    async def _set_update_offset(self, acct_id: str, offset: int) -> None:
        await self.conductor.db.set_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="telegram",
            state_key="poller",
            state={"offset": int(offset)},
        )

    async def send_typing(self, *, token: str, chat_id: str) -> bool:
        await self._ensure_network_ready()
        api_chat_id, thread_from_chat = split_telegram_chat_id(chat_id)
        url = f"{self._api_base()}/bot{token}/sendChatAction"
        payload: dict[str, Any] = {"chat_id": api_chat_id, "action": "typing"}
        payload.update(_thread_payload(thread_id=thread_from_chat, for_typing=True))
        async with self.session.post(url, json=payload) as resp:
            body = await resp.json(content_type=None)
        body = ext_dict('telegram sendChatAction body', body)
        return bool(body.get("ok"))

    async def download_file(self, *, token: str, file_id: str) -> bytes | None:
        await self._ensure_network_ready()
        get_url = f"{self._api_base()}/bot{token}/getFile"
        async with self.session.post(get_url, json={"file_id": file_id}) as resp:
            body = await resp.json(content_type=None)
        body = ext_dict('telegram getFile body', body)
        if not body.get("ok"):
            return None
        result = body.get("result")
        result = ext_dict('telegram getFile result', result)
        fs = result.get("file_size")
        file_size = ext_int(fs, "file_size") if fs is not None else 0
        try:
            guidance.check_media_size("telegram", file_size, "document", telegram_base_url=self._api_base())
        except guidance.MediaTooLargeError:
            return None
        file_path = ext_str(result.get("file_path"), "file_path").strip()
        if not file_path:
            return None
        dl_url = f"{self._api_base()}/file/bot{token}/{file_path}"
        limit = guidance.max_telegram_media_bytes(self._api_base())
        async with self.session.get(dl_url) as resp:
            if resp.status != 200:
                return None
            data = await resp.content.read(limit + 1)
            if len(data) > limit:
                return None
            return data

    def _rate_limit_delay(self, status, body: dict) -> int:
        code = 0
        retry_after = 0
        raw_code = body.get("error_code")
        if raw_code is not None:
            code = ext_int(raw_code, "error_code")
        params = body.get("parameters")
        if params is not None:
            params = ext_dict('telegram error parameters', params)
            ra = params.get("retry_after")
            if ra is not None:
                retry_after = ext_int(ra, "retry_after")
        if int(status) == 429 or code == 429:
            return max(1, retry_after)
        return 0

    async def _send(
        self,
        *,
        token: str,
        method: str,
        payload: dict,
        file_bytes: bytes | None = None,
        file_field: str | None = None,
        filename: str | None = None,
    ) -> tuple[bool, str]:
        api_method = _TELEGRAM_SEND_METHOD.get(method)
        if not api_method:
            return False, "unknown method"
        await self._ensure_network_ready()
        url = f"{self._api_base()}/bot{token}/{api_method}"
        last_desc = ""
        for _ in range(4):
            if file_bytes is None:
                async with self.session.post(url, json=payload) as resp:
                    body = await resp.json(content_type=None)
                    status = int(resp.status)
            else:
                data = aiohttp.FormData()
                for k, v in payload.items():
                    if v is None:
                        continue
                    data.add_field(k, str(v))
                field = (file_field or "file")
                name = (filename or "").strip() or "file"
                data.add_field(field, file_bytes, filename=name)
                async with self.session.post(url, data=data) as resp:
                    body = await resp.json(content_type=None)
                    status = int(resp.status)
            body = ext_dict('telegram send body', body)
            if body.get("ok"):
                return True, ""
            delay = self._rate_limit_delay(status, body)
            if delay:
                await asyncio.sleep(min(delay, 120))
                continue
            last_desc = ext_str(body.get("description"), "description")
            if self._is_reply_not_found_error(last_desc) and (
                "reply_to_message_id" in payload or "reply_parameters" in payload
            ):
                payload = dict(payload)
                payload.pop("reply_to_message_id", None)
                payload.pop("reply_parameters", None)
                continue
            if self._is_thread_not_found_error(last_desc) and "message_thread_id" in payload:
                payload = dict(payload)
                payload.pop("message_thread_id", None)
                continue
            return False, last_desc
        return False, last_desc

    @staticmethod
    def _is_parse_mode_error(desc: str) -> bool:
        msg = (desc or "").lower()
        needles = (
            "parse",
            "can't find end",
            "cant find end",
            "can't parse entities",
            "cant parse entities",
            "entity",
            "entities",
            "unsupported start tag",
            "character is reserved",
            "nested entities",
            "must be escaped",
            "begin with a valid",
            "unexpected end of",
            "unmatched",
        )
        return any(n in msg for n in needles)

    @staticmethod
    def _is_thread_not_found_error(desc: str) -> bool:
        msg = (desc or "").lower()
        return (
            "thread not found" in msg
            or "message thread not found" in msg
            or "message thread_id is invalid" in msg
            or "topic_closed" in msg
            or ("message_thread_id" in msg and "invalid" in msg)
        )

    @staticmethod
    def _is_reply_not_found_error(desc: str) -> bool:
        msg = (desc or "").lower()
        return (
            "message to be replied not found" in msg
            or "replied message not found" in msg
            or "message to reply not found" in msg
            or "reply message not found" in msg
            or "message to be replied is not found" in msg
            or "message can't be replied" in msg
            or "message can not be replied" in msg
            or ("reply" in msg and "not found" in msg)
        )
    def _reply_to_payload(self, reply_to: str | None) -> dict[str, Any]:
        if not reply_to:
            return {}
        raw = reply_to.strip()
        if not raw.isdigit():
            return {}
        return {"reply_to_message_id": int(raw)}

    async def send_message(
        self,
        *,
        token: str,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        keyboard=None,
    ) -> bool:
        api_chat_id, thread_from_chat = split_telegram_chat_id(chat_id)
        chunks = guidance.prepare_outbound_text("telegram", (text or ""))
        markup = telegram_reply_markup(keyboard) if parse_keyboard(keyboard) else None
        if not chunks:
            if markup is None:
                return False
            chunks = [" "]
        last_ok = False
        for idx, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": api_chat_id,
                "text": chunk,
                "parse_mode": "MarkdownV2",
            }
            payload.update(_thread_payload(thread_id=thread_from_chat))
            if idx == 0:
                payload.update(self._reply_to_payload(reply_to))
                if markup is not None:
                    payload["reply_markup"] = markup
            ok, desc = await self._send(token=token, method="send_message", payload=payload)
            if not ok and self._is_parse_mode_error(desc):
                plain_payload = {
                    "chat_id": api_chat_id,
                    "text": guidance.strip_markdown_v2(chunk),
                }
                plain_payload.update(_thread_payload(thread_id=thread_from_chat))
                if idx == 0:
                    plain_payload.update(self._reply_to_payload(reply_to))
                    if markup is not None:
                        plain_payload["reply_markup"] = markup
                ok, _ = await self._send(token=token, method="send_message", payload=plain_payload)
            if not ok:
                return False
            last_ok = True
        return last_ok

    async def set_webhook(self, *, token: str, url: str, secret: str | None) -> bool:
        await self._ensure_network_ready()
        api = f"{self._api_base()}/bot{token}/setWebhook"
        secret = (secret or "").strip()
        if not secret:
            return False
        payload: dict[str, Any] = {
            "url": url,
            "allowed_updates": list(_TELEGRAM_ALLOWED_UPDATES),
            "secret_token": secret,
        }
        async with self.session.post(api, json=payload) as resp:
            body = await resp.json(content_type=None)
        body = ext_dict('telegram setWebhook body', body)
        return bool(body.get("ok"))

    async def send_media(
        self,
        *,
        token: str,
        method: str,
        chat_id: str,
        caption: str,
        file_bytes: bytes | None,
        media_ref: str | None,
        reply_to: str | None = None,
        filename: str | None = None,
    ) -> bool:
        method = guidance.resolve_telegram_media_method(method, filename or "")
        field = _METHOD_MEDIA_FIELD.get(method)
        if not field:
            return False
        if file_bytes is not None:
            kind = guidance.media_kind_from_method(method)
            try:
                guidance.check_media_size(
                    "telegram",
                    len(file_bytes),
                    kind,
                    telegram_base_url=self._api_base(),
                )
            except guidance.MediaTooLargeError:
                return False
        api_chat_id, thread_from_chat = split_telegram_chat_id(chat_id)
        payload: dict[str, Any] = {"chat_id": api_chat_id}
        payload.update(_thread_payload(thread_id=thread_from_chat))
        prepared_caption = ""
        if caption and method != "send_sticker":
            prepared_caption = guidance.prepare_outbound_caption("telegram", caption)
            if prepared_caption:
                payload["caption"] = prepared_caption
                payload["parse_mode"] = "MarkdownV2"
        payload.update(self._reply_to_payload(reply_to))
        if file_bytes is None:
            ref = (media_ref or "").strip()
            if not ref:
                return False
            payload[field] = ref
            ok, desc = await self._send(token=token, method=method, payload=payload)
        else:
            ok, desc = await self._send(
                token=token,
                method=method,
                payload=payload,
                file_bytes=file_bytes,
                file_field=field,
                filename=filename,
            )
        if not ok and prepared_caption and self._is_parse_mode_error(desc):
            plain = guidance.strip_markdown_v2(prepared_caption)
            plain = guidance.truncate_to_limit(plain, guidance.MAX_TELEGRAM_CAPTION_LENGTH, guidance.utf16_len)
            retry = dict(payload)
            retry["caption"] = plain
            retry.pop("parse_mode", None)
            if file_bytes is None:
                ok, _ = await self._send(token=token, method=method, payload=retry)
            else:
                ok, _ = await self._send(
                    token=token,
                    method=method,
                    payload=retry,
                    file_bytes=file_bytes,
                    file_field=field,
                    filename=filename,
                )
        return ok

    async def close(self) -> None:
        if self._api_session is not None and not self._api_session.closed:
            await self._api_session.close()
        self._api_session = None
        self._network_ready = False

    async def ensure_webhook(self, *, acct_id: str) -> bool:
        if not acct_id:
            return False
        now = asyncio.get_running_loop().time()
        last = self._webhook_configured.get(acct_id, 0.0)
        if now - last < 60.0:
            return True
        acct = await self.conductor.db.get_local_account(acct_id)
        if not acct:
            return False
        enc = (acct.get("encrypted_token") or "").strip()
        token, _ = decrypt_if_encrypted(self.conductor.private_key, enc)
        token = (token or "").strip()
        if not token:
            return False
        base = self.conductor.conductor_address()
        secret = self.webhook_secret
        if not secret:
            return False
        url = f"{base}/webhook/telegram/{acct_id}"
        ok = await self.set_webhook(token=token, url=url, secret=secret)
        if ok:
            self._webhook_configured[acct_id] = now
        return ok

    async def process_update(self, *, acct_id: str, update: dict) -> bool:
        parsed = self.parse_update(update)
        if not parsed:
            return True
        force = parsed.text.startswith("/start")
        open_id = open_telegram_chat_id(parsed.chat_id)
        model_id = None if force else (await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=open_id))
        if not model_id:
            model_id = await self.conductor.ensure_chat_mapping(
                acct_id=acct_id,
                chat_id=open_id,
                chat_type="telegram",
                carrier_user_id=parsed.carrier_user_id,
                force=force,
            )
        if not model_id:
            return False
        body = {
            "method": "send_message",
            "params": {"text": parsed.text},
            "text": parsed.text,
            "caption": parsed.caption,
            "attachments": parsed.attachments,
            "msg_id": parsed.msg_id,
        }
        if parsed.reply_to:
            body["reply_to"] = parsed.reply_to
        if parsed.reply_to_text:
            body["reply_to_text"] = parsed.reply_to_text
        if parsed.chat_type:
            body["chat_type"] = parsed.chat_type
        if parsed.is_edit:
            body["is_edit"] = True
        if not parsed.text and not parsed.caption and not parsed.attachments:
            body["raw"] = update
        ok = await self.conductor.deliver_inbound(
            model_id=model_id,
            acct_id=acct_id,
            chat_id=parsed.chat_id,
            request_id=inbound_request_id("telegram", acct_id, parsed.chat_id, parsed.update_id),
            body=body,
        )
        if ok and parsed.callback_query_id:
            await self._answer_callback_query(acct_id=acct_id, callback_query_id=parsed.callback_query_id)
        return ok

    async def _answer_callback_query(self, *, acct_id: str, callback_query_id: str) -> None:
        callback_query_id = (callback_query_id or "").strip()
        if not callback_query_id:
            return
        acct = await self.conductor.db.get_local_account(acct_id)
        if not acct:
            return
        enc = (acct.get("encrypted_token") or "").strip()
        token, _ = decrypt_if_encrypted(self.conductor.private_key, enc)
        token = (token or "").strip()
        if not token:
            return
        try:
            await self._ensure_network_ready()
            url = f"{self._api_base()}/bot{token}/answerCallbackQuery"
            async with self.session.post(url, json={"callback_query_id": callback_query_id}) as resp:
                await resp.read()
        except Exception as e:
            logger.error("unexpected where=telegram_answer_callback acct_id=%s error=%s", acct_id, e, exc_info=e)

    async def poller_loop(self, acct_id: str) -> None:
        if self.webhook_active:
            return
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
                offset = await self._get_update_offset(acct_id)
                updates = await self._get_updates(token=token, offset=offset)
                if updates is None:
                    await asyncio.sleep(1)
                    continue
                max_update_id = None
                failed = False
                for u in updates:
                    u = ext_dict('telegram update', u)
                    uid = ext_int(u.get("update_id"), "update_id")
                    ok = await self.process_update(acct_id=acct_id, update=u)
                    if not ok:
                        failed = True
                        break
                    if max_update_id is None or uid > max_update_id:
                        max_update_id = uid
                if max_update_id is not None:
                    await self._set_update_offset(acct_id, max_update_id + 1)
                if failed:
                    await asyncio.sleep(2)
                    continue
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=telegram_poller acct_id=%s error=%s", acct_id, e, exc_info=e)
                await asyncio.sleep(2)
                continue
