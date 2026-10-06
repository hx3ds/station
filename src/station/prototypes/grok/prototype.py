import asyncio

from station.prototypes.attachments import find_audio_attachment, find_image_attachment
from station.prototypes.boundary import validate_inbound_message_fields
from station.prototypes.chat_history import note_chat_opened
from station.prototypes.model_config import PrototypeModelConfig, load_overlay
from station.prototypes.prototype import Prototype

from .auth_flow import GrokAuthFlow
from .config import GrokLaunchSettings
from .turn import GrokTurn
from .voice_bridge import GrokVoiceBridge

class GrokPrototype(PrototypeModelConfig, GrokAuthFlow, GrokVoiceBridge, GrokTurn, Prototype):
    def __init__(self, app, prototype_id, model_id, model_settings=None, *, config_file=None, secret_file=None):
        super().__init__(
            app,
            prototype_id,
            model_id,
            model_settings=model_settings,
            config_file=config_file,
            secret_file=secret_file,
        )
        self._active_chat_requests = set()
        self._init_device_auth()
        self._realtime_by_call = {}
        self._realtime_by_chat = {}
        self._realtime_guard = asyncio.Lock()

    def _build_settings(self, model_settings=None):
        raw = self.model_settings if model_settings is None else model_settings
        return GrokLaunchSettings.from_model_settings(
            raw,
            config_file=self.config_file,
            secret_file=self.secret_file,
            overlay=load_overlay(self.storage_dir),
        )

    def _config_api_key_path(self):
        return "model.api_key"

    def _config_base_url_path(self):
        return "model.base_url"

    def _config_accepts_key_message(self, settings):
        return not settings.api_key

    def _note_arrived_chat(self, data, chat_id, acct_id):
        if not acct_id or not chat_id:
            return
        fields = validate_inbound_message_fields(data)
        note_chat_opened(
            self.storage_dir,
            acct_id,
            chat_id,
            platform=fields["platform"],
            chat_type=fields["chat_type"],
            user_id=fields["user_id"],
            sender=fields["sender"],
        )

    async def _handle_command_impl(self, data, model_id, model_settings, chat_id=None, acct_id=None, request_id=None):
        self._note_arrived_chat(data, chat_id, acct_id)
        return await super()._handle_command_impl(
            data,
            model_id,
            model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )

    async def _handle_message_impl(self, data, model_id, model_settings, chat_id=None, acct_id=None, request_id=None):
        self._note_arrived_chat(data, chat_id, acct_id)
        return await super()._handle_message_impl(
            data,
            model_id,
            model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )

    def _kind_help_commands(self):
        return [
            ("/login", "Sign in to Grok"),
            ("/logout", "Sign out"),
            ("/usage", "Show Grok usage"),
            ("/image", "Generate an image"),
            ("/video", "Generate a video"),
        ]

    async def _handle_kind_command(self, *, cmd, args, chat_id, acct_id, reply_to, model_settings, platform="", chat_type=""):
        if cmd == "/start":
            return True
        if cmd not in {"/login", "/logout", "/image", "/video", "/usage"}:
            return False
        if cmd in {"/login", "/logout"}:
            await self._handle_auth_command(
                cmd=cmd,
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return True
        settings = self._build_settings(model_settings)
        if cmd == "/usage":
            await self._handle_usage_command(
                chat_id=chat_id,
                acct_id=acct_id,
                settings=settings,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return True
        if cmd == "/video":
            await self._handle_generate_video_command(
                prompt=args.strip(),
                chat_id=chat_id,
                acct_id=acct_id,
                settings=settings,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return True
        await self._handle_generate_command(
            prompt=args.strip(),
            chat_id=chat_id,
            acct_id=acct_id,
            settings=settings,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )
        return True

    async def _handle_inbound_context(self, ctx):
        photo_attachment = find_image_attachment(ctx.attachments)
        voice_attachment = find_audio_attachment(ctx.attachments)

        await self._run_chat_turn(
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            settings=self._build_settings(ctx.model_settings),
            text=ctx.text,
            photo_attachment=photo_attachment,
            voice_attachment=voice_attachment,
            reply_to=ctx.reply_to,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
            user_id=ctx.user_id,
            sender=ctx.sender,
        )
