import asyncio
import io
import json
import os
import time
from typing import Any

import aiohttp

from station.conductor.crypto import decrypt_if_encrypted
from station.conductor.platforms import guidance
from station.conductor.platforms.keyboard import discord_components, parse_keyboard
from station.conductor.util import attachment_type_from_meta, ext_bool, ext_id, ext_int, ext_str
from station import logger
from station.prototypes.boundary import ext_dict, ext_float, ext_list, ext_require

_THREAD_CHANNEL_TYPES = {10, 11, 12}

def is_thread_channel_type(channel_type):
    return int(channel_type) in _THREAD_CHANNEL_TYPES

def _discord_chat_type_name(channel_type):
    if channel_type == 1:
        return "dm"
    if channel_type == 3:
        return "group_dm"
    if channel_type in (2, 13):
        return "voice"
    if channel_type == 15:
        return "forum"
    if is_thread_channel_type(channel_type):
        return "thread"
    return "guild_text"

def join_chat_id(chat_id, thread_id=""):
    chat_id = (chat_id or "").strip()
    thread_id = (thread_id or "").strip()
    if not chat_id or not thread_id:
        return chat_id
    return f"{chat_id}:{thread_id}"

def split_chat_id(value):
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

def open_chat_id(platform_chat_id):
    chat_id, _ = split_chat_id(platform_chat_id)
    return chat_id

def send_channel_id(platform_chat_id):
    parent, thread = split_chat_id(platform_chat_id)
    if thread:
        return thread
    return parent

def resolve_chat_and_thread(channel_id, parent_id="", channel_type=0):
    channel_id = (channel_id or "").strip()
    parent_id = (parent_id or "").strip()
    if not channel_id:
        return "", "", ""
    ctype = 0 if channel_type is None else int(channel_type)
    if is_thread_channel_type(ctype) and parent_id:
        return join_chat_id(parent_id, channel_id), parent_id, "thread"
    if parent_id and parent_id != channel_id:
        return join_chat_id(parent_id, channel_id), parent_id, "thread"
    return channel_id, channel_id, _discord_chat_type_name(ctype)

def _discord_channel_parent_id(chan) -> str:
    pid = chan.parent_id
    if pid is None:
        return ""
    return ext_id(pid, "parent_id").strip()

def _discord_sticker_format(s) -> int:
    fmt = s.format
    if fmt is None:
        return 1
    return int(fmt)

class _DiscordAcctState:

    __slots__ = (
        "client",
        "client_task",
        "voice_pending",
        "gateway_ws",
        "gateway_task",
        "gateway_ready",
        "gateway_token",
        "gateway_user_id",
        "gateway_session_id",
    )

    def __init__(self) -> None:
        self.client: Any = None
        self.client_task: asyncio.Task | None = None
        self.voice_pending: dict[str, dict] = {}
        self.gateway_ws: Any = None
        self.gateway_task: asyncio.Task | None = None
        self.gateway_ready = asyncio.Event()
        self.gateway_token: str = ""
        self.gateway_user_id: str = ""
        self.gateway_session_id: str = ""

