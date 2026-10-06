from pathlib import Path

from station import logger
from station.prototypes.gateway_lifecycle import PrototypeGateway
from station.prototypes.launch_settings import extra_env_has_provider_key
from station.prototypes.model_config import PrototypeModelConfig, load_overlay
from station.prototypes.prototype import Prototype
from station.prototypes.speech import PrototypeSpeech

from .attachments import PiAttachments
from .config import PiLaunchSettings
from .gateway_client import PiGatewayProcess
from .worker import PiWorker

class PiPrototype(PrototypeModelConfig, PiAttachments, PiWorker, PrototypeSpeech, PrototypeGateway, Prototype):
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
        self._init_bridge_worker(worker_name="pi-worker")

    def _gateway_label(self):
        return "Pi"

    def _build_settings(self):
        return PiLaunchSettings.from_model_settings(
            self.model_settings,
            default_workspace=self.storage_dir,
            default_session_root=str(Path(self.storage_dir) / "pi_sessions"),
            default_agent_home=str(Path(self.storage_dir) / "pi_home"),
            config_file=self.config_file,
            secret_file=self.secret_file,
            overlay=load_overlay(self.storage_dir),
        )

    def _config_api_key_path(self):
        settings = self._build_settings()
        provider = (settings.provider or "").strip().lower()
        mapping = {
            "openrouter": "keys.openrouter_api_key",
            "openai": "keys.openai_api_key",
            "anthropic": "keys.anthropic_api_key",
            "groq": "keys.groq_api_key",
            "xai": "keys.xai_api_key",
            "mistral": "keys.mistral_api_key",
            "google": "keys.google_api_key",
            "gemini": "keys.gemini_api_key",
        }
        return mapping.get(provider, "keys.openai_api_key")

    def _config_requires_key(self, settings):
        return not extra_env_has_provider_key(settings.extra_env)

    async def _start_gateway_locked(self):
        settings = self._build_settings()
        Path(settings.workspace_dir).expanduser().mkdir(parents=True, exist_ok=True)
        gateway = PiGatewayProcess(settings=settings)
        await gateway.start()
        self._settings = settings
        logger.info(
            "Pi gateway ready model_id=%s command=%s workspace=%s",
            self.model_id,
            settings.command,
            settings.workspace_dir,
        )
        return gateway

    async def _handle_kind_command(self, *, cmd, args, chat_id, acct_id, reply_to, model_settings, platform="", chat_type=""):
        return cmd == "/start"

    async def _handle_inbound_context(self, ctx):
        remaining, transcript, had_audio = await self._prepare_hosted_voice_input(ctx.attachments)
        combined = self._combine_user_text(text=ctx.text, transcript_text=transcript)
        message, images = await self._build_prompt_payload(
            text=combined,
            attachments=remaining,
            acct_id=ctx.acct_id,
            chat_id=ctx.chat_id,
        )
        if not message and not images:
            return

        await self._enqueue_turn(
            message=message,
            images=images,
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
            reply_with_voice=self._should_send_voice_reply(
                incoming_had_audio=had_audio,
                reply_mode=self._voice_reply_mode(),
            ),
        )
