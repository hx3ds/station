import os

from station import logger
from station.prototypes.gateway_lifecycle import PrototypeGateway
from station.prototypes.model_config import PrototypeModelConfig, load_overlay
from station.prototypes.prototype import Prototype
from station.prototypes.speech import PrototypeSpeech
from station.prototypes.workspace import PrototypeWorkspace

from .attachments import CursorAttachments
from .client import start_cursor_gateway
from .config import CursorLaunchSettings
from .worker import CursorWorker


class CursorPrototype(PrototypeModelConfig, PrototypeWorkspace, CursorWorker, CursorAttachments, PrototypeSpeech, PrototypeGateway, Prototype):
    WORKSPACE_OVERRIDE_NAME = "cursor_workspace"

    def __init__(self, app, prototype_id, model_id, model_settings=None, *, config_file=None, secret_file=None):
        super().__init__(
            app,
            prototype_id,
            model_id,
            model_settings=model_settings,
            config_file=config_file,
            secret_file=secret_file,
        )
        self._init_gateway_lifecycle()
        self._init_bridge_worker(worker_name="cursor-worker")

    def _gateway_label(self):
        return "Cursor"

    def _build_settings(self, *, workspace_override=None):
        if workspace_override is None:
            workspace_override = self._loaded_workspace_override()
        return CursorLaunchSettings.from_model_settings(
            self.model_settings,
            default_workspace=self.storage_dir,
            default_state_root=os.path.join(self.storage_dir, "cursor_bridge"),
            config_file=self.config_file,
            secret_file=self.secret_file,
            workspace_override=workspace_override,
            overlay=load_overlay(self.storage_dir),
        )

    def _config_api_key_path(self):
        return "keys.cursor_api_key"

    def _config_requires_key(self, settings):
        return not settings.api_key

    async def _start_gateway_locked(self):
        settings = self._build_settings()
        if not settings.model:
            raise RuntimeError("Cursor model is required")
        os.makedirs(settings.workspace_dir, exist_ok=True)
        gateway = await start_cursor_gateway(
            workspace=settings.workspace_dir,
            state_root=settings.state_root,
        )
        self._settings = settings
        logger.info(
            "Cursor bridge started model_id=%s workspace=%s model=%s",
            self.model_id,
            settings.workspace_dir,
            settings.model,
        )
        return gateway

    async def _after_overlay_change(self):
        self._settings = None
        gateway = self._gateway
        if gateway is None:
            return
        for key, agent in list(gateway.agents.items()):
            gateway.agents.pop(key, None)
            try:
                await agent.close()
            except Exception as e:
                logger.error("unexpected where=cursor_agent_overlay_drop key=%s error=%s", key, e, exc_info=e)

    def _kind_help_commands(self):
        return [
            ("/workspace", "Show or set workspace"),
            ("/cwd", "Show or set workspace"),
        ]

    async def _handle_kind_command(self, *, cmd, args, chat_id, acct_id, reply_to, model_settings, platform="", chat_type=""):
        if cmd == "/start":
            return True
        if cmd not in {"/workspace", "/cwd"}:
            return False
        await self._handle_workspace_command(
            args=args,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )
        return True

    async def _handle_inbound_context(self, ctx):
        remaining, transcript, had_audio = await self._prepare_hosted_voice_input(ctx.attachments)
        combined = self._combine_user_text(text=ctx.text, transcript_text=transcript)
        message, image_paths = await self._build_prompt_payload(
            text=combined,
            attachments=remaining,
            acct_id=ctx.acct_id,
            chat_id=ctx.chat_id,
        )
        if not message and not image_paths:
            return
        await self._enqueue_turn(
            message=message,
            image_paths=image_paths,
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
            reply_with_voice=self._should_send_voice_reply(
                incoming_had_audio=had_audio,
                reply_mode=self._voice_reply_mode(),
            ),
        )
