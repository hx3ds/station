import asyncio
from typing import Any

from aiohttp import web

from station.api.http import read_json_object
from station.conductor.crypto import decrypt_if_encrypted
from station.conductor.platforms.policy import media_ref_action
from station.conductor.platforms.base import LocalPlatformAdapter
from station.conductor.platforms.discord import DiscordIO
from station.conductor.platforms.matrix import MatrixIO
from station.conductor.platforms.qq import QQIO
from station.conductor.platforms.telegram import TelegramIO
from station.conductor.platforms.whatsapp import WhatsAppAdapter
from station.conductor.platforms.whatsapp_cloud import WhatsAppCloudAdapter
from station.conductor.util import constant_time_equal, err, catch_external
from station import logger

def _plaintext_server(conductor, acct: dict) -> str:
    raw = (acct.get("server") or "").strip()
    if not raw:
        return ""
    plain, _ = decrypt_if_encrypted(conductor.private_key, raw)
    return (plain or "").strip()

def _media_ref_from_payload(payload: dict) -> str:
    return (
        payload.get("file_id")
        or payload.get("photo")
        or payload.get("video")
        or payload.get("audio")
        or payload.get("document")
        or payload.get("voice")
        or payload.get("animation")
        or payload.get("sticker")
        or ""
    ).strip()

def _http_url_from_payload(payload: dict) -> str:
    for key in ("photo", "video", "audio", "document", "voice", "animation", "sticker", "file_id"):
        val = (payload.get(key) or "").strip()
        if val.startswith("http://") or val.startswith("https://"):
            return val
    return ""

