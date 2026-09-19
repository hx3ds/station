import asyncio

from station.prototypes.prototype import Prototype

from .auth_flow import GrokAuthFlow
from .config import GrokLaunchSettings
from .turn import GrokTurn
from .voice_bridge import GrokVoiceBridge

class GrokPrototype(GrokAuthFlow, GrokVoiceBridge, GrokTurn, Prototype):
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
        return GrokLaunchSettings.from_model_settings(raw, config_file=self.config_file)

    async def _handle_kind_command(self, *, cmd, args, chat_id, acct_id, reply_to, model_settings, platform="", chat_type=""):
        if cmd == "/start":
            return True
        if cmd not in {"/login", "/logout", "/generate", "/usage"}:
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
        if await self._dispatch_kind_command(
            ctx.data,
            ctx.model_settings,
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            text=ctx.text,
        ):
            return

        photo_attachment = self._find_attachment(ctx.attachments, types={"photo"}, require_file_id=True)
        voice_attachment = self._find_attachment(ctx.attachments, types={"voice", "audio"}, require_file_id=True)

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
        )
