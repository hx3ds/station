from aiohttp import web
from station.client.conductor import send_proxy, send_outbound
from station.errors import ExternalError, InternalError
from station.prototypes.attachments import (
    PrototypeAttachments,
    find_attachment,
    normalize_attachments,
)
from station.prototypes.boundary import (
    ext_str,
    validate_inbound_data,
    validate_inbound_message_fields,
)
from .echo import PrototypeEcho
from .pfs import PrototypeFS
from .webrtc import PrototypeWebRTC
from .discord_voice import PrototypeDiscordVoice
from .inbound import InboundMessageContext, PrototypeInbound
from station import logger

class Prototype(PrototypeEcho, PrototypeWebRTC, PrototypeDiscordVoice, PrototypeAttachments, PrototypeFS, PrototypeInbound):
    def __init__(
        self,
        app,
        prototype_id,
        model_id,
        model_settings=None,
        *,
        config_file=None,
        secret_file=None,
        client_context=None,
    ):
        if prototype_id is None:
            raise InternalError("prototype_id is required")
        if model_id is None:
            raise InternalError("model_id is required")
        self.app = app
        self.prototype_id = prototype_id
        self.model_id = model_id
        self._model_settings = model_settings if model_settings is not None else {}
        self.config_file = config_file
        self.secret_file = secret_file
        if client_context is not None:
            self.client_context = client_context
        else:
            registry = app["tenants"]
            tenant = registry.get(prototype_id)
            if tenant is None or tenant.client_context is None:
                raise InternalError("client_context required")
            self.client_context = tenant.client_context
        self._fs = None
        self._settings = None
        self._inbound_bind()

    @property
    def model_settings(self):
        return self._model_settings

    @property
    def prototype_config(self):
        registry = self.app.get("tenants")
        if registry is not None:
            tenant = registry.get(self.prototype_id)
            if tenant is not None:
                from station.config.config import PrototypeConfig

                return PrototypeConfig(
                    id=tenant.id,
                    token=tenant.token,
                    kind=tenant.kind,
                    config_file=tenant.config_file,
                    secret_file=tenant.secret_file,
                    ava=tenant.ava,
                    reply_to=tenant.reply_to,
                )
        return self.app["config"].prototype

    @property
    def token(self):
        ctx = self.client_context
        if ctx is not None and ctx.token:
            return ctx.token
        return self.prototype_config.token

    async def send_proxy(self, method="send_message", params=None, files=None, request_id=None, chat_id=None, acct_id=None, **kwargs):
        return await send_proxy(
            self.client_context,
            model_id=self.model_id,
            token=self.token,
            method=method,
            params=params,
            files=files,
            request_id=request_id,
            chat_id=chat_id,
            acct_id=acct_id,
            **kwargs
        )

    async def send_outbound(
        self,
        *,
        text="",
        attachments=None,
        reply_to=None,
        chat_id=None,
        acct_id=None,
        request_id=None,
        platform="",
        chat_type="",
        keyboard=None,
    ):
        return await send_outbound(
            self,
            text=text,
            attachments=attachments,
            reply_to=reply_to,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
            platform=platform,
            chat_type=chat_type,
            keyboard=keyboard,
        )

    def _message_attachments(self, data):
        return normalize_attachments(data.get("attachments"))

    def _find_attachment(self, attachments, *, types=None, require_file_id=False):
        return find_attachment(attachments, types=types, require_file_id=require_file_id)

    async def _ensure_attachment_ready(self, attachment):
        file_id = attachment.get("file_id")
        if not file_id:
            return None
        meta = await self.download_chat_file(file_id=file_id)
        if not meta:
            return None
        return self.resolve_file_id(file_id)

    def _slash_command(self, text):
        t = ext_str("text", text if text is not None else "")
        if not t.startswith("/"):
            return "", ""
        parts = t.split(None, 1)
        cmd = parts[0].split("@", 1)[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return cmd, args

    async def _open_inbound_context(
        self,
        data,
        model_id,
        model_settings,
        *,
        chat_id=None,
        acct_id=None,
        request_id=None,
    ):
        if await self._prepare_inbound(
            data,
            model_id,
            model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        ):
            return None
        fields = validate_inbound_message_fields(data)
        attachments = await self._materialize_attachments(
            normalize_attachments(fields["attachments"])
        )
        return InboundMessageContext(
            data=data,
            fields=fields,
            attachments=attachments,
            model_id=model_id,
            model_settings=model_settings if model_settings is not None else {},
            chat_id="" if chat_id is None else ext_str("chat_id", chat_id),
            acct_id="" if acct_id is None else ext_str("acct_id", acct_id),
            request_id=request_id,
        )

    def _passes_inbound_guards(self, ctx):
        acct_id = ext_str("acct_id", ctx.acct_id)
        if not acct_id:
            raise ExternalError("acct_id is required")
        ctx.acct_id = acct_id
        if not ctx.text and not ctx.attachments and ctx.data.get("raw") is None:
            return False
        return True

    async def _prepare_inbound(self, data, model_id, model_settings, *, chat_id=None, acct_id=None, request_id=None):
        if data.get("is_expired"):
            await self.on_subscription_expired(
                model_id,
                data,
                model_settings,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
            )
            return True
        if await self.handle_webrtc_message(
            data,
            model_id=model_id,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        ):
            return True
        if await self.handle_discord_voice_message(
            data,
            model_id=model_id,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        ):
            return True
        return False

    async def _handle_kind_command(self, *, cmd, args, chat_id, acct_id, reply_to, model_settings, platform="", chat_type=""):
        return False

    async def _dispatch_kind_command(self, data, model_settings, *, chat_id=None, acct_id=None, text=None):
        fields = validate_inbound_message_fields(data)
        source = fields["text"] if text is None else text
        cmd, args = self._slash_command(source)
        if not cmd:
            return False
        msg_id = fields["msg_id"]
        reply_to = "" if msg_id is None else str(msg_id)
        if reply_to.startswith("callback:") and fields["reply_to"]:
            reply_to = fields["reply_to"]
        elif reply_to.startswith("callback:"):
            reply_to = ""
        return await self._handle_kind_command(
            cmd=cmd,
            args=args,
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            model_settings=model_settings,
            platform=fields["platform"],
            chat_type=fields["chat_type"],
        )

    async def _handle_command_impl(self, data, model_id, model_settings, chat_id=None, acct_id=None, request_id=None):
        if await self._dispatch_kind_command(data, model_settings, chat_id=chat_id, acct_id=acct_id):
            return None
        fields = validate_inbound_message_fields(data)
        cmd, _ = self._slash_command(fields["text"])
        if cmd == "/start":
            proto_type = ""
            ctx = self.client_context
            if ctx is not None:
                proto_type = (ctx.prototype_type or "").strip().lower()
            if proto_type == "subscription":
                await self.send_outbound(
                    text="hi",
                    chat_id=chat_id,
                    request_id=request_id,
                    acct_id=acct_id,
                    reply_to=fields["msg_id"],
                    platform=fields["platform"],
                    chat_type=fields["chat_type"],
                )
            return
        if cmd == "/reset":
            await self.send_outbound(
                text="reset completed",
                chat_id=chat_id,
                request_id=request_id,
                acct_id=acct_id,
                platform=fields["platform"],
                chat_type=fields["chat_type"],
            )
            return
        return None

    async def _handle_message_impl(self, data, model_id, model_settings, chat_id=None, acct_id=None, request_id=None):
        ctx = await self._open_inbound_context(
            data,
            model_id,
            model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )
        if ctx is None:
            return
        if not self._passes_inbound_guards(ctx):
            return
        await self._handle_inbound_context(ctx)

    async def _handle_inbound_context(self, ctx):
        echo_file = self.prototype_config.ava or ctx.attachments != []

        await self.echo_inbound(
            ctx.data,
            echo_file=echo_file,
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            request_id=ctx.request_id,
        )

    async def on_subscription_expired(self, model_id, data, model_settings, chat_id=None, acct_id=None, request_id=None):
        fields = validate_inbound_message_fields(data)
        if chat_id:
            await self.echo_inbound(
                data,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
            )
        if self.client_context:
            await self.send_outbound(
                text="subscription expired",
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
                platform=fields["platform"],
                chat_type=fields["chat_type"],
            )

    async def handle_event(self, data, model_id, model_settings, chat_id=None, acct_id=None, request_id=None):
        data = validate_inbound_data(data, label="event data")
        logger.debug("event received model_id=%s", model_id)
        chat_opened = data.get("chat_opened")
        if chat_opened is not None:
            from station.prototypes.boundary import ext_dict

            chat_opened = ext_dict("chat_opened", chat_opened)
            await self.handle_discord_voice_chat_opened(
                chat_opened,
                model_id=model_id,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
            )

    async def handle_pause(self, request):
        raise ExternalError("Model does not support pause")

    async def handle_resume(self, request):
        raise ExternalError("Model does not support resume")

    async def handle_rewind(self, request):
        raise ExternalError("Model does not support rewind")

    async def handle_admin_memory(self, body):
        raise ExternalError("Model does not support admin memory")

    async def reload(self, model_settings=None):
        self._model_settings = model_settings
        return None
