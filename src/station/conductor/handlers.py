import asyncio
import json
import os
from typing import Any
import uuid

import aiohttp
from aiohttp import web
from aiohttp.web_request import FileField
from cryptography.hazmat.primitives.asymmetric import rsa

from station.api.reception import is_slash_command
from station.conductor.crypto import default_private_key_path, decrypt_if_encrypted, load_private_key, public_key_token, write_private_key
from station.conductor.platform_types import (
    is_qr_account_type,
    strip_qr_prefix,
    validate_local_platform_type,
)
from station.conductor.platforms import LocalPlatformAdapter, build_builtin_platforms
from station.conductor.pair import PairManager
from station.conductor.util import constant_time_equal, err, ext_bool, ext_int, ext_str, ok
from station import logger
from station.prototypes.boundary import ext_dict, ext_float, ext_list, ext_require

_MEDIA_METHODS = {
    "send_photo",
    "send_video",
    "send_audio",
    "send_document",
    "send_voice",
    "send_animation",
    "send_sticker",
}

class LocalConductor:
    def __init__(self, app: web.Application):
        self.app = app
        self.db = app["db"]
        self.config = app["config"]
        self.session: aiohttp.ClientSession = app["session"]

        lc = self.config.local_conductor
        self.private_key_path = lc.private_key_path.strip() if lc.private_key_path else ""
        if not self.private_key_path:
            self.private_key_path = default_private_key_path(self.config.database.path)
        self.public_key_path = lc.public_key_path.strip() if lc.public_key_path else ""
        if not self.public_key_path:
            self.public_key_path = self.private_key_path + ".pub"

        self.private_key: rsa.RSAPrivateKey | None = None
        self.public_key_token: str = ""

        self.poller_stop = asyncio.Event()
        self._poller_tasks: dict[str, asyncio.Task] = {}
        self._poller_task_types: dict[str, str] = {}
        self._poller_reconciler: asyncio.Task | None = None

        self.platforms: dict[str, LocalPlatformAdapter] = build_builtin_platforms(self)
        self.pair_manager = PairManager(self)

    @property
    def enabled(self) -> bool:
        return self.app["prototype_is_local"]

    def require_own_prototype(self, prototype_id: int) -> web.Response | None:
        registry = self.app["tenants"]
        if not registry.hosts(prototype_id):
            return err("Prototype mismatch", status=404)
        return None

    def conductor_address(self) -> str:
        configured = self.config.local_conductor.address.strip().rstrip("/") if self.config.local_conductor.address else ""
        if configured:
            if not configured.lower().startswith("http://") and not configured.lower().startswith("https://"):
                configured = "http://" + configured
            return configured
        access_point = ""
        if "_prototype_access_point" in self.app:
            ap = self.app["_prototype_access_point"]
            access_point = ap.strip().rstrip("/") if ap else ""
        if access_point:
            if not access_point.lower().startswith("http://") and not access_point.lower().startswith("https://"):
                access_point = "http://" + access_point
            return access_point
        host = self.config.host.strip() if self.config.host else "127.0.0.1"
        port = self.config.port if self.config.port is not None else 0
        return f"http://{host}:{port}".rstrip("/")

    def get_platform(self, acct_type: str | None) -> LocalPlatformAdapter | None:
        normalized = strip_qr_prefix(acct_type)
        if not normalized:
            return None
        return self.platforms.get(normalized)

    def _is_session_bound_account(self, acct: dict | None) -> bool:
        if not acct:
            return False
        if is_qr_account_type(acct.get("acct_type")):
            return True
        return strip_qr_prefix(acct.get("acct_type")) == "whatsapp"

    async def _resolve_local_account_context(
        self,
        acct_id: str,
    ) -> tuple[dict | None, str | None, LocalPlatformAdapter | None]:
        acct = await self.db.get_local_account(acct_id)
        if not acct:
            return None, None, None
        adapter = self.get_platform(acct.get("acct_type"))
        server_raw = acct.get("server")
        if server_raw is None:
            server_raw = ""
        server_raw = server_raw.strip()
        if server_raw:
            server_plain, _ = decrypt_if_encrypted(self.private_key, server_raw)
            plain = server_plain.strip() if server_plain else ""
            acct = {**acct, "server": plain}
        if self._is_session_bound_account(acct):
            tok = acct.get("encrypted_token")
            if tok is None:
                tok = ""
            tok = tok.strip()
            return acct, tok if tok else "session", adapter
        token = await self._decrypt_acct_token(acct)
        return acct, token, adapter

    async def _start_platform_poller(
        self,
        *,
        acct_id: str,
        adapter: LocalPlatformAdapter,
    ) -> None:
        await adapter.ensure_account_ready(acct_id)
        await adapter.poller_loop(acct_id)

    async def _dispatch_platform_method(
        self,
        *,
        adapter: LocalPlatformAdapter | None,
        acct: dict,
        token: str,
        method: str,
        chat_id: str,
        payload: dict,
        file_bytes: bytes | None,
    ) -> web.Response:
        acct_type = acct.get("acct_type")
        if acct_type is None:
            acct_type = ""
        acct_type = acct_type.strip().lower()
        if adapter is None:
            return err(f"No adapter registered for {acct_type}", status=501)

        try:
            if method == "send_typing":
                if not adapter.supports("send_typing"):
                    return err("Platform does not support send_typing", status=501)
                typing_kwargs = {"acct": acct, "token": token, "chat_id": chat_id}
                ok_ = await adapter.send_typing(**typing_kwargs)
                return ok({"ok": ok_}) if ok_ else err("send_typing failed", status=502)

            if method == "download_file":
                file_id = ext_str(payload.get("file_id"), "file_id").strip()
                if not file_id:
                    return err("Missing file_id", status=400)
                if not adapter.supports("download_file"):
                    return err("Platform does not support download_file", status=501)
                blob = await adapter.download_file(acct=acct, token=token, file_id=file_id)
                if blob is None:
                    return err("download failed", status=502)
                return web.Response(body=blob, status=200)

            if method == "send_message":
                if not adapter.supports("send_message"):
                    return err("Platform does not support send_message", status=501)
                text = ext_str(payload.get("text"), "text").strip()
                reply_to = ext_str(payload.get("reply_to"), "reply_to").strip() or None
                send_kwargs = {
                    "acct": acct,
                    "token": token,
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to": reply_to,
                    "keyboard": payload.get("keyboard"),
                }
                ok_ = await adapter.send_message(**send_kwargs)
                return ok({"ok": ok_}) if ok_ else err("send_message failed", status=502)

            if method == "send_webrtc":
                if not adapter.supports("send_webrtc"):
                    return err("Platform does not support send_webrtc", status=501)
                webrtc_type = ext_str(payload.get("webrtc_type"), "webrtc_type").strip()
                raw_content = payload.get("webrtc_content")
                if raw_content is None:
                    content_obj = {}
                elif isinstance(raw_content, dict):
                    content_obj = raw_content
                elif isinstance(raw_content, str):
                    try:
                        parsed = json.loads(raw_content)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        return err("Invalid webrtc_content", status=400)
                    try:
                        parsed = ext_dict('parsed', parsed)
                    except TypeError:
                        return err("Invalid webrtc_content", status=400)
                    content_obj = parsed
                else:
                    return err("Invalid webrtc_content", status=400)
                if not webrtc_type:
                    return err("Missing webrtc_type", status=400)
                ok_ = await adapter.send_webrtc(
                    acct=acct,
                    token=token,
                    chat_id=chat_id,
                    webrtc_type=webrtc_type,
                    content=content_obj,
                )
                return ok({"ok": ok_}) if ok_ else err("send_webrtc failed", status=502)

            if method == "send_discord_voice":
                if not adapter.supports("send_discord_voice"):
                    return err("Platform does not support send_discord_voice", status=501)
                action = ext_str(payload.get("action"), "action", default="join").strip().lower() or "join"
                channel_id = ext_str(payload.get("channel_id"), "channel_id").strip() or None
                guild_id = ext_str(payload.get("guild_id"), "guild_id").strip() or None
                self_mute = ext_bool(payload.get("self_mute"), "self_mute")
                self_deaf = ext_bool(payload.get("self_deaf"), "self_deaf")
                ok_ = await adapter.send_discord_voice(
                    acct=acct,
                    token=token,
                    chat_id=chat_id,
                    action=action,
                    channel_id=channel_id,
                    guild_id=guild_id,
                    self_mute=self_mute,
                    self_deaf=self_deaf,
                )
                return ok({"ok": ok_}) if ok_ else err("send_discord_voice failed", status=502)

            if method in _MEDIA_METHODS:
                if not adapter.supports("send_media"):
                    return err("Platform does not support media send", status=501)
                caption = ext_str(payload.get("caption"), "caption").strip()
                reply_to = ext_str(payload.get("reply_to"), "reply_to").strip() or None
                ok_ = await adapter.send_media(
                    acct=acct,
                    token=token,
                    chat_id=chat_id,
                    method=method,
                    payload=payload,
                    file_bytes=file_bytes,
                    caption=caption,
                    reply_to=reply_to,
                )
                return ok({"ok": ok_}) if ok_ else err("send failed", status=502)
        except TypeError as exc:
            return err(str(exc), status=400)

        return err("Unsupported gateway method", status=501)

    def ensure_keys(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.private_key_path)), exist_ok=True)
        try:
            os.chmod(os.path.dirname(os.path.abspath(self.private_key_path)), 0o700)
        except OSError as e:
            logger.error("unexpected where=chmod_private_key_dir error=%s", e, exc_info=e)

        if os.path.exists(self.private_key_path):
            key = load_private_key(self.private_key_path)
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            write_private_key(self.private_key_path, key)

        token = public_key_token(key.public_key())
        self.private_key = key
        self.public_key_token = token

        os.makedirs(os.path.dirname(os.path.abspath(self.public_key_path)), exist_ok=True)
        with open(self.public_key_path, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        try:
            os.chmod(self.public_key_path, 0o644)
        except OSError as e:
            logger.error("unexpected where=chmod_public_key error=%s", e, exc_info=e)

    def require_consul_token(self, request: web.Request) -> web.Response | None:
        token = request.headers.get("X-Consul-Token")
        if token is None:
            token = ""
        token = token.strip()
        if not self.public_key_token or not constant_time_equal(token, self.public_key_token):
            return err("Unauthorized", status=401)
        return None

    def require_prototype_token(self, request: web.Request) -> web.Response | None:
        from station.api.tenant_auth import authorize_request
        if authorize_request(request) is None:
            return err("Unauthorized", status=401)
        return None

    async def _prototype_version_ok(self, request: web.Request, prototype_id: int) -> bool:
        raw = request.headers.get("X-Prototype-Version")
        if raw is None or raw == "":
            return False
        try:
            incoming = int(raw)
        except ValueError:
            return False
        stored = (await self.db.get_prototype_info(prototype_id)) if prototype_id else None
        local = 0
        if stored is not None:
            v = stored.get("version")
            if v is not None:
                local = v
        registry = self.app["tenants"]
        tenant = registry.get(prototype_id)
        ctx = tenant.client_context if tenant is not None else None
        if ctx is not None and ctx.prototype_version is not None:
            local = max(local, ctx.prototype_version)
        if local == incoming:
            return True
        if tenant is None:
            return False
        token = tenant.token_state.token
        from station.client.consul import fetch_prototype
        from station.client.context import update_client_context_prototype
        fetched = await fetch_prototype(
            session=self.session,
            consul_url=self.config.consul_url,
            token=token,
        )
        if not fetched:
            return False
        fetched = ext_dict('fetch_prototype data', fetched)
        await self.db.put_prototype(fetched)
        if ctx is not None:
            update_client_context_prototype(ctx, fetched)
            local = ctx.prototype_version if ctx.prototype_version is not None else 0
        else:
            local = ext_int(fetched.get("version"), "version")
        return local == incoming

    async def _consul_post_body(self, *, path: str, payload: dict, prototype_token: str | None = None) -> dict | None:
        consul_url = self.config.consul_url.rstrip("/") if self.config.consul_url else ""
        if not consul_url:
            return None
        url = "%s%s" % (consul_url, path)
        headers = {"Content-Type": "application/json"}
        if self.public_key_token:
            headers["X-Conductor-Token"] = self.public_key_token
        if prototype_token:
            headers["X-Prototype-Token"] = prototype_token
        async with self.session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                return None
            body = await resp.json(content_type=None)
        body = ext_dict('consul body', body)
        return body

    async def _consul_post(self, *, path: str, payload: dict, prototype_token: str | None = None) -> dict | None:
        body = await self._consul_post_body(path=path, payload=payload, prototype_token=prototype_token)
        if not body or body.get("result") != 0:
            return None
        data = body.get("data")
        if data is None:
            return None
        data = ext_dict('consul data', data)
        return data

    async def _consul_post_full(self, *, path: str, payload: dict) -> dict | None:
        consul_url = self.config.consul_url.rstrip("/") if self.config.consul_url else ""
        if not consul_url:
            return None
        url = "%s%s" % (consul_url, path)
        headers = {"Content-Type": "application/json"}
        if self.public_key_token:
            headers["X-Conductor-Token"] = self.public_key_token
        async with self.session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                return None
            body = await resp.json(content_type=None)
        body = ext_dict('consul body', body)
        return body

    async def fetch_model_from_consul(self, *, model_id: str) -> dict | None:
        return await self._consul_post(path="/api/local_conductor/get_model", payload={"model_id": model_id})

    async def fetch_model_chats_from_consul(self, *, model_id: str) -> list[dict] | None:
        data = await self._consul_post(path="/api/local_conductor/get_model_chats", payload={"model_id": model_id})
        if not data:
            return None
        chats = data.get("chats")
        if chats is None:
            return None
        chats = ext_list('chats', chats)
        out = []
        for c in chats:
            c = ext_dict('chat', c)
            out.append(c)
        return out

    async def ensure_chat_mapping(self, *, acct_id: str, chat_id: str, chat_type: str, carrier_user_id: str | None, force: bool = False) -> str | None:
        if acct_id is None:
            acct_id = ""
        if chat_id is None:
            chat_id = ""
        acct_id = acct_id.strip()
        chat_id = chat_id.strip()
        if not (acct_id and chat_id):
            return None
        if not force:
            existing = await self.db.lookup_model_for_chat(acct_id=acct_id, chat_id=chat_id)
            if existing:
                return existing
        acct = await self.db.get_local_account(acct_id)
        if not acct:
            return None
        try:
            base_type = chat_type if chat_type else acct.get("acct_type")
            chat_type = validate_local_platform_type(
                strip_qr_prefix(base_type),
                field_name="chat_type",
            )
        except ValueError:
            return None
        model_id = acct.get("model_id")
        if model_id is None:
            model_id = ""
        model_id = model_id.strip()
        if not model_id:
            return None
        if carrier_user_id is None:
            carrier = "unknown"
        else:
            carrier = carrier_user_id.strip() or "unknown"
        body = await self._consul_post_full(
            path="/api/local_conductor/add_chat_to_model",
            payload={
                "chat_id": chat_id,
                "model_id": model_id,
                "acct_id": acct_id,
                "chat_type": chat_type,
                "carrier_user_id": carrier,
            },
        )
        if not body or body.get("result") != 0:
            return None
        stored_model = await self.db.get_model(model_id)
        prototype_id = stored_model.get("prototype_id") if stored_model and stored_model.get("prototype_id") is not None else None
        await self.db.upsert_local_chat(
            acct_id=acct_id,
            chat_id=chat_id,
            model_id=model_id,
            prototype_id=prototype_id,
            chat_type=chat_type,
            carrier_user_id=carrier_user_id,
        )
        return model_id

    async def _ensure_instance(self, *, model_id: str, prototype_id: int, settings: dict) -> Any | None:
        instances = self.app["instances"]
        instance_lock = self.app["instance_lock"]
        created = False
        async with instance_lock:
            instance = instances.get(model_id)
            if instance is None:
                registry = self.app["tenants"]
                tenant = registry.get(prototype_id)
                if tenant is None:
                    return None
                from station.api.instances import create_tenant_instance
                instance = create_tenant_instance(
                    self.app,
                    tenant,
                    prototype_id=prototype_id,
                    model_id=model_id,
                    model_settings=settings,
                )
                instances[model_id] = instance
                created = True
        if created:
            from station.api.instances import isolate_instance
            try:
                await instance.start()
            except ValueError:
                await isolate_instance(self.app, model_id, instance)
                return None
            except Exception as e:
                logger.error("unexpected where=ensure_instance_start model_id=%s error=%s", model_id, e, exc_info=e)
                await isolate_instance(self.app, model_id, instance)
                raise
        return instance

    async def _send_chat_opened(self, *, acct_id: str, chat_id: str, model_id: str | None = None, instance=None) -> bool:
        if acct_id is None:
            acct_id = ""
        if chat_id is None:
            chat_id = ""
        acct_id = acct_id.strip()
        chat_id = chat_id.strip()
        if not acct_id or not chat_id:
            return False
        acct, token, adapter = await self._resolve_local_account_context(acct_id)
        if not acct or not token or adapter is None:
            return False
        if not adapter.supports("send_message"):
            return False
        ok = await adapter.send_message(
            acct=acct,
            token=token,
            chat_id=chat_id,
            text="Chat opened.",
        )
        if ok:
            await self._notify_discord_chat_opened(
                acct=acct,
                token=token,
                adapter=adapter,
                chat_id=chat_id,
                model_id=model_id,
                instance=instance,
                acct_id=acct_id,
            )
        return ok

    async def _notify_discord_chat_opened(self, *, acct: dict, token: str, adapter, chat_id: str, model_id: str | None, instance, acct_id: str) -> None:
        if adapter is None or not adapter.supports("send_discord_voice"):
            return
        channel_type = 0
        guild_id = ""
        is_voice = False
        info = await adapter.io.get_channel_info(token=token, channel_id=chat_id)
        if info is not None:
            channel_type = ext_int(info.get("type"), "type")
            guild_id = ext_str(info.get("guild_id"), "guild_id").strip()
            is_voice = channel_type in (2, 13)
        call_support = False
        own_id = 0
        if model_id:
            model = await self.db.get_model(model_id)
            if model and model.get("prototype_id") is not None:
                own_id = model.get("prototype_id")
        if not own_id:
            registry = self.app["tenants"]
            own_id = registry.primary.id
        proto = await self.db.get_prototype_info(own_id) if own_id else None
        if not proto:
            registry = self.app["tenants"]
            tenant = registry.get(own_id)
            if tenant is None:
                return
            proto_token = tenant.token_state.token
            from station.client.consul import fetch_prototype
            fetched = await fetch_prototype(
                session=self.session,
                consul_url=self.config.consul_url,
                token=proto_token,
            )
            if fetched:
                fetched = ext_dict('fetch_prototype data', fetched)
                await self.db.put_prototype(fetched)
                proto = fetched
        if proto is not None:
            cs = proto.get("call_support")
            call_support = False if cs is None else cs
        if is_voice and not call_support:
            is_voice = False
            if adapter.supports("send_message"):
                await adapter.send_message(
                    acct=acct,
                    token=token,
                    chat_id=chat_id,
                    text="This model does not support voice calls. Continuing as a text channel.",
                )
        if instance is None or not model_id:
            if is_voice:
                await self._maybe_auto_join_discord_voice(
                    acct=acct, token=token, adapter=adapter, chat_id=chat_id, prototype_id=own_id
                )
            return
        await instance.handle_event(
            {
                "chat_opened": {
                    "platform": "discord",
                    "channel_id": chat_id,
                    "guild_id": guild_id,
                    "channel_type": channel_type,
                    "is_voice": is_voice,
                }
            },
            model_id,
            None,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=None,
            event_level="chat",
        )

    async def _maybe_auto_join_discord_voice(
        self, *, acct: dict, token: str, adapter, chat_id: str, prototype_id: int | None = None
    ) -> None:
        if adapter is None or not adapter.supports("send_discord_voice"):
            return
        own_id = prototype_id or 0
        if not own_id:
            registry = self.app["tenants"]
            own_id = registry.primary.id
        info = await self.db.get_prototype_info(own_id) if own_id else None
        if not info or not info.get("call_support"):
            if adapter.supports("send_message"):
                await adapter.send_message(
                    acct=acct,
                    token=token,
                    chat_id=chat_id,
                    text="This model does not support voice calls. Continuing as a text channel.",
                )
            return
        info = await adapter.io.get_channel_info(token=token, channel_id=chat_id)
        if info is None:
            return
        channel_type = ext_int(info.get("type"), "type")
        if channel_type not in (2, 13):
            return
        guild_raw = ext_str(info.get("guild_id"), "guild_id").strip()
        guild_id = guild_raw or None
        id_raw = info.get("id")
        if id_raw is None:
            channel_id = chat_id
        else:
            id_raw = ext_str(id_raw, 'id')
            channel_id = id_raw.strip() or chat_id
        await adapter.send_discord_voice(
            acct=acct,
            token=token,
            chat_id=chat_id,
            action="join",
            channel_id=channel_id,
            guild_id=guild_id,
        )

    async def deliver_inbound(self, *, model_id: str, acct_id: str, chat_id: str, request_id: str, body: dict) -> bool:
        if request_id is None:
            request_id = ""
        request_id = request_id.strip()
        if request_id:
            dedupe_key = "%s:%s" % (model_id, request_id)
            if await self.db.is_duplicate_request(dedupe_key):
                return True
        model = await self.db.get_model(model_id)
        if not model:
            remote = await self.fetch_model_from_consul(model_id=model_id)
            if remote:
                remote_settings = self._decrypt_model_settings(remote.get("settings"))
                if remote_settings is None:
                    return False
                remote["settings"] = remote_settings
                await self.db.put_model(model_id, remote)
                model = await self.db.get_model(model_id)
        if not model:
            return False
        prototype_id = model.get("prototype_id")
        settings = self._decrypt_model_settings(model.get("settings"))
        if settings is None:
            return False
        instance = await self._ensure_instance(model_id=model_id, prototype_id=prototype_id, settings=settings)
        if instance is None:
            return False
        text = ext_str(body.get("text"), "text").strip()
        platform = body.get("platform")
        if platform is None:
            platform = ""
        else:
            platform = ext_str(platform, 'platform')
        if not platform.strip() and acct_id:
            acct_lookup = acct_id.strip() if acct_id else ""
            acct = await self.db.get_local_account(acct_lookup)
            if acct:
                plat = strip_qr_prefix(acct.get("acct_type"))
                if plat:
                    body["platform"] = plat
        body = instance.rewrite_inbound_attachments(body, chat_id=chat_id, acct_id=acct_id)
        if is_slash_command(text):
            cmd = text.split(None, 1)[0].split("@", 1)[0].lower() if text else ""
            if cmd == "/start":
                await self._send_chat_opened(
                    acct_id=acct_id,
                    chat_id=chat_id,
                    model_id=model_id,
                    instance=instance,
                )
            accepted = await instance.handle_command(
                body,
                model_id=model_id,
                model_settings=settings,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
            )
        else:
            accepted = await instance.handle_message(
                body,
                model_id=model_id,
                model_settings=settings,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
            )
        if accepted is False:
            return False
        if request_id:
            await self.db.remember_request("%s:%s" % (model_id, request_id))
        return True

    async def _parse_body_and_file(self, request: web.Request) -> tuple[dict, bytes | None]:
        if request.content_type and request.content_type.startswith("multipart/"):
            form = await request.post()
            payload: dict[str, Any] = {}
            file_bytes: bytes | None = None
            for k, v in form.items():
                if isinstance(v, FileField):
                    v.file.seek(0)
                    file_bytes = v.file.read()
                else:
                    payload[k] = v
            return payload, file_bytes
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise ValueError("Invalid JSON body") from exc
        try:
            body = ext_dict("body", body)
        except TypeError as exc:
            raise ValueError("Invalid JSON body") from exc
        return body, None

    async def _poller_reconcile_loop(self) -> None:
        while not self.poller_stop.is_set():
            try:
                if not self.enabled:
                    await asyncio.sleep(5)
                    continue
                self.ensure_keys()
                desired: dict[str, LocalPlatformAdapter] = {}
                grouped_accounts: dict[str, list[dict]] = {}
                accounts = await self.db.list_local_accounts()
                for acct in accounts:
                    acct_type = strip_qr_prefix(acct.get("acct_type"))
                    if not acct_type:
                        continue
                    model_id = acct.get("model_id")
                    if model_id is None:
                        model_id = ""
                    model_id = model_id.strip()
                    if not model_id:
                        continue
                    if not await self.db.get_model(model_id):
                        continue
                    grouped_accounts.setdefault(acct_type, []).append(acct)

                for acct_type, grouped in grouped_accounts.items():
                    adapter = self.get_platform(acct_type)
                    if adapter is None:
                        continue
                    for acct in grouped:
                        acct_id = acct.get("acct_id")
                        if acct_id is None:
                            acct_id = ""
                        acct_id = acct_id.strip()
                        if acct_id:
                            desired[acct_id] = adapter

                desired_ids = set(desired.keys())
                current_ids = set(self._poller_tasks.keys())

                for acct_id in sorted(current_ids & desired_ids):
                    desired_type = desired[acct_id].acct_type
                    if self._poller_task_types.get(acct_id) == desired_type:
                        continue
                    t = self._poller_tasks.pop(acct_id, None)
                    if t:
                        t.cancel()
                    self._poller_task_types.pop(acct_id, None)

                for acct_id in sorted(desired_ids - set(self._poller_tasks.keys())):
                    adapter = desired.get(acct_id)
                    if adapter is None:
                        continue
                    self._poller_tasks[acct_id] = asyncio.create_task(
                        self._start_platform_poller(acct_id=acct_id, adapter=adapter)
                    )
                    self._poller_task_types[acct_id] = adapter.acct_type

                for acct_id in sorted(set(self._poller_tasks.keys()) - desired_ids):
                    t = self._poller_tasks.pop(acct_id, None)
                    self._poller_task_types.pop(acct_id, None)
                    if t:
                        t.cancel()
            except Exception as e:
                logger.error("unexpected where=local poller reconcile error=%s", e, exc_info=e)
            await asyncio.sleep(5)

    async def poller_ctx(self, app: web.Application):
        self.poller_stop = asyncio.Event()
        self._poller_reconciler = asyncio.create_task(self._poller_reconcile_loop())
        yield
        self.poller_stop.set()
        for t in list(self._poller_tasks.values()):
            t.cancel()
        self._poller_tasks.clear()
        self._poller_task_types.clear()
        if self._poller_reconciler is not None:
            self._poller_reconciler.cancel()
            self._poller_reconciler = None
        for adapter in self.platforms.values():
            try:
                await adapter.close()
            except Exception as e:
                logger.error("unexpected where=platform_close acct_type=%s error=%s", adapter.acct_type, e, exc_info=e)

    async def handle_consul_ping(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        return ok({"ok": True})

    async def handle_consul_info(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        model_count = await self.db.count_models()
        return ok({"conductor_address": self.conductor_address(), "model_count": model_count})

    async def handle_model_create(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        prototype_id_raw = request.match_info.get("prototype_id")
        if prototype_id_raw is None:
            prototype_id_raw = ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        try:
            prototype_id = int(prototype_id_raw)
        except ValueError:
            return err("Invalid prototype_id", status=400)
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return mismatch
        if not model_id:
            return err("Missing model_id", status=400)

        remote = await self.fetch_model_from_consul(model_id=model_id)
        if not remote:
            return err("Model not found", status=404)
        remote_settings = self._decrypt_model_settings(remote.get("settings"))
        if remote_settings is None:
            return err("Model settings decrypt failed", status=500)
        remote["settings"] = remote_settings
        remote["prototype_id"] = prototype_id
        await self.db.put_model(model_id, remote)

        try:
            accts = remote.get("accts")
            if accts is None:
                accts = []
            accts = ext_list('accts', accts)
            for a in accts:
                a = ext_dict('acct', a)
                acct_id = a.get("acct_id")
                acct_id = ext_str(acct_id, 'acct_id', default="")
                if not acct_id:
                    raise TypeError("acct_id must be non-empty str")
                acct_id = acct_id.strip()
                acct_type = a.get("acct_type")
                if acct_type is None:
                    acct_type = a.get("type")
                acct_type = ext_str(acct_type, 'acct_type')
                username = ext_str(a.get("acct_username"), "acct_username")
                server = ext_str(a.get("server"), "server")
                encrypted_token = ext_str(a.get("acct_token"), "acct_token")
                await self.db.upsert_local_account(
                    acct_id=acct_id,
                    model_id=model_id,
                    prototype_id=prototype_id,
                    acct_type=acct_type.strip(),
                    username=username.strip(),
                    server=server.strip(),
                    encrypted_token=encrypted_token.strip(),
                    is_local=True,
                )

            chats = await self.fetch_model_chats_from_consul(model_id=model_id)
            if chats is not None:
                await self.db.replace_model_chats(model_id=model_id, prototype_id=prototype_id, chats=chats)
        except (ValueError, TypeError) as exc:
            return err(str(exc), status=400)

        await self._ensure_instance(model_id=model_id, prototype_id=prototype_id, settings=remote_settings)
        return ok({"ok": True})

    async def handle_model_remove(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        prototype_id_raw = request.match_info.get("prototype_id")
        if prototype_id_raw is None:
            prototype_id_raw = ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        try:
            prototype_id = int(prototype_id_raw)
        except ValueError:
            return err("Invalid prototype_id", status=400)
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return mismatch
        if not model_id:
            return err("Missing model_id", status=400)

        instances = self.app["instances"]
        instance_lock = self.app["instance_lock"]
        async with instance_lock:
            instance = instances.pop(model_id, None)
            self.app["instance_runtime"].pop(model_id, None)
        if instance is not None:
            try:
                await instance.stop()
            except Exception as e:
                logger.error("unexpected where=instance_stop model_id=%s error=%s", model_id, e, exc_info=e)
        await self.db.delete_local_conductor_state_for_model(model_id)
        await self.db.delete_model(model_id)
        return ok({"ok": True})

    async def handle_model_update_acct(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        acct_id_raw = request.match_info.get("acct_id")
        acct_id = acct_id_raw.strip() if acct_id_raw is not None else ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        if not acct_id:
            return err("Missing acct_id", status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            return err("Invalid JSON body", status=400)
        try:
            body = ext_dict('body', body)
        except TypeError:
            return err("Invalid JSON body", status=400)
        prototype_id = None
        remote_acct_ids = set()
        if model_id:
            stored = await self.db.get_model(model_id)
            remote = None
            if not stored:
                remote = await self.fetch_model_from_consul(model_id=model_id)
                if not remote:
                    return err("Model not found", status=404)
                remote_settings = self._decrypt_model_settings(remote.get("settings"))
                if remote_settings is None:
                    return err("Model settings decrypt failed", status=500)
                remote["settings"] = remote_settings
                if remote.get("prototype_id") is not None:
                    remote["prototype_id"] = remote.get("prototype_id")
                await self.db.put_model(model_id, remote)
                stored = await self.db.get_model(model_id)
                await self._ensure_instance(
                    model_id=model_id,
                    prototype_id=ext_int(remote.get("prototype_id"), "prototype_id"),
                    settings=remote_settings,
                )
            else:
                remote = await self.fetch_model_from_consul(model_id=model_id)
            if remote:
                accts = remote.get("accts")
                if accts is not None:
                    accts = ext_list('accts', accts)
                    for a in accts:
                        a = ext_dict('acct', a)
                        rid = ext_str(a.get("acct_id"), "acct_id").strip()
                        if rid:
                            remote_acct_ids.add(rid)
            if stored:
                prototype_id = stored.get("prototype_id")
        try:
            acct_type = ext_str(body.get("acct_type"), "acct_type").strip()
            await self.db.upsert_local_account(
                acct_id=acct_id,
                model_id=model_id if model_id else None,
                prototype_id=prototype_id,
                acct_type=acct_type,
                username=ext_str(body.get("acct_username"), "acct_username").strip(),
                server=ext_str(body.get("server"), "server").strip(),
                encrypted_token=ext_str(body.get("acct_token"), "acct_token").strip(),
                is_local=True,
            )
        except (ValueError, TypeError) as exc:
            return err(str(exc), status=400)
        if model_id:
            keep = set(remote_acct_ids)
            keep.add(acct_id)
            for existing in await self.db.list_local_accounts():
                existing_mid = existing.get("model_id")
                if existing_mid is None:
                    existing_mid = ""
                if existing_mid.strip() != model_id:
                    continue
                existing_id = existing.get("acct_id")
                if existing_id is None:
                    existing_id = ""
                existing_id = existing_id.strip()
                if existing_id and existing_id not in keep:
                    await self.db.delete_local_account(existing_id)
        adapter = self.get_platform(acct_type)
        if adapter is not None:
            await adapter.ensure_account_ready(acct_id)
        try:
            want_otp = ext_bool(body.get("otp"), "otp")
        except TypeError as exc:
            return err(str(exc), status=400)
        if want_otp:
            otp = "%06d" % (uuid.uuid4().int % 1000000)
            return ok({"otp": otp})
        return ok({"ok": True})

    async def handle_model_delete_acct(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        acct_id_raw = request.match_info.get("acct_id")
        acct_id = acct_id_raw.strip() if acct_id_raw is not None else ""
        if not acct_id:
            return err("Missing acct_id", status=400)
        await self.db.delete_local_account(acct_id)
        return ok({"ok": True})

    async def handle_model_remove_acct(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        acct_id_raw = request.match_info.get("acct_id")
        acct_id = acct_id_raw.strip() if acct_id_raw is not None else ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        if not acct_id or not model_id:
            return err("Invalid path", status=400)
        await self.db.delete_local_account(acct_id)
        return ok({"ok": True})

    async def handle_model_sync_chats(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        prototype_id_raw = request.match_info.get("prototype_id")
        if prototype_id_raw is None:
            prototype_id_raw = ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        try:
            prototype_id = int(prototype_id_raw)
        except ValueError:
            return err("Invalid prototype_id", status=400)
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return mismatch
        if not model_id:
            return err("Missing model_id", status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            return err("Invalid JSON body", status=400)
        try:
            body = ext_dict('body', body)
        except TypeError:
            return err("Invalid JSON body", status=400)
        chats = body.get("chats")
        if chats is None:
            chats = []
        try:
            chats = ext_list('chats', chats)
        except TypeError:
            return err("Invalid chats", status=400)
        try:
            await self.db.replace_model_chats(model_id=model_id, prototype_id=prototype_id, chats=chats)
        except (ValueError, TypeError) as exc:
            return err(str(exc), status=400)
        return ok({"ok": True})

    async def handle_model_remove_chat(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        chat_id_raw = request.match_info.get("chat_id")
        chat_id = chat_id_raw.strip() if chat_id_raw is not None else ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        if not chat_id or not model_id:
            return err("Invalid path", status=400)
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            return err("Invalid JSON body", status=400)
        try:
            body = ext_dict('body', body)
        except TypeError:
            return err("Invalid JSON body", status=400)
        try:
            acct_id = ext_str(body.get("acct_id"), "acct_id").strip()
        except TypeError as exc:
            return err(str(exc), status=400)
        if not acct_id:
            return err("Missing acct_id", status=400)
        await self.db.remove_chat(model_id=model_id, chat_id=chat_id, acct_id=acct_id)
        return ok({"ok": True})

    async def handle_model_update_period(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        return ok({"ok": True})

    async def handle_prototype_update(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        prototype_id_raw = request.match_info.get("prototype_id")
        if prototype_id_raw is None:
            prototype_id_raw = ""
        try:
            prototype_id = int(prototype_id_raw)
        except ValueError:
            return err("Invalid prototype_id", status=400)
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return mismatch
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            return err("Invalid JSON body", status=400)
        try:
            body = ext_dict('body', body)
        except TypeError:
            return err("Invalid JSON body", status=400)
        stored_row = await self.db.get_prototype_info(prototype_id)
        stored = dict(stored_row) if stored_row is not None else {}
        updated = dict(stored)
        if body.get("token") is not None:
            updated["token"] = body.get("token")
        if body.get("type") is not None:
            updated["type"] = body.get("type")
        if body.get("access_point") is not None:
            updated["access_point"] = body.get("access_point")
        if body.get("max_chats") is not None:
            updated["max_chats"] = body.get("max_chats")
        if body.get("version") is not None:
            updated["version"] = body.get("version")
        updated["prototype_id"] = prototype_id
        if "name" not in updated:
            name = stored.get("name")
            updated["name"] = "" if name is None else name
        if "ava" not in updated:
            ava = stored.get("ava")
            updated["ava"] = False if ava is None else ava
        if "token" not in updated:
            token = stored.get("token")
            updated["token"] = "" if token is None else token
        if "access_point" not in updated:
            ap = stored.get("access_point")
            updated["access_point"] = "" if ap is None else ap
        if "type" not in updated:
            ptype = stored.get("type")
            updated["type"] = "token" if ptype is None else ptype
        if "version" not in updated:
            ver = stored.get("version")
            updated["version"] = 0 if ver is None else ver
        await self.db.put_prototype(updated)
        return ok({"ok": True})

    async def handle_prototype_delete(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        return ok({"ok": True})

    async def handle_gateway_list_chats(self, request: web.Request) -> web.Response:
        unauthorized = self.require_prototype_token(request)
        if unauthorized is not None:
            return unauthorized
        prototype_id_raw = request.match_info.get("prototype_id")
        if prototype_id_raw is None:
            prototype_id_raw = ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        acct_id_raw = request.match_info.get("acct_id")
        acct_id = acct_id_raw.strip() if acct_id_raw is not None else ""
        try:
            prototype_id = int(prototype_id_raw)
        except ValueError:
            return err("Invalid prototype_id", status=400)
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return mismatch
        if not model_id:
            return err("Missing model_id", status=400)
        if not (await self._prototype_version_ok(request, prototype_id)):
            return err("Synchronize from consul", status=490)
        chats = await self.db.list_model_chats(model_id=model_id, acct_id=acct_id if acct_id else None)
        return ok({"chats": chats})

    async def _decrypt_acct_token(self, acct: dict) -> str | None:
        token_enc = acct.get("encrypted_token")
        if token_enc is None:
            token_enc = ""
        token_enc = token_enc.strip()
        if is_qr_account_type(acct.get("acct_type")):
            return token_enc
        token, _ = decrypt_if_encrypted(self.private_key, token_enc)
        return token.strip()

    async def handle_pair_start(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            return err("Invalid JSON body", status=400)
        try:
            body = ext_dict('body', body)
        except TypeError:
            return err("Invalid JSON body", status=400)
        try:
            data = await self.pair_manager.start(
                acct_id=ext_str(body.get("acct_id"), "acct_id").strip(),
                platform=ext_str(body.get("platform"), "platform").strip(),
                qr_timeout_ms=body.get("qr_timeout_ms"),
            )
        except (ValueError, TypeError) as exc:
            return err(str(exc), status=400)
        return ok(data)

    async def handle_pair_status(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            return err("Invalid JSON body", status=400)
        try:
            body = ext_dict('body', body)
        except TypeError:
            return err("Invalid JSON body", status=400)
        try:
            data = await self.pair_manager.status(
                acct_id=ext_str(body.get("acct_id"), "acct_id").strip(),
                platform=ext_str(body.get("platform"), "platform").strip(),
            )
        except TypeError as exc:
            return err(str(exc), status=400)
        return ok(data)

    async def handle_pair_cancel(self, request: web.Request) -> web.Response:
        unauthorized = self.require_consul_token(request)
        if unauthorized is not None:
            return unauthorized
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError, TypeError):
            return err("Invalid JSON body", status=400)
        try:
            body = ext_dict('body', body)
        except TypeError:
            return err("Invalid JSON body", status=400)
        try:
            data = await self.pair_manager.cancel(
                acct_id=ext_str(body.get("acct_id"), "acct_id").strip(),
                platform=ext_str(body.get("platform"), "platform").strip(),
            )
        except TypeError as exc:
            return err(str(exc), status=400)
        return ok(data)

    def _decrypt_model_settings(self, settings: Any) -> dict | None:
        if settings is None:
            return {}
        settings = ext_dict('settings', settings)
        if not settings:
            return {}
        enc = settings.get("__enc__")
        if enc is None:
            return dict(settings)
        try:
            enc = ext_str(enc, "__enc__", default="")
        except TypeError:
            return dict(settings)
        if not enc:
            return dict(settings)
        plain, _ = decrypt_if_encrypted(self.private_key, enc)
        decoded = json.loads(plain) if plain.strip() else {}
        decoded = ext_dict('decrypted settings', decoded)
        return decoded

    async def handle_gateway(self, request: web.Request) -> web.Response:
        unauthorized = self.require_prototype_token(request)
        if unauthorized is not None:
            return unauthorized
        method_raw = request.match_info.get("method")
        method = method_raw.strip() if method_raw is not None else ""
        chat_id_raw = request.match_info.get("chat_id")
        chat_id = chat_id_raw.strip() if chat_id_raw is not None else ""
        prototype_id_raw = request.match_info.get("prototype_id")
        if prototype_id_raw is None:
            prototype_id_raw = ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        acct_id_raw = request.match_info.get("acct_id")
        acct_id = acct_id_raw.strip() if acct_id_raw is not None else ""
        try:
            prototype_id = int(prototype_id_raw)
        except ValueError:
            return err("Invalid prototype_id", status=400)
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return mismatch
        if not (method and model_id and acct_id):
            return err("Invalid path", status=400)
        if not (await self._prototype_version_ok(request, prototype_id)):
            return err("Synchronize from consul", status=490)

        acct, token, adapter = await self._resolve_local_account_context(acct_id)
        if not acct:
            return err("Acct not found", status=404)
        if not token:
            return err("Acct token decrypt failed", status=500)
        try:
            payload, file_bytes = await self._parse_body_and_file(request)
        except ValueError as exc:
            return err(str(exc), status=400)
        payload.pop("acct_id", None)
        payload.pop("chat_id", None)
        payload.pop("request_id", None)
        return await self._dispatch_platform_method(
            adapter=adapter,
            acct=acct,
            token=token,
            method=method,
            chat_id=chat_id,
            payload=payload,
            file_bytes=file_bytes,
        )

    async def handle_reply(self, request: web.Request) -> web.Response:
        unauthorized = self.require_prototype_token(request)
        if unauthorized is not None:
            return unauthorized

        method_raw = request.match_info.get("method")
        method = method_raw.strip() if method_raw is not None else ""
        chat_id_raw = request.match_info.get("chat_id")
        chat_id = chat_id_raw.strip() if chat_id_raw is not None else ""
        request_id_raw = request.match_info.get("request_id")
        request_id = request_id_raw.strip() if request_id_raw is not None else ""
        prototype_id_raw = request.match_info.get("prototype_id")
        if prototype_id_raw is None:
            prototype_id_raw = ""
        model_id_raw = request.match_info.get("model_id")
        model_id = model_id_raw.strip() if model_id_raw is not None else ""
        try:
            prototype_id = int(prototype_id_raw)
        except ValueError:
            return err("Invalid prototype_id", status=400)
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return mismatch
        if not (method and chat_id and model_id):
            return err("Invalid path", status=400)
        if not (await self._prototype_version_ok(request, prototype_id)):
            return err("Synchronize from consul", status=490)

        try:
            payload, file_bytes = await self._parse_body_and_file(request)
        except ValueError as exc:
            return err(str(exc), status=400)
        try:
            preferred_acct_id = ext_str(payload.get("acct_id"), "acct_id").strip()
        except TypeError as exc:
            return err(str(exc), status=400)
        acct_id = await self._resolve_reply_acct_id(
            model_id=model_id,
            chat_id=chat_id,
            preferred_acct_id=preferred_acct_id,
        )
        if not acct_id:
            return err("Chat not found", status=404)
        acct, token, adapter = await self._resolve_local_account_context(acct_id)
        if not acct:
            return err("Acct not found", status=404)
        if not token:
            return err("Acct token decrypt failed", status=500)
        payload.pop("acct_id", None)
        payload.pop("chat_id", None)
        payload.pop("request_id", None)
        if method not in {"send_message", *_MEDIA_METHODS}:
            return err("Unsupported reply method", status=501)
        return await self._dispatch_platform_method(
            adapter=adapter,
            acct=acct,
            token=token,
            method=method,
            chat_id=chat_id,
            payload=payload,
            file_bytes=file_bytes,
        )

    async def _resolve_reply_acct_id(self, *, model_id, chat_id, preferred_acct_id=""):
        chats = await self.db.list_model_chats(model_id=model_id)
        acct_id = None
        preferred = preferred_acct_id.strip() if preferred_acct_id else ""
        if preferred:
            for c in chats:
                cid = c.get("chat_id")
                if cid is None:
                    cid = ""
                aid = c.get("acct_id")
                if aid is None:
                    aid = ""
                if cid == chat_id and aid.strip() == preferred:
                    return preferred
            if await self.db.get_local_account(preferred):
                return preferred
        model = await self.db.get_model(model_id)
        primary = ""
        if model is not None:
            p = model.get("account_id")
            if p is None:
                p = ""
            primary = p.strip()
        for c in chats:
            cid = c.get("chat_id")
            if cid is None:
                cid = ""
            if cid != chat_id:
                continue
            cand = c.get("acct_id")
            if cand is None:
                cand = ""
            cand = cand.strip()
            if not cand:
                continue
            if primary and cand == primary:
                return cand
            if acct_id is None:
                acct_id = cand
        return acct_id

    def _read_first_file_bytes(self, files):
        if not files:
            return None
        for path in files.values():
            if not path:
                continue
            with open(path, "rb") as f:
                return f.read()
        return None

    async def invoke(
        self,
        *,
        method,
        prototype_id,
        model_id,
        chat_id=None,
        request_id=None,
        acct_id=None,
        params=None,
        files=None,
        use_gateway=False,
    ):
        if not self.enabled:
            return False
        method = method.strip() if method else ""
        model_id = model_id.strip() if model_id else ""
        chat_id = chat_id.strip() if chat_id else ""
        if not (method and model_id):
            return False
        mismatch = self.require_own_prototype(prototype_id)
        if mismatch is not None:
            return False
        payload = dict(params) if params else {}
        preferred = ""
        raw_preferred = payload.get("acct_id")
        if raw_preferred is not None:
            preferred = str(raw_preferred).strip()
        if acct_id:
            preferred = acct_id.strip()
        if use_gateway or method in {"send_webrtc", "send_discord_voice", "send_typing", "download_file"}:
            if not preferred:
                return False
            resolved_acct = preferred
        else:
            if not chat_id:
                return False
            resolved_acct = await self._resolve_reply_acct_id(
                model_id=model_id,
                chat_id=chat_id,
                preferred_acct_id=preferred,
            )
            if not resolved_acct:
                return False
            if method not in {"send_message", *_MEDIA_METHODS}:
                return False
        acct, token, adapter = await self._resolve_local_account_context(resolved_acct)
        if not acct or not token:
            return False
        payload.pop("acct_id", None)
        payload.pop("chat_id", None)
        payload.pop("request_id", None)
        file_bytes = self._read_first_file_bytes(files)
        if method == "download_file":
            resp = await self._dispatch_platform_method(
                adapter=adapter,
                acct=acct,
                token=token,
                method=method,
                chat_id=chat_id,
                payload=payload,
                file_bytes=file_bytes,
            )
            if resp.status >= 400:
                return False
            body = resp.body
            if body is None:
                return b""
            if isinstance(body, (bytes, bytearray)):
                return bytes(body)
            return body
        resp = await self._dispatch_platform_method(
            adapter=adapter,
            acct=acct,
            token=token,
            method=method,
            chat_id=chat_id,
            payload=payload,
            file_bytes=file_bytes,
        )
        return resp.status < 400

    def decrypt_settings(self, settings):
        return self._decrypt_model_settings(settings)

def ensure_local_conductor(app: web.Application):
    existing = app.get("local_conductor")
    if existing is None:
        app["local_conductor"] = LocalConductor(app)
        existing = app["local_conductor"]
    app["prototype_is_local"] = True
    existing.ensure_keys()
    if not app.get("_local_conductor_routes"):
        setup_local_conductor_routes(app)
        app["_local_conductor_routes"] = True
    return existing

def setup_local_conductor_routes(app: web.Application) -> None:
    lc: LocalConductor = app["local_conductor"]
    app.router.add_post("/consul/ping", lc.handle_consul_ping)
    app.router.add_post("/consul/info", lc.handle_consul_info)
    app.router.add_post("/pair/start", lc.handle_pair_start)
    app.router.add_post("/pair/status", lc.handle_pair_status)
    app.router.add_post("/pair/cancel", lc.handle_pair_cancel)
    for adapter in lc.platforms.values():
        adapter.register_routes(app)
    app.router.add_post("/model/create/{prototype_id}/{model_id}", lc.handle_model_create)
    app.router.add_post("/model/remove/{prototype_id}/{model_id}", lc.handle_model_remove)
    app.router.add_post("/model/remove_chat/{chat_id}/{model_id}", lc.handle_model_remove_chat)
    app.router.add_post("/model/remove_acct/{acct_id}/{model_id}", lc.handle_model_remove_acct)
    app.router.add_post("/model/update_acct/{acct_id}/{model_id}", lc.handle_model_update_acct)
    app.router.add_post("/model/update_acct/{acct_id}", lc.handle_model_delete_acct)
    app.router.add_post("/model/sync_chats/{prototype_id}/{model_id}", lc.handle_model_sync_chats)
    app.router.add_post("/model/update_period/{prototype_id}/{model_id}", lc.handle_model_update_period)
    app.router.add_post("/prototype/update/{prototype_id}", lc.handle_prototype_update)
    app.router.add_post("/prototype/delete/{prototype_id}", lc.handle_prototype_delete)
    app.router.add_post("/gateway/list_chats/{prototype_id}/{model_id}", lc.handle_gateway_list_chats)
    app.router.add_post("/gateway/list_chats/{prototype_id}/{model_id}/{acct_id}", lc.handle_gateway_list_chats)
    app.router.add_post("/gateway/{method}/{chat_id}/{prototype_id}/{model_id}/{acct_id}", lc.handle_gateway)
    app.router.add_post("/reply/{method}/{chat_id}/{request_id}/{prototype_id}/{model_id}", lc.handle_reply)