class DiscordIO:
    def __init__(self, conductor):
        self.conductor = conductor
        self._accounts: dict[str, _DiscordAcctState] = {}
        self._dsb_channel_cursors: dict[str, dict[str, str]] = {}

    def _acct_state(self, acct_id: str) -> _DiscordAcctState:
        if not acct_id:
            raise ValueError("discord acct_id required")
        st = self._accounts.get(acct_id)
        if st is None:
            st = _DiscordAcctState()
            self._accounts[acct_id] = st
        return st

    def _find_client(self) -> Any | None:
        for st in self._accounts.values():
            if st.client is not None:
                return st.client
        return None

    def _get_cursors(self, acct_id: str) -> dict[str, str]:
        if not acct_id:
            return {}
        cache = self._dsb_channel_cursors.get(acct_id)
        if cache is None:
            cache = {}
            self._dsb_channel_cursors[acct_id] = cache
        return cache

    async def _load_runtime_state(self, acct_id: str) -> dict:
        state = await self.conductor.db.get_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="discord",
            state_key="poller",
        )
        if state is None:
            self._dsb_channel_cursors.pop(acct_id, None)
            return {}
        state = ext_dict('discord runtime state', state)
        restored: dict[str, str] = {}
        raw_cursors = state.get("channel_cursors")
        if raw_cursors is None:
            raw_cursors = {}
        else:
            raw_cursors = ext_dict('discord channel_cursors', raw_cursors)
        for channel_id, msg_id in raw_cursors.items():
            channel_key = ext_id(channel_id, "channel_id").strip()
            msg_key = ext_id(msg_id, "msg_id").strip()
            if channel_key and msg_key:
                restored[channel_key] = msg_key
        if restored:
            self._dsb_channel_cursors[acct_id] = restored
        else:
            self._dsb_channel_cursors.pop(acct_id, None)
        return state

    async def _save_runtime_state(self, acct_id: str, *, mode: str) -> None:
        cursors = self._get_cursors(acct_id)
        await self.conductor.db.set_local_account_runtime_state(
            acct_id=acct_id,
            acct_type="discord",
            state_key="poller",
            state={
                "mode": mode or "discord",
                "channel_cursors": cursors,
                "updated_at": time.time(),
            },
        )

    @property
    def session(self) -> aiohttp.ClientSession:
        return self.conductor.session

    def _event_attachments(self, atts: list[Any], stickers: list[Any] | None = None) -> tuple[list[dict], str]:
        out: list[dict] = []
        for a in atts or []:
            url_raw = a.url
            if not url_raw:
                url_raw = a.proxy_url
            url = ext_str(url_raw, "url").strip()
            file_id = ext_id(a.id, "id").strip()
            if not url and not file_id:
                continue
            filename = ext_str(a.filename, "filename").strip()
            content_type = ext_str(a.content_type, "content_type").strip()
            item: dict[str, Any] = {
                "type": attachment_type_from_meta(filename=filename, content_type=content_type),
                "file_id": file_id or url,
                "file_name": filename,
                "content_type": content_type,
            }
            if url:
                item["url"] = url
            size = a.size
            if size is not None:
                item["file_size"] = ext_int(size, "size")
            out.append(item)
        for s in stickers or []:
            sid = ext_id(s.id, "id").strip()
            name = ext_str(s.name, "name").strip()
            if not sid and not name:
                continue
            fmt = _discord_sticker_format(s)
            url_str, mime_type, animated = guidance.discord_sticker_url(
                sid, fmt, self._api_base()
            )
            item = {
                "type": "sticker",
                "file_id": sid,
                "file_name": name,
                "content_type": mime_type,
            }
            if url_str:
                item["url"] = url_str
            if animated and not url_str:
                item["is_animated"] = True
            out.append(item)
        kept, notes = guidance.filter_oversized_attachments("discord", out)
        return kept, "\n".join(notes) if notes else ""

    async def _deliver_component_interaction(
        self,
        *,
        acct_id: str,
        interaction_id: str,
        user_id: str,
        channel_id: str,
        message_id: str,
        custom_id: str,
        parent_id: str = "",
        channel_type: int = 0,
    ) -> None:
        custom_id = (custom_id or "").strip()
        channel_id = (channel_id or "").strip()
        user_id = (user_id or "").strip()
        if not custom_id or not channel_id or not user_id:
            return
        chat_id, open_id, chat_type = resolve_chat_and_thread(channel_id, parent_id, channel_type)
        force = custom_id.startswith("/start")
        model_id = None if force else (await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=open_id))
        if not model_id:
            model_id = await self.conductor.ensure_chat_mapping(
                acct_id=acct_id,
                chat_id=open_id,
                chat_type="discord",
                carrier_user_id=user_id,
                force=force,
            )
        if not model_id:
            return
        msg_id = f"interaction:{(interaction_id or '').strip() or channel_id}"
        body = {
            "method": "send_message",
            "params": {"text": custom_id},
            "text": custom_id,
            "caption": "",
            "attachments": [],
            "msg_id": msg_id,
        }
        if message_id:
            body["reply_to"] = message_id
        if chat_type:
            body["chat_type"] = chat_type
        await self.conductor.deliver_inbound(
            model_id=model_id,
            acct_id=acct_id,
            chat_id=chat_id,
            request_id=msg_id,
            body=body,
        )

    async def _ack_dsb_interaction(self, *, token: str, interaction_id: str, interaction_token: str) -> None:
        interaction_id = (interaction_id or "").strip()
        interaction_token = (interaction_token or "").strip()
        if not interaction_id or not interaction_token:
            return
        await self._dsb_request(
            method="POST",
            token=token,
            path=f"/api/v9/interactions/{interaction_id}/{interaction_token}/callback",
            json_body={"type": 6},
        )

    async def _deliver_reaction_payload(self, *, acct_id: str, data: dict, removed: bool) -> None:
        emoji = data.get("emoji")
        if emoji is None:
            emoji = {}
        else:
            emoji = ext_dict('discord reaction emoji', emoji)
        await self._deliver_reaction_fields(
            acct_id=acct_id,
            user_id=ext_id(data.get("user_id"), "user_id").strip(),
            channel_id=ext_id(data.get("channel_id"), "channel_id").strip(),
            message_id=ext_id(data.get("message_id"), "message_id").strip(),
            emoji_name=ext_str(emoji.get("name"), "name").strip(),
            emoji_id=ext_id(emoji.get("id"), "id").strip() if emoji.get("id") not in (None, "") else "",
            removed=removed,
        )

    async def _deliver_reaction(self, *, acct_id: str, payload, removed: bool) -> None:
        if payload is None:
            return
        emoji = payload.emoji
        emoji_name = ""
        emoji_id = ""
        if emoji is not None:
            emoji_name = ext_str(emoji.name, "name").strip()
            if emoji.id is not None:
                emoji_id = ext_id(emoji.id, "id").strip()
        await self._deliver_reaction_fields(
            acct_id=acct_id,
            user_id=ext_id(payload.user_id, "user_id").strip(),
            channel_id=ext_id(payload.channel_id, "channel_id").strip(),
            message_id=ext_id(payload.message_id, "message_id").strip(),
            emoji_name=emoji_name,
            emoji_id=emoji_id,
            removed=removed,
        )

    async def _deliver_reaction_fields(
        self,
        *,
        acct_id: str,
        user_id: str,
        channel_id: str,
        message_id: str,
        emoji_name: str,
        emoji_id: str,
        removed: bool,
    ) -> None:
        if not user_id or not channel_id or not message_id:
            return
        st = self._acct_state(acct_id)
        client = st.client
        if client is not None and client.user is not None and ext_id(client.user.id, "user.id") == user_id:
            return
        if st.gateway_user_id and st.gateway_user_id == user_id:
            return
        text = guidance.format_reaction_text(None, None)
        if not removed:
            if emoji_id:
                text = guidance.format_reaction_text(None, [f"{emoji_name}:{emoji_id}" if emoji_name else emoji_id])
            elif emoji_name:
                text = guidance.format_reaction_text([emoji_name], None)
        msg_id = f"reaction:{channel_id}:{message_id}:{user_id}:{emoji_id}{emoji_name}"
        if removed:
            msg_id = msg_id + ":removed"
        acct = await self.conductor.db.get_local_account(acct_id)
        token = ""
        if acct:
            enc = (acct.get("encrypted_token") or "").strip()
            token, _ = decrypt_if_encrypted(self.conductor.private_key, enc)
        chat_id, open_id, chat_type = await self.resolve_message_chat_thread(token=token, channel_id=channel_id)
        model_id = await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=open_id)
        if not model_id:
            model_id = await self.conductor.ensure_chat_mapping(
                acct_id=acct_id,
                chat_id=open_id,
                chat_type="discord",
                carrier_user_id=user_id,
                force=False,
            )
        if not model_id:
            return
        body = {
            "method": "send_message",
            "params": {"text": text},
            "text": text,
            "caption": "",
            "attachments": [],
            "msg_id": msg_id,
            "reply_to": message_id,
        }
        if chat_type:
            body["chat_type"] = chat_type
        await self.conductor.deliver_inbound(
            model_id=model_id,
            acct_id=acct_id,
            chat_id=chat_id,
            request_id=msg_id,
            body=body,
        )

    def _api_base(self) -> str:
        raw = self.conductor.config.discord_api_url.strip()
        if raw:
            return raw.rstrip("/")
        for k in ("DISCORD_API_URL", "DSB_API_URL"):
            v = (os.environ.get(k) or "").strip()
            if v:
                return v.rstrip("/")
        return ""

    async def _dsb_request(self, *, method: str, token: str, path: str, params: dict | None = None, json_body: dict | None = None) -> tuple[int, Any]:
        base = self._api_base()
        if not base:
            return 0, None
        url = f"{base}{path}"
        headers = {"Authorization": "Bot " + (token or "").strip()}
        async with self.session.request(method.upper(), url, headers=headers, params=params, json=json_body) as resp:
            status = int(resp.status)
            try:
                body = await resp.json(content_type=None)
            except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                body = await resp.text()
            return status, body

    def _dsb_attachments(self, atts: list[Any], stickers: list[Any] | None = None) -> tuple[list[dict], str]:
        out: list[dict] = []
        for a in atts:
            a = ext_dict('discord attachment', a)
            url_raw = a.get("url")
            if not url_raw:
                url_raw = a.get("proxy_url")
            url = ext_str(url_raw, "url").strip()
            file_id = ext_id(a.get("id"), "id").strip()
            if not url and not file_id:
                continue
            filename = ext_str(a.get("filename"), "filename").strip()
            content_type = ext_str(a.get("content_type"), "content_type").strip()
            item: dict[str, Any] = {
                "type": attachment_type_from_meta(filename=filename, content_type=content_type),
                "file_id": file_id or url,
                "file_name": filename,
                "content_type": content_type,
            }
            if url:
                item["url"] = url
            size = a.get("size")
            if size is not None:
                item["file_size"] = ext_int(size, "size")
            out.append(item)
        for s in stickers or []:
            s = ext_dict('discord sticker', s)
            sid = ext_id(s.get("id"), "id").strip()
            name = ext_str(s.get("name"), "name").strip()
            if not sid and not name:
                continue
            fmt = s.get("format_type")
            if fmt is None:
                fmt = 1
            else:
                fmt = ext_int(fmt, "format_type")
            url_str, mime_type, animated = guidance.discord_sticker_url(
                sid, fmt, self._api_base()
            )
            item = {
                "type": "sticker",
                "file_id": sid,
                "file_name": name,
                "content_type": mime_type,
            }
            if url_str:
                item["url"] = url_str
            if animated and not url_str:
                item["is_animated"] = True
            out.append(item)
        kept, notes = guidance.filter_oversized_attachments("discord", out)
        return kept, "\n".join(notes) if notes else ""

    async def get_channel_info(self, *, token: str, channel_id: str) -> dict | None:
        channel_id = send_channel_id(channel_id)
        if not channel_id:
            return None
        api_base = self._api_base()
        if api_base:
            status, payload = await self._dsb_request(
                method="GET",
                token=token,
                path=f"/api/v9/channels/{channel_id}",
            )
            if status < 200 or status >= 300 or not isinstance(payload, dict):
                return None
            return {
                "id": (ext_id(payload.get("id"), "id") or channel_id).strip(),
                "type": ext_int(payload.get("type"), "type"),
                "guild_id": ext_id(payload.get("guild_id"), "guild_id").strip(),
                "parent_id": ext_id(payload.get("parent_id"), "parent_id").strip(),
            }
        import discord
        chan = await self._get_channel(discord=discord, token=token, channel_id=channel_id)
        if chan is None:
            return None
        channel_type = int(chan.type)
        guild = chan.guild
        guild_id = ext_id(guild.id, "id").strip() if guild is not None else ""
        parent_id = _discord_channel_parent_id(chan)
        return {
            "id": (ext_id(chan.id, "id") or channel_id).strip(),
            "type": channel_type,
            "guild_id": guild_id,
            "parent_id": parent_id,
        }

    async def resolve_message_chat_thread(self, *, token: str, channel_id: str, channel_meta=None):
        channel_id = (channel_id or "").strip()
        if channel_meta is None:
            channel_meta = {}
        parent_id = ext_id(channel_meta.get("parent_id"), "parent_id").strip()
        channel_type = channel_meta.get("type", 0)
        if not parent_id and channel_id:
            info = await self.get_channel_info(token=token, channel_id=channel_id)
            if info is not None:
                parent_id = ext_id(info.get("parent_id"), "parent_id").strip()
                channel_type = info.get("type", channel_type)
        return resolve_chat_and_thread(channel_id, parent_id, channel_type)

    async def send_typing(self, *, token: str, channel_id: str) -> bool:
        channel_id = send_channel_id(channel_id)
        api_base = self._api_base()
        if api_base:
            if not channel_id:
                return False
            status, _ = await self._dsb_request(
                method="POST",
                token=token,
                path=f"/api/v9/channels/{channel_id}/typing",
            )
            return 200 <= int(status) < 300
        import discord
        chan = await self._get_channel(discord=discord, token=token, channel_id=channel_id)
        if chan is None:
            return False
        await chan.trigger_typing()
        return True

    async def _get_channel(self, *, discord: Any, token: str, channel_id: str) -> Any | None:
        channel_id = (channel_id or "").strip()
        if not channel_id:
            return None
        cid = int(channel_id)
        client = self._find_client()
        if client is not None:
            ch = client.get_channel(cid)
            if ch is not None:
                return ch
        intents = discord.Intents.none()
        intents.guilds = True
        temp = discord.Client(intents=intents)
        try:
            await temp.login(token)
            ch = await temp.fetch_channel(cid)
            return ch
        finally:
            try:
                await temp.close()
            except Exception as e:
                logger.error("unexpected where=discord_temp_client_close error=%s", e, exc_info=e)

    def _discord_allowed_mentions_obj(self, discord: Any) -> Any:
        return discord.AllowedMentions(
            everyone=False,
            users=True,
            roles=False,
            replied_user=True,
        )

    async def send_message(
        self,
        *,
        token: str,
        channel_id: str,
        content: str,
        file_bytes: bytes | None = None,
        filename: str | None = None,
        reply_to: str | None = None,
        keyboard=None,
    ) -> bool:
        channel_id = send_channel_id(channel_id)
        api_base = self._api_base()
        if file_bytes is not None:
            try:
                guidance.check_media_size("discord", len(file_bytes), "document")
            except guidance.MediaTooLargeError:
                return False
        chunks = guidance.prepare_outbound_text("discord", (content or ""))
        if len(chunks) > guidance.MAX_DISCORD_SPLIT_MESSAGES:
            chunks = chunks[: guidance.MAX_DISCORD_SPLIT_MESSAGES]
        components = discord_components(keyboard) if parse_keyboard(keyboard) else None
        if file_bytes is None and not chunks and components is None:
            return False
        if not chunks:
            chunks = [""]
        if api_base:
            channel_id = (channel_id or "").strip()
            if not channel_id:
                return False
            last_ok = False
            for idx, chunk in enumerate(chunks):
                use_reply = reply_to if idx == 0 else None
                use_file = file_bytes if idx == 0 else None
                use_name = filename if idx == 0 else None
                ok = await self._dsb_send_message(
                    token=token,
                    channel_id=channel_id,
                    content=chunk,
                    reply_to=use_reply,
                    file_bytes=use_file,
                    filename=use_name,
                    components=components if idx == 0 else None,
                )
                if not ok:
                    return False
                last_ok = True
            return last_ok
        import discord
        chan = await self._get_channel(discord=discord, token=token, channel_id=channel_id)
        if chan is None:
            return False
        allowed = self._discord_allowed_mentions_obj(discord)
        for idx, chunk in enumerate(chunks):
            kwargs: dict[str, Any] = {"content": chunk, "allowed_mentions": allowed}
            if idx == 0 and components:
                view = discord.ui.View(timeout=None)
                for row_idx, row in enumerate(components):
                    for item in row.get("components") or []:
                        label = ext_str(item.get("label"), "label")
                        style = ext_int(item.get("style"), "style")
                        if style == 5:
                            btn = discord.ui.Button(style=discord.ButtonStyle.link, label=label, url=ext_str(item.get("url"), "url"))
                        else:
                            btn = discord.ui.Button(
                                style=discord.ButtonStyle.primary,
                                label=label,
                                custom_id=ext_str(item.get("custom_id"), "custom_id"),
                            )
                        btn.row = row_idx
                        view.add_item(btn)
                kwargs["view"] = view
            if idx == 0 and reply_to:
                ref_id = int(reply_to.strip())
                kwargs["reference"] = discord.MessageReference(
                    message_id=ref_id,
                    channel_id=int(chan.id) if chan.id else None,
                    fail_if_not_exists=False,
                )
            if idx == 0 and file_bytes is not None:
                kwargs["file"] = discord.File(fp=io.BytesIO(file_bytes), filename=(filename or "file"))
            try:
                await chan.send(**kwargs)
            except discord.HTTPException as e:
                if idx == 0 and reply_to and (e.code == 10008 or "system message" in str(e).lower() or "unknown message" in str(e).lower()):
                    kwargs.pop("reference", None)
                    await chan.send(**kwargs)
                else:
                    raise
        return True

    async def _dsb_send_message(
        self,
        *,
        token: str,
        channel_id: str,
        content: str,
        reply_to: str | None,
        file_bytes: bytes | None,
        filename: str | None,
        components=None,
    ) -> bool:
        base = self._api_base()
        if not base:
            return False
        url = f"{base}/api/v9/channels/{channel_id}/messages"
        headers = {"Authorization": "Bot " + (token or "").strip()}
        allowed = guidance.default_discord_allowed_mentions()
        if file_bytes is None:
            payload: dict[str, Any] = {
                "content": (content or ""),
                "allowed_mentions": allowed,
            }
            if reply_to:
                payload["message_reference"] = {"message_id": reply_to.strip()}
            if components:
                payload["components"] = components
            async with self.session.post(url, headers=headers, json=payload) as resp:
                status = int(resp.status)
                err_body = await resp.text()
            if 200 <= status < 300:
                return True
            if reply_to and self._is_unknown_reply_error(status, err_body):
                payload.pop("message_reference", None)
                async with self.session.post(url, headers=headers, json=payload) as resp:
                    return 200 <= int(resp.status) < 300
            return False
        payload = {
            "content": (content or ""),
            "allowed_mentions": allowed,
        }
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to.strip()}
        if components:
            payload["components"] = components
        data = aiohttp.FormData()
        data.add_field("payload_json", json.dumps(payload), content_type="application/json")
        data.add_field("files[0]", file_bytes, filename=(filename or "file"), content_type="application/octet-stream")
        async with self.session.post(url, headers=headers, data=data) as resp:
            status = int(resp.status)
            err_body = await resp.text()
        if 200 <= status < 300:
            return True
        if reply_to and self._is_unknown_reply_error(status, err_body):
            payload.pop("message_reference", None)
            data = aiohttp.FormData()
            data.add_field("payload_json", json.dumps(payload), content_type="application/json")
            data.add_field("files[0]", file_bytes, filename=(filename or "file"), content_type="application/octet-stream")
            async with self.session.post(url, headers=headers, data=data) as resp:
                return 200 <= int(resp.status) < 300
        return False

    @staticmethod
    def _is_unknown_reply_error(status: int, body: str) -> bool:
        raw = (body or "").lower()
        if "10008" in raw or "unknown message" in raw or "system message" in raw:
            return True
        return status == 400 and "message_reference" in raw

    async def download(self, *, token: str, url: str) -> bytes | None:
        url = (url or "").strip()
        if not url:
            return None
        limit = guidance.max_media_bytes("discord", "document")
        headers = {}
        tok = (token or "").strip()
        if tok:
            if tok.startswith("Bot ") or tok.startswith("Bearer "):
                headers["Authorization"] = tok
            else:
                headers["Authorization"] = "Bot " + tok
        max_redirects = 10
        current = url
        for _ in range(max_redirects + 1):
            async with self.session.get(current, headers=headers, allow_redirects=False) as resp:
                status = int(resp.status)
                if status in (301, 302, 303, 307, 308):
                    loc = ext_str(resp.headers.get("Location"), "Location").strip()
                    if not loc:
                        return None
                    from urllib.parse import urljoin

                    current = urljoin(current, loc)
                    continue
                if status != 200:
                    return None
                cl = resp.headers.get("Content-Length")
                if cl:
                    try:
                        guidance.check_media_size("discord", int(cl), "document")
                    except guidance.MediaTooLargeError:
                        return None
                data = await resp.content.read(limit + 1)
                if len(data) > limit:
                    return None
                return data
        return None

    async def _relay_discord_voice(
        self, *, acct_id: str, relay_chat_id: str, user_id: str, event_type: str, content: dict
    ) -> None:
        if not (acct_id and relay_chat_id):
            return
        model_id = await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=open_chat_id(relay_chat_id))
        if not model_id:
            model_id = await self.conductor.ensure_chat_mapping(
                acct_id=acct_id,
                chat_id=open_chat_id(relay_chat_id),
                chat_type="discord",
                carrier_user_id=user_id or None,
            )
        if not model_id:
            return
        update_id = f"{event_type}:{relay_chat_id}:{int(time.time() * 1000)}"
        body = {
            "discord_voice": {
                "type": event_type,
                "content": content,
            },
            "msg_id": update_id,
        }
        await self.conductor.deliver_inbound(
            model_id=model_id,
            acct_id=acct_id,
            chat_id=relay_chat_id,
            request_id=update_id,
            body=body,
        )

    async def _resolve_guild_id(self, *, acct_id: str, token: str, channel_id: str, relay_chat_id: str) -> str:
        channel_id = (channel_id or "").strip()
        relay_chat_id = (relay_chat_id or "").strip()
        if channel_id:
            info = await self.get_channel_info(token=token, channel_id=channel_id)
            if info is not None:
                guild_id = ext_id(info.get("guild_id"), "guild_id").strip()
                if guild_id:
                    return guild_id
        st = self._acct_state(acct_id) if acct_id else None
        client = st.client if st is not None else self._find_client()
        if client is not None and channel_id:
            ch = client.get_channel(int(channel_id))
            if ch is None:
                ch = await client.fetch_channel(int(channel_id))
            guild = ch.guild
            guild_id = ext_id(guild.id, "id").strip() if guild is not None else ""
            if guild_id:
                return guild_id
        if st is not None:
            for gid, pending in st.voice_pending.items():
                if ext_str(pending.get("relay_chat_id"), "relay_chat_id").strip() == relay_chat_id:
                    return ext_id(gid, "guild_id")
        return ""

    async def _ensure_dsb_gateway(self, *, acct_id: str, token: str) -> bool:
        if not (acct_id and token):
            return False
        st = self._acct_state(acct_id)
        if (
            st.gateway_ws is not None
            and not st.gateway_ws.closed
            and st.gateway_ready.is_set()
            and st.gateway_token == token
        ):
            return True
        if st.gateway_task is not None and not st.gateway_task.done():
            if st.gateway_token and st.gateway_token != token:
                st.gateway_task.cancel()
                try:
                    await st.gateway_task
                except Exception as e:
                    logger.error("unexpected where=discord_gateway_task_cancel acct_id=%s error=%s", acct_id, e, exc_info=e)
                st.gateway_task = None
            else:
                try:
                    await asyncio.wait_for(st.gateway_ready.wait(), timeout=10)
                    return (
                        st.gateway_ws is not None
                        and not st.gateway_ws.closed
                        and st.gateway_token == token
                    )
                except asyncio.TimeoutError:
                    return False
        st.gateway_token = token
        st.gateway_ready.clear()
        st.gateway_task = asyncio.create_task(self._dsb_gateway_loop(acct_id=acct_id, token=token))
        try:
            await asyncio.wait_for(st.gateway_ready.wait(), timeout=10)
        except asyncio.TimeoutError:
            return False
        return st.gateway_ws is not None and not st.gateway_ws.closed and st.gateway_token == token

    async def _dsb_gateway_loop(self, *, acct_id: str, token: str) -> None:
        st = self._acct_state(acct_id)
        base = self._api_base()
        if not base:
            return
        status, payload = await self._dsb_request(method="GET", token=token, path="/api/v9/gateway/bot")
        gateway_url = ""
        if status == 200 and isinstance(payload, dict):
            gateway_url = ext_str(payload.get("url"), "url").strip()
        if not gateway_url:
            if base.startswith("https://"):
                gateway_url = "wss://" + base[len("https://"):] + "/gateway"
            elif base.startswith("http://"):
                gateway_url = "ws://" + base[len("http://"):] + "/gateway"
            else:
                gateway_url = "ws://" + base + "/gateway"
        ws = None
        heartbeat_task = None
        try:
            try:
                ws = await self.session.ws_connect(gateway_url, heartbeat=None)
            except (aiohttp.ClientError, OSError) as e:
                logger.error("unexpected where=discord_gateway_connect acct_id=%s error=%s", acct_id, e, exc_info=e)
                return
            st.gateway_ws = ws
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    continue
                try:
                    frame = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                frame = ext_dict('discord gateway frame', frame)
                op = frame.get("op")
                if op == 10:
                    d = frame.get("d")
                    d = ext_dict('discord hello d', d)
                    interval = float(d.get("heartbeat_interval") or 0) / 1000.0
                    await ws.send_json({
                        "op": 2,
                        "d": {
                            "token": token,
                            "properties": {"os": "linux", "browser": "station", "device": "station"},
                            "intents": 129 | 1024 | 8192,
                        },
                    })
                    if interval > 0:
                        if heartbeat_task is not None:
                            heartbeat_task.cancel()
                        async def _hb(ws=ws, interval=interval):
                            while True:
                                await asyncio.sleep(interval)
                                await ws.send_json({"op": 1, "d": None})
                        heartbeat_task = asyncio.create_task(_hb())
                    continue
                if op == 11:
                    continue
                if op == 1:
                    await ws.send_json({"op": 1, "d": None})
                    continue
                if op != 0:
                    continue
                event_type = ext_str(frame.get("t"), "t").strip()
                data = frame.get("d")
                data = ext_dict('discord dispatch d', data)
                if event_type == "MESSAGE_REACTION_ADD":
                    await self._deliver_reaction_payload(acct_id=acct_id, data=data, removed=False)
                    continue
                if event_type == "MESSAGE_REACTION_REMOVE":
                    await self._deliver_reaction_payload(acct_id=acct_id, data=data, removed=True)
                    continue
                if event_type == "INTERACTION_CREATE":
                    if ext_int(data.get("type"), "type") != 3:
                        continue
                    interaction_id = ext_id(data.get("id"), "id").strip()
                    interaction_token = ext_str(data.get("token"), "token").strip()
                    await self._ack_dsb_interaction(
                        token=token,
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                    )
                    inner = data.get("data")
                    inner = ext_dict("discord interaction data", inner) if inner is not None else {}
                    custom_id = ext_str(inner.get("custom_id"), "custom_id").strip()
                    member = data.get("member")
                    user = data.get("user")
                    user_id = ""
                    if member is not None:
                        member = ext_dict("discord interaction member", member)
                        muser = member.get("user")
                        if muser is not None:
                            muser = ext_dict("discord interaction member user", muser)
                            user_id = ext_id(muser.get("id"), "id").strip()
                    if not user_id and user is not None:
                        user = ext_dict("discord interaction user", user)
                        user_id = ext_id(user.get("id"), "id").strip()
                    message = data.get("message")
                    message_id = ""
                    parent_id = ""
                    channel_type = 0
                    if message is not None:
                        message = ext_dict("discord interaction message", message)
                        message_id = ext_id(message.get("id"), "id").strip()
                    await self._deliver_component_interaction(
                        acct_id=acct_id,
                        interaction_id=interaction_id,
                        user_id=user_id,
                        channel_id=ext_id(data.get("channel_id"), "channel_id").strip(),
                        message_id=message_id,
                        custom_id=custom_id,
                        parent_id=parent_id,
                        channel_type=channel_type,
                    )
                    continue
                if event_type == "READY":
                    user = data.get("user")
                    user = ext_dict('discord READY user', user)
                    st.gateway_user_id = ext_id(user.get("id"), "id").strip()
                    st.gateway_session_id = ext_str(data.get("session_id"), "session_id", default="dsb_session").strip() or "dsb_session"
                    st.gateway_ready.set()
                    continue
                if event_type == "VOICE_STATE_UPDATE":
                    user_id = ext_id(data.get("user_id"), "user_id").strip()
                    if st.gateway_user_id and user_id and user_id != st.gateway_user_id:
                        continue
                    guild_id = ext_id(data.get("guild_id"), "guild_id").strip()
                    if not guild_id:
                        continue
                    channel_id = ext_id(data.get("channel_id"), "channel_id").strip()
                    session_id = ext_str(data.get("session_id") or st.gateway_session_id, "session_id").strip()
                    pending = st.voice_pending.get(guild_id)
                    if pending is None:
                        pending = {"guild_id": guild_id}
                    pending["user_id"] = user_id or st.gateway_user_id
                    pending["session_id"] = session_id
                    pending["channel_id"] = channel_id
                    if not pending.get("relay_chat_id") and channel_id:
                        pending["relay_chat_id"] = channel_id
                    st.voice_pending[guild_id] = pending
                    if not channel_id:
                        await self._relay_discord_voice(
                            acct_id=acct_id,
                            relay_chat_id=(ext_str(pending.get("relay_chat_id"), "relay_chat_id") or guild_id),
                            user_id=ext_id(pending.get("user_id"), "user_id"),
                            event_type="discord.voice.left",
                            content={"guild_id": guild_id},
                        )
                        continue
                    self._maybe_emit_credentials(acct_id, guild_id)
                    continue
                if event_type == "VOICE_SERVER_UPDATE":
                    guild_id = ext_id(data.get("guild_id"), "guild_id").strip()
                    endpoint = ext_str(data.get("endpoint"), "endpoint").strip()
                    token_v = ext_str(data.get("token"), "token").strip()
                    if not (guild_id and endpoint and token_v):
                        continue
                    pending = st.voice_pending.get(guild_id)
                    if pending is None:
                        pending = {"guild_id": guild_id}
                    pending["endpoint"] = endpoint
                    pending["token"] = token_v
                    if not pending.get("user_id"):
                        pending["user_id"] = st.gateway_user_id
                    if not pending.get("session_id"):
                        pending["session_id"] = st.gateway_session_id or "dsb_session"
                    st.voice_pending[guild_id] = pending
                    self._maybe_emit_credentials(acct_id, guild_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("unexpected where=discord_gateway acct_id=%s error=%s", acct_id, e, exc_info=e)
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            st.gateway_ready.clear()
            if ws is not None:
                try:
                    await ws.close()
                except Exception as e:
                    logger.error("unexpected where=discord_gateway_close acct_id=%s error=%s", acct_id, e, exc_info=e)
            if st.gateway_ws is ws:
                st.gateway_ws = None

    async def voice_state_update(
        self,
        *,
        token: str,
        acct_id: str,
        relay_chat_id: str,
        guild_id: str | None,
        channel_id: str | None,
        self_mute: bool = False,
        self_deaf: bool = False,
    ) -> bool:
        guild_id = (guild_id or "").strip()
        channel_id = (channel_id or "").strip()
        relay_chat_id = (relay_chat_id or "").strip()
        token = (token or "").strip()
        if not acct_id:
            return False
        st = self._acct_state(acct_id)
        if not guild_id:
            guild_id = await self._resolve_guild_id(
                acct_id=acct_id, token=token, channel_id=channel_id, relay_chat_id=relay_chat_id
            )
        if not guild_id:
            return False
        if not relay_chat_id:
            relay_chat_id = channel_id or guild_id
        pending = st.voice_pending.get(guild_id)
        if pending is None:
            pending = {}
        pending.update({
            "relay_chat_id": relay_chat_id,
            "guild_id": guild_id,
            "channel_id": channel_id,
        })
        if not channel_id:
            pending["session_id"] = ""
            pending["endpoint"] = ""
            pending["token"] = ""
        st.voice_pending[guild_id] = pending

        client = st.client
        if client is not None and client.ws is not None:
            await client.ws.voice_state(
                int(guild_id),
                int(channel_id) if channel_id else None,
                self_mute=self_mute,
                self_deaf=self_deaf,
            )
            return True

        if self._api_base():
            if not await self._ensure_dsb_gateway(acct_id=acct_id, token=token):
                return False
            ws = st.gateway_ws
            if ws is None or ws.closed:
                return False
            await ws.send_json({
                "op": 4,
                "d": {
                    "guild_id": guild_id,
                    "channel_id": channel_id or None,
                    "self_mute": self_mute,
                    "self_deaf": self_deaf,
                },
            })
            return True
        return False

    def _maybe_emit_credentials(self, acct_id: str, guild_id: str) -> None:
        if not acct_id:
            return
        st = self._acct_state(acct_id)
        pending = st.voice_pending.get(guild_id)
        if pending is None:
            return
        relay_chat_id = ext_str(pending.get("relay_chat_id"), "relay_chat_id").strip()
        channel_id = ext_id(pending.get("channel_id"), "channel_id").strip()
        session_id = ext_str(pending.get("session_id"), "session_id").strip()
        endpoint = ext_str(pending.get("endpoint"), "endpoint").strip()
        token = ext_str(pending.get("token"), "token").strip()
        user_id = ext_id(pending.get("user_id"), "user_id").strip()
        if not (relay_chat_id and channel_id and session_id and endpoint and token and user_id):
            return
        asyncio.create_task(
            self._relay_discord_voice(
                acct_id=acct_id,
                relay_chat_id=relay_chat_id,
                user_id=user_id,
                event_type="discord.voice.credentials",
                content={
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "endpoint": endpoint,
                    "token": token,
                    "session_id": session_id,
                    "user_id": user_id,
                },
            )
        )

    async def poller_loop(self, acct_id: str) -> None:
        acct = await self.conductor.db.get_local_account(acct_id)
        if not acct:
            return
        await self._load_runtime_state(acct_id)
        enc = (acct.get("encrypted_token") or "").strip()
        token, _ = decrypt_if_encrypted(self.conductor.private_key, enc)
        token = (token or "").strip()
        if not token:
            return

        st = self._acct_state(acct_id)
        api_base = self._api_base()
        if api_base:
            await self._save_runtime_state(acct_id, mode="dsb")
            await self._ensure_dsb_gateway(acct_id=acct_id, token=token)
            try:
                await self._dsb_poller_loop(acct_id=acct_id, token=token)
            finally:
                if st.gateway_task is not None:
                    st.gateway_task.cancel()
                    try:
                        await st.gateway_task
                    except Exception as e:
                        logger.error("unexpected where=discord_poller_gateway_cancel acct_id=%s error=%s", acct_id, e, exc_info=e)
                    st.gateway_task = None
                if st.gateway_ws is not None:
                    try:
                        await st.gateway_ws.close()
                    except Exception as e:
                        logger.error("unexpected where=discord_poller_gateway_close acct_id=%s error=%s", acct_id, e, exc_info=e)
                    st.gateway_ws = None
            return
        await self._save_runtime_state(acct_id, mode="gateway")
        while not self.conductor.poller_stop.is_set():
            try:
                await self._gateway_client_session(acct_id=acct_id, token=token)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=discord_gateway_session acct_id=%s error=%s", acct_id, e, exc_info=e)
            if self.conductor.poller_stop.is_set():
                break
            try:
                await asyncio.wait_for(self.conductor.poller_stop.wait(), timeout=3.0)
                break
            except asyncio.TimeoutError:
                pass

    async def _gateway_client_session(self, *, acct_id: str, token: str) -> None:
        import discord

        st = self._acct_state(acct_id)
        intents = discord.Intents.none()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        intents.reactions = True
        intents.voice_states = True
        client = discord.Client(intents=intents)
        st.client = client

        @client.event
        async def on_message(message):
            if message.author.bot or message.author.system:
                return
            if client.user is not None and message.author.id == client.user.id:
                return
            if message.type not in (discord.MessageType.default, discord.MessageType.reply):
                return
            channel_id = ext_id(message.channel.id, "channel.id").strip()
            if not channel_id:
                return
            parent_id = _discord_channel_parent_id(message.channel)
            channel_type = int(message.channel.type)
            chat_id, open_id, chat_type = resolve_chat_and_thread(channel_id, parent_id, channel_type)
            msg_id = ext_id(message.id, "id").strip()
            author_id = ext_id(message.author.id, "author.id").strip() or None
            content = ext_str(message.content, "content").strip()
            force = content.startswith("/start")
            model_id = None if force else (await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=open_id))
            if not model_id:
                model_id = await self.conductor.ensure_chat_mapping(acct_id=acct_id, chat_id=open_id, chat_type="discord", carrier_user_id=author_id, force=force)
            if not model_id:
                return
            attachments, size_note = self._event_attachments(
                list(message.attachments or []),
                stickers=list(message.stickers or []),
            )
            if size_note:
                content = f"{content}\n{size_note}".strip() if content else size_note
            reply_to = ""
            reply_snippet = ""
            ref = message.reference
            if ref is not None:
                reply_to = ext_id(ref.message_id, "message_id").strip()
                resolved = ref.resolved
                if resolved is not None:
                    reply_snippet = ext_str(resolved.content, "content").strip()
                    if not reply_snippet:
                        atts = resolved.attachments or []
                        stickers = resolved.stickers or []
                        if atts or stickers:
                            reply_snippet = "(attachment)"
            reply_to_text = guidance.reply_snippet(reply_snippet)
            content = guidance.inject_reply_context(content, reply_to_text)
            if not content and attachments:
                for a in attachments:
                    if a.get("type") == "sticker":
                        name = ext_str(a.get("file_name"), "file_name").strip()
                        if a.get("is_animated") or not ext_str(a.get("url"), "url").strip():
                            content = guidance.animated_sticker_injection(name)
                        else:
                            content = guidance.sticker_injection(name or "a sticker", "", "")
                        break
            body = {
                "method": "send_message",
                "params": {"text": content},
                "text": content,
                "caption": "",
                "attachments": attachments,
                "msg_id": msg_id,
            }
            if reply_to:
                body["reply_to"] = reply_to
            if reply_to_text:
                body["reply_to_text"] = reply_to_text
            if chat_type:
                body["chat_type"] = chat_type
            if not content and not attachments:
                body["raw"] = {
                    "id": msg_id,
                    "channel_id": channel_id,
                    "content": ext_str(message.content, "content"),
                    "type": int(message.type),
                    "author": {"id": author_id or "", "bot": bool(message.author.bot)},
                }
            await self.conductor.deliver_inbound(
                model_id=model_id,
                acct_id=acct_id,
                chat_id=chat_id,
                request_id=msg_id,
                body=body,
            )

        @client.event
        async def on_interaction(interaction):
            try:
                itype = int(interaction.type)
            except (TypeError, ValueError):
                return
            if itype != 3:
                return
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception as e:
                logger.error("unexpected where=discord_interaction_ack acct_id=%s error=%s", acct_id, e, exc_info=e)
            data = interaction.data or {}
            if not isinstance(data, dict):
                data = {}
            custom_id = ext_str(data.get("custom_id"), "custom_id").strip()
            user = interaction.user
            user_id = ext_id(user.id, "user.id").strip() if user is not None else ""
            channel = interaction.channel
            channel_id = ext_id(interaction.channel_id, "channel_id").strip()
            parent_id = _discord_channel_parent_id(channel) if channel is not None else ""
            channel_type = int(channel.type) if channel is not None else 0
            message = interaction.message
            message_id = ext_id(message.id, "id").strip() if message is not None else ""
            await self._deliver_component_interaction(
                acct_id=acct_id,
                interaction_id=ext_id(interaction.id, "id").strip(),
                user_id=user_id,
                channel_id=channel_id,
                message_id=message_id,
                custom_id=custom_id,
                parent_id=parent_id,
                channel_type=channel_type,
            )

        @client.event
        async def on_raw_reaction_add(payload):
            await self._deliver_reaction(acct_id=acct_id, payload=payload, removed=False)

        @client.event
        async def on_raw_reaction_remove(payload):
            await self._deliver_reaction(acct_id=acct_id, payload=payload, removed=True)

        @client.event
        async def on_message_edit(before, after):
            return

        @client.event
        async def on_voice_state_update(member, before, after):
            if client.user is None or member.id != client.user.id:
                return
            guild_id = ext_id(member.guild.id, "guild.id").strip()
            if not guild_id:
                return
            channel_id = ext_id(after.channel.id, "channel.id").strip() if after.channel is not None else ""
            session_id = ext_id(after.session_id, "session_id").strip()
            pending = st.voice_pending.get(guild_id)
            if pending is None:
                pending = {"guild_id": guild_id}
            pending["user_id"] = ext_id(member.id, "member.id")
            pending["session_id"] = session_id
            pending["channel_id"] = channel_id
            if not pending.get("relay_chat_id") and channel_id:
                pending["relay_chat_id"] = channel_id
            st.voice_pending[guild_id] = pending
            if not channel_id:
                await self._relay_discord_voice(
                    acct_id=acct_id,
                    relay_chat_id=(ext_str(pending.get("relay_chat_id"), "relay_chat_id") or guild_id),
                    user_id=ext_id(member.id, "member.id"),
                    event_type="discord.voice.left",
                    content={"guild_id": guild_id},
                )
                return
            self._maybe_emit_credentials(acct_id, guild_id)

        state = client._connection
        original_voice_server = state.parse_voice_server_update

        def parse_voice_server_update(data):
            data = ext_dict('discord voice_server_update', data)
            guild_id = ext_id(data.get("guild_id"), "guild_id").strip()
            endpoint = ext_str(data.get("endpoint"), "endpoint").strip()
            token_v = ext_str(data.get("token"), "token").strip()
            if guild_id and endpoint and token_v:
                pending = st.voice_pending.get(guild_id)
                if pending is None:
                    pending = {"guild_id": guild_id}
                pending["endpoint"] = endpoint
                pending["token"] = token_v
                if not pending.get("user_id") and client.user is not None:
                    pending["user_id"] = ext_id(client.user.id, "user.id")
                st.voice_pending[guild_id] = pending
                self._maybe_emit_credentials(acct_id, guild_id)
            return original_voice_server(data)

        state.parse_voice_server_update = parse_voice_server_update

        st.client_task = asyncio.create_task(client.start(token))
        liveness_failures = 0
        liveness_interval = 15.0
        liveness_threshold = 3
        heartbeat_ack_max_age = 90.0
        max_latency = 30.0
        try:
            ready_deadline = time.time() + 30.0
            while not self.conductor.poller_stop.is_set():
                if client.is_ready():
                    break
                if st.client_task.done():
                    exc = st.client_task.exception() if not st.client_task.cancelled() else None
                    raise RuntimeError(f"discord client exited before ready: {exc}")
                if time.time() >= ready_deadline:
                    raise RuntimeError("discord gateway ready timeout")
                await asyncio.sleep(0.25)
            while not self.conductor.poller_stop.is_set():
                if st.client_task is not None and st.client_task.done():
                    exc = st.client_task.exception() if not st.client_task.cancelled() else None
                    raise RuntimeError(f"discord session dead after ready: {exc}")
                healthy, reason = self._discord_gateway_health(
                    client,
                    heartbeat_ack_max_age=heartbeat_ack_max_age,
                    max_latency=max_latency,
                )
                if healthy:
                    liveness_failures = 0
                else:
                    liveness_failures += 1
                    logger.warning(
                        "discord gateway unhealthy acct_id=%s reason=%s failures=%s/%s",
                        acct_id,
                        reason,
                        liveness_failures,
                        liveness_threshold,
                    )
                    if liveness_failures >= liveness_threshold:
                        raise RuntimeError(f"discord gateway health failed: {reason}")
                await asyncio.sleep(liveness_interval)
        finally:
            try:
                await client.close()
            except Exception as e:
                logger.error("unexpected where=discord_client_close acct_id=%s error=%s", acct_id, e, exc_info=e)
            st.client = None
            if st.client_task is not None:
                st.client_task.cancel()
                st.client_task = None

    @staticmethod
    def _discord_gateway_health(client, *, heartbeat_ack_max_age: float, max_latency: float) -> tuple[bool, str]:
        if not client.is_ready():
            return False, "not_ready"
        if client.is_closed():
            return False, "client_closed"
        ws = client.ws
        if ws is None:
            return False, "socket_closed"
        if not ws.open:
            return False, "socket_closed"
        keep_alive = ws._keep_alive
        last_ack = None
        if keep_alive is not None:
            last_ack = keep_alive._last_ack
        if last_ack is not None:
            ack_age = time.perf_counter() - float(last_ack)
            if ack_age > heartbeat_ack_max_age:
                return False, "ack_stale"
        latency = client.latency
        if latency is not None:
            if latency < 0 or latency > max_latency:
                return False, "latency_exceeded"
        return True, "healthy"

    async def _dsb_poller_loop(self, *, acct_id: str, token: str) -> None:
        refresh_at = 0.0
        channel_ids: list[str] = []
        channel_meta: dict[str, dict] = {}
        await self._load_runtime_state(acct_id)
        st = self._acct_state(acct_id)
        if not st.gateway_user_id:
            status, me = await self._dsb_request(method="GET", token=token, path="/api/v9/users/@me")
            if status == 200 and isinstance(me, dict):
                st.gateway_user_id = ext_id(me.get("id"), "id").strip()
        while not self.conductor.poller_stop.is_set():
            try:
                now = time.time()
                state_dirty = False
                cursors = self._get_cursors(acct_id)
                if now >= refresh_at:
                    refresh_at = now + 2.0
                    status, guilds = await self._dsb_request(method="GET", token=token, path="/api/v9/users/@me/guilds")
                    if status == 200 and isinstance(guilds, list):
                        next_channels: list[str] = []
                        next_meta: dict[str, dict] = {}
                        for g in guilds:
                            g = ext_dict('discord guild', g)
                            gid = ext_id(g.get("id"), "id").strip()
                            if not gid:
                                continue
                            cStatus, channels = await self._dsb_request(method="GET", token=token, path=f"/api/v9/guilds/{gid}/channels")
                            if cStatus != 200 or not isinstance(channels, list):
                                continue
                            for ch in channels:
                                ch = ext_dict('discord channel', ch)
                                cid = ext_id(ch.get("id"), "id").strip()
                                if cid:
                                    next_channels.append(cid)
                                    next_meta[cid] = {
                                        "parent_id": ext_id(ch.get("parent_id"), "parent_id").strip(),
                                        "type": ext_int(ch.get("type"), "type"),
                                    }
                        channel_ids = next_channels
                        channel_meta = next_meta

                for channel_id in channel_ids:
                    cursor = ext_str(cursors.get(channel_id), "cursor").strip()
                    msgs: list[Any] = []
                    meta = channel_meta.get(channel_id) or {}
                    is_thread = is_thread_channel_type(ext_int(meta.get("type"), "type")) or bool(ext_id(meta.get("parent_id"), "parent_id").strip())
                    if not cursor:
                        if is_thread:
                            cursors[channel_id] = "0"
                            cursor = "0"
                            state_dirty = True
                        else:
                            status, seed_msgs = await self._dsb_request(
                                method="GET",
                                token=token,
                                path=f"/api/v9/channels/{channel_id}/messages",
                                params={"limit": "1"},
                            )
                            if status != 200 or not isinstance(seed_msgs, list):
                                continue
                            if not seed_msgs:
                                cursors[channel_id] = "0"
                                state_dirty = True
                                continue
                            seed = seed_msgs[0]
                            seed = ext_dict('discord seed message', seed)
                            seed_id = ext_id(seed.get("id"), "id").strip()
                            if not seed_id:
                                continue
                            author = seed.get("author")
                            is_bot = isinstance(author, dict) and bool(author.get("bot"))
                            content = ext_str(seed.get("content"), "content").strip()
                            if (not is_bot) and content.startswith("/start"):
                                msgs = [seed]
                            else:
                                cursors[channel_id] = seed_id
                                state_dirty = True
                                continue
                    if cursor and not msgs:
                        status, fetched = await self._dsb_request(
                            method="GET",
                            token=token,
                            path=f"/api/v9/channels/{channel_id}/messages",
                            params={"after": cursor, "limit": "100"},
                        )
                        if status != 200 or not isinstance(fetched, list):
                            continue
                        msgs = list(reversed(fetched))
                    for m in msgs:
                        m = ext_dict('discord message', m)
                        msg_id = ext_id(m.get("id"), "id").strip()
                        if not msg_id:
                            continue
                        author = m.get("author")
                        author = ext_dict('discord message author', author)
                        author_id = ext_id(author.get("id"), "id").strip() or None
                        self_id = ext_id(st.gateway_user_id, "gateway_user_id").strip()
                        msg_type = ext_int(m.get("type"), "type")
                        if ext_bool(author.get("bot"), "bot") or ext_bool(author.get("system"), "system"):
                            cursors[channel_id] = msg_id
                            state_dirty = True
                            continue
                        if self_id and author_id == self_id:
                            cursors[channel_id] = msg_id
                            state_dirty = True
                            continue
                        if msg_type not in (0, 19):
                            cursors[channel_id] = msg_id
                            state_dirty = True
                            continue
                        content = ext_str(m.get("content"), "content").strip()
                        sticker_raw = m.get("sticker_items")
                        if sticker_raw is None:
                            sticker_raw = m.get("stickers")
                        if sticker_raw is None:
                            sticker_list = []
                        else:
                            sticker_raw = ext_list('discord stickers', sticker_raw)
                            sticker_list = sticker_raw
                        atts_raw = m.get("attachments")
                        if atts_raw is None:
                            atts_raw = []
                        else:
                            atts_raw = ext_list('discord attachments', atts_raw)
                        attachments, size_note = self._dsb_attachments(
                            atts_raw,
                            stickers=sticker_list,
                        )
                        if size_note:
                            content = f"{content}\n{size_note}".strip() if content else size_note
                        meta = channel_meta.get(channel_id) or {}
                        chat_id, open_id, chat_type = resolve_chat_and_thread(
                            channel_id,
                            meta.get("parent_id") or "",
                            meta.get("type") or 0,
                        )
                        force = content.startswith("/start")
                        model_id = None if force else (await self.conductor.db.lookup_model_for_chat(acct_id=acct_id, chat_id=open_id))
                        if not model_id:
                            model_id = await self.conductor.ensure_chat_mapping(
                                acct_id=acct_id,
                                chat_id=open_id,
                                chat_type="discord",
                                carrier_user_id=author_id,
                                force=force,
                            )
                        if not model_id:
                            break
                        ref = m.get("message_reference")
                        reply_to = ""
                        reply_snippet = ""
                        if ref is not None:
                            ref = ext_dict('discord message_reference', ref)
                            reply_to = ext_id(ref.get("message_id"), "message_id").strip()
                        referenced = m.get("referenced_message")
                        if referenced is not None:
                            referenced = ext_dict('discord referenced_message', referenced)
                            reply_snippet = ext_str(referenced.get("content"), "content").strip()
                            if not reply_to:
                                reply_to = ext_id(referenced.get("id"), "id").strip()
                            if not reply_snippet:
                                ref_atts = referenced.get("attachments")
                                ref_stickers = referenced.get("sticker_items")
                                if ref_stickers is None:
                                    ref_stickers = referenced.get("stickers")
                                if ref_atts is not None:
                                    ref_atts = ext_list('discord referenced attachments', ref_atts)
                                if ref_stickers is not None:
                                    ref_stickers = ext_list('discord referenced stickers', ref_stickers)
                                if (isinstance(ref_atts, list) and ref_atts) or (
                                    isinstance(ref_stickers, list) and ref_stickers
                                ):
                                    reply_snippet = "(attachment)"
                        reply_to_text = guidance.reply_snippet(reply_snippet)
                        content = guidance.inject_reply_context(content, reply_to_text)
                        if not content and attachments:
                            for a in attachments:
                                if a.get("type") == "sticker":
                                    name = ext_str(a.get("file_name"), "file_name").strip()
                                    if a.get("is_animated") or not ext_str(a.get("url"), "url").strip():
                                        content = guidance.animated_sticker_injection(name)
                                    else:
                                        content = guidance.sticker_injection(name or "a sticker", "", "")
                                    break
                        body = {
                            "method": "send_message",
                            "params": {"text": content},
                            "text": content,
                            "caption": "",
                            "attachments": attachments,
                            "msg_id": msg_id,
                        }
                        if reply_to:
                            body["reply_to"] = reply_to
                        if reply_to_text:
                            body["reply_to_text"] = reply_to_text
                        if chat_type:
                            body["chat_type"] = chat_type
                        if not content and not attachments:
                            body["raw"] = m
                        ok = await self.conductor.deliver_inbound(
                            model_id=model_id,
                            acct_id=acct_id,
                            chat_id=chat_id,
                            request_id=msg_id,
                            body=body,
                        )
                        if not ok:
                            break
                        cursors[channel_id] = msg_id
                        state_dirty = True

                if state_dirty:
                    await self._save_runtime_state(acct_id, mode="dsb")

                await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=discord_dsb_poller acct_id=%s error=%s", acct_id, e, exc_info=e)
                await asyncio.sleep(2)
                continue