class TelegramAdapter(LocalPlatformAdapter):
    acct_type = "telegram"
    capabilities = frozenset({"send_typing", "download_file", "send_message", "send_media", "handle_webhook"})

    def __init__(self, conductor) -> None:
        self.conductor = conductor
        self.io = TelegramIO(conductor)

    async def send_typing(self, *, acct: dict, token: str, chat_id: str) -> bool:
        return await self.io.send_typing(token=token, chat_id=chat_id)

    async def download_file(self, *, acct: dict, token: str, file_id: str) -> bytes | None:
        return await self.io.download_file(token=token, file_id=file_id)

    async def send_message(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        keyboard=None,
    ) -> bool:
        return await self.io.send_message(
            token=token,
            chat_id=chat_id,
            text=text,
            reply_to=reply_to,
            keyboard=keyboard,
        )

    async def send_media(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        method: str,
        payload: dict,
        file_bytes: bytes | None,
        caption: str,
        reply_to: str | None,
    ) -> bool:
        media_ref = _media_ref_from_payload(payload)
        if file_bytes is None:
            action = media_ref_action("telegram", media_ref)
            if action == "download":
                file_bytes = await self.io.download_file(token=token, file_id=media_ref)
                media_ref = None
            elif action != "reuse":
                media_ref = None
        return await self.io.send_media(
            token=token,
            method=method,
            chat_id=chat_id,
            caption=caption,
            file_bytes=file_bytes,
            media_ref=media_ref if file_bytes is None else None,
            reply_to=reply_to,
            filename=(payload.get("file_name") or ""),
        )

    async def ensure_account_ready(self, acct_id: str) -> bool:
        if not self.io.webhook_active:
            return True
        return await self.io.ensure_webhook(acct_id=acct_id)

    async def _webhook_keeper_loop(self, acct_id: str) -> None:
        acct_id = (acct_id or "").strip()
        while not self.conductor.poller_stop.is_set():
            try:
                await self.io.ensure_webhook(acct_id=acct_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=telegram_webhook_keep acct_id=%s error=%s", acct_id, e, exc_info=e)
            await asyncio.sleep(30)

    async def poller_loop(self, acct_id: str) -> None:
        if self.io.webhook_active:
            await self._webhook_keeper_loop(acct_id)
            return
        await self.io.poller_loop(acct_id)

    async def close(self) -> None:
        await self.io.close()

    async def handle_webhook(self, request: web.Request) -> web.Response:
        acct_id = (request.match_info.get("acct_id") or "").strip()
        if not acct_id:
            return err("Missing acct_id", status=400)
        secret = self.io.webhook_secret
        if not secret:
            return err("Unauthorized", status=401)
        got = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
        if not constant_time_equal(got, secret):
            return err("Unauthorized", status=401)
        body = await read_json_object(request)
        ok_ = await self.io.process_update(acct_id=acct_id, update=body)
        return web.json_response({"result": 0, "data": {"ok": bool(ok_)}})

    def register_routes(self, app: web.Application) -> None:
        app.router.add_post("/webhook/telegram/{acct_id}", catch_external(self.handle_webhook))

class DiscordAdapter(LocalPlatformAdapter):
    acct_type = "discord"
    capabilities = frozenset(
        {"send_typing", "download_file", "send_message", "send_media", "send_discord_voice"}
    )

    def __init__(self, conductor) -> None:
        self.conductor = conductor
        self.io = DiscordIO(conductor)

    async def send_typing(self, *, acct: dict, token: str, chat_id: str) -> bool:
        return await self.io.send_typing(token=token, channel_id=chat_id)

    async def download_file(self, *, acct: dict, token: str, file_id: str) -> bytes | None:
        ref = (file_id or "").strip()
        if ref.startswith("http://") or ref.startswith("https://"):
            return await self.io.download(token=token, url=ref)
        return None

    async def send_message(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        keyboard=None,
    ) -> bool:
        return await self.io.send_message(
            token=token,
            channel_id=chat_id,
            content=text,
            reply_to=reply_to,
            keyboard=keyboard,
        )

    async def send_media(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        method: str,
        payload: dict,
        file_bytes: bytes | None,
        caption: str,
        reply_to: str | None,
    ) -> bool:
        bytes_to_send = file_bytes
        if bytes_to_send is None:
            download_url = _http_url_from_payload(payload)
            if download_url and media_ref_action("discord", download_url) == "download":
                bytes_to_send = await self.io.download(token=token, url=download_url)
        if bytes_to_send is None:
            return False
        return await self.io.send_message(
            token=token,
            channel_id=chat_id,
            content=caption,
            file_bytes=bytes_to_send,
            filename=(payload.get("file_name") or "file"),
            reply_to=reply_to,
        )

    async def send_discord_voice(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        action: str,
        channel_id: str | None,
        guild_id: str | None,
        self_mute: bool = False,
        self_deaf: bool = False,
    ) -> bool:
        action = (action or "").strip().lower() or "join"
        voice_channel_id = (channel_id or "").strip()
        if action == "leave":
            voice_channel_id = ""
        elif action != "join":
            return False
        elif not voice_channel_id:
            return False
        return await self.io.voice_state_update(
            token=token,
            acct_id=(acct.get("acct_id") or "").strip(),
            relay_chat_id=chat_id,
            guild_id=guild_id,
            channel_id=voice_channel_id or None,
            self_mute=self_mute,
            self_deaf=self_deaf,
        )

    async def poller_loop(self, acct_id: str) -> None:
        await self.io.poller_loop(acct_id)

class MatrixAdapter(LocalPlatformAdapter):
    acct_type = "matrix"
    capabilities = frozenset(
        {"send_typing", "download_file", "send_message", "send_media", "send_webrtc"}
    )

    def __init__(self, conductor) -> None:
        self.conductor = conductor
        self.io = MatrixIO(conductor)

    def _homeserver(self, acct: dict) -> str:
        return _plaintext_server(self.conductor, acct)

    async def send_typing(self, *, acct: dict, token: str, chat_id: str) -> bool:
        return await self.io.send_typing(
            acct_id=acct.get("acct_id") or "",
            homeserver=self._homeserver(acct),
            access_token=token,
            room_id=chat_id,
        )

    async def download_file(self, *, acct: dict, token: str, file_id: str) -> bytes | None:
        return await self.io.download_mxc(
            acct_id=acct.get("acct_id") or "",
            homeserver=self._homeserver(acct),
            access_token=token,
            mxc=file_id,
        )

    async def send_message(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        keyboard=None,
    ) -> bool:
        return await self.io.send_message(
            acct_id=acct.get("acct_id") or "",
            homeserver=self._homeserver(acct),
            access_token=token,
            room_id=chat_id,
            text=text,
            reply_to=reply_to,
            keyboard=keyboard,
        )

    async def send_media(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        method: str,
        payload: dict,
        file_bytes: bytes | None,
        caption: str,
        reply_to: str | None,
    ) -> bool:
        msgtype = "m.file"
        if method == "send_photo":
            msgtype = "m.image"
        elif method == "send_video":
            msgtype = "m.video"
        elif method in ("send_audio", "send_voice"):
            msgtype = "m.audio"
        name = payload.get("file_name") or "file"
        acct_id = acct.get("acct_id") or ""
        homeserver = self._homeserver(acct)
        if file_bytes is None:
            ref = _media_ref_from_payload(payload)
            if media_ref_action("matrix", ref) != "reuse":
                return False
            msg_content: dict[str, Any] = {"msgtype": msgtype, "body": caption or name, "url": ref}
            if method == "send_voice":
                msg_content["org.matrix.msc3245.voice"] = {}
            if reply_to:
                msg_content["m.relates_to"] = {"m.in_reply_to": {"event_id": reply_to.strip()}}
            return await self.io.send_event(
                acct_id=acct_id,
                homeserver=homeserver,
                access_token=token,
                room_id=chat_id,
                event_type="m.room.message",
                content=msg_content,
            )
        return await self.io.send_media_message(
            acct_id=acct_id,
            homeserver=homeserver,
            access_token=token,
            room_id=chat_id,
            msgtype=msgtype,
            file_bytes=file_bytes,
            filename=name,
            content_type=(payload.get("content_type") or "") or None,
            caption=caption or name,
            is_voice=method == "send_voice",
            reply_to=reply_to,
        )

    async def send_webrtc(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        webrtc_type: str,
        content: dict,
    ) -> bool:
        return await self.io.send_event(
            acct_id=acct.get("acct_id") or "",
            homeserver=self._homeserver(acct),
            access_token=token,
            room_id=chat_id,
            event_type=webrtc_type,
            content=content,
        )

    async def poller_loop(self, acct_id: str) -> None:
        await self.io.poller_loop(acct_id)

    async def close(self) -> None:
        await self.io.close()

class QQAdapter(LocalPlatformAdapter):
    acct_type = "qq"
    capabilities = frozenset({"send_typing", "download_file", "send_message", "send_media"})

    def __init__(self, conductor) -> None:
        self.conductor = conductor
        self.io = QQIO(conductor)

    async def send_typing(self, *, acct: dict, token: str, chat_id: str) -> bool:
        return await self.io.send_typing(
            token=token,
            chat_id=chat_id,
            acct_id=acct.get("acct_id") or "",
        )

    async def download_file(self, *, acct: dict, token: str, file_id: str) -> bytes | None:
        return await self.io.download(token=token, url=file_id)

    async def send_message(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        text: str,
        reply_to: str | None = None,
        keyboard=None,
    ) -> bool:
        return await self.io.send_message(
            token=token,
            chat_id=chat_id,
            content=text,
            reply_to=reply_to,
            keyboard=keyboard,
            acct_id=acct.get("acct_id") or "",
            server=_plaintext_server(self.conductor, acct) or None,
        )

    async def send_media(
        self,
        *,
        acct: dict,
        token: str,
        chat_id: str,
        method: str,
        payload: dict,
        file_bytes: bytes | None,
        caption: str,
        reply_to: str | None,
    ) -> bool:
        media_ref = _media_ref_from_payload(payload)
        if file_bytes is None:
            action = media_ref_action("qq", media_ref)
            if action == "download":
                file_bytes = await self.io.download(token=token, url=media_ref)
                media_ref = None
            elif action != "reuse":
                media_ref = None
        return await self.io.send_media(
            token=token,
            chat_id=chat_id,
            method=method,
            caption=caption,
            file_bytes=file_bytes,
            media_ref=media_ref if file_bytes is None else None,
            filename=payload.get("file_name") or "file",
            reply_to=reply_to,
            acct_id=acct.get("acct_id") or "",
            server=_plaintext_server(self.conductor, acct) or None,
        )

    async def poller_loop(self, acct_id: str) -> None:
        await self.io.poller_loop(acct_id)

    async def close(self) -> None:
        await self.io.close()

def build_builtin_platforms(conductor) -> dict[str, LocalPlatformAdapter]:
    platforms: list[LocalPlatformAdapter] = [
        TelegramAdapter(conductor),
        DiscordAdapter(conductor),
        MatrixAdapter(conductor),
        QQAdapter(conductor),
        WhatsAppAdapter(conductor),
        WhatsAppCloudAdapter(conductor),
    ]
    return {platform.acct_type: platform for platform in platforms}
