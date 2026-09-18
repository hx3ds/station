import json
from pathlib import Path

from station import logger
from station.config.config import merge_nested
from station.prototypes.gateway_lifecycle import PrototypeGateway
from station.prototypes.prototype import Prototype

from .attachments import HermesAttachments
from .auth_flow import HermesAuthFlow
from .config import HermesLaunchSettings
from .gateway_client import HermesGatewayProcess
from .slash import HermesSlash
from .voice import HermesVoice
from .worker import HermesWorker

class HermesPrototype(HermesAuthFlow, HermesAttachments, HermesVoice, HermesSlash, HermesWorker, PrototypeGateway, Prototype):
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
        self._init_bridge_worker(worker_name="hermes-worker")
        self._init_auth_flow()
        self._hermes_home = None

    def _gateway_label(self):
        return "Hermes"

    def _clear_gateway_state(self):
        self._hermes_home = None

    def _build_settings(self):
        return HermesLaunchSettings.from_model_settings(
            self.model_settings,
            default_workspace=self.storage_dir,
            config_file=self.config_file,
        )

    def _launch_settings(self):
        if self._settings is not None:
            return self._settings
        return self._build_settings()

    async def _start_gateway_locked(self):
        settings = self._build_settings()
        Path(settings.workspace_dir).expanduser().mkdir(parents=True, exist_ok=True)
        if not settings.hermes_root.exists():
            raise FileNotFoundError("Hermes root not found: %s" % settings.hermes_root)
        if settings.uses_local_llm() and not settings.local_llm_base_url:
            raise RuntimeError(
                "Hermes local LLM requires [local_llm].base_url or LOCAL_LLM_BASE_URL "
                "(OpenAI-compatible, ending in /v1)"
            )

        hermes_home = Path(self.storage_dir).resolve() / "hermes_home"
        self._write_hermes_config(hermes_home=hermes_home, settings=settings)
        if settings.uses_local_llm():
            logger.info(
                "Hermes local llm config written path=%s base_url=%s model=%s provider=%s",
                hermes_home / "config.yaml",
                settings.local_llm_base_url,
                settings.model,
                settings.gateway_provider(),
            )

        gateway = HermesGatewayProcess(settings=settings, hermes_home=hermes_home)
        await gateway.start()
        self._settings = settings
        self._hermes_home = hermes_home
        logger.info(
            "Hermes gateway started model_id=%s provider=%s model=%s workspace=%s",
            self.model_id,
            settings.gateway_provider(),
            settings.model,
            settings.workspace_dir,
        )
        return gateway

    def _write_hermes_config(self, *, hermes_home, settings):
        hermes_home.mkdir(parents=True, exist_ok=True)
        hermes_approvals = settings.approvals_mode or "off"
        if hermes_approvals == "always":
            hermes_approvals = "off"
        config = {
            "approvals": {"mode": hermes_approvals},
            "terminal": {"cwd": settings.workspace_dir},
        }
        model_cfg = {}
        provider = settings.gateway_provider()
        if provider:
            model_cfg["provider"] = provider
        if settings.model:
            model_cfg["default"] = settings.model
        if settings.uses_local_llm():
            if settings.local_llm_base_url:
                model_cfg["base_url"] = settings.local_llm_base_url
            if settings.local_llm_api_key:
                model_cfg["api_key"] = settings.local_llm_api_key
            # Hermes agent_init floors context at 64k; a 32k report refuses to start.
            model_cfg["context_length"] = max(settings.local_llm_context_window or 0, 65536)
            if settings.local_llm_max_output:
                model_cfg["max_tokens"] = settings.local_llm_max_output
            model_cfg["reasoning_effort"] = settings.reasoning_effort or "none"
            # Local serial backends (max_concurrent_llm=1): title gen steals the slot
            # and thinking-only turns come back as empty content.
            config["auxiliary"] = {"title_generation": {"enabled": False}}
            if settings.local_llm_base_url:
                config["custom_providers"] = [
                    {
                        "name": settings.gateway_provider(),
                        "base_url": settings.local_llm_base_url,
                        "api_key": settings.local_llm_api_key or "local",
                        "model": settings.model,
                        "extra_body": {
                            "chat_template_kwargs": {"enable_thinking": False},
                        },
                    }
                ]
        if model_cfg:
            config["model"] = model_cfg
        config = merge_nested(config, settings.config_overrides)
        (hermes_home / "config.yaml").write_text(json.dumps(config, indent=2), encoding="utf-8")

    async def _handle_kind_command(self, *, cmd, args, chat_id, acct_id, reply_to, model_settings, platform="", chat_type=""):
        if cmd == "/start":
            await self._ensure_provider_auth(
                acct_id=acct_id,
                chat_id=chat_id,
                text="",
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return True
        if cmd not in {"/login", "/logout"}:
            return False
        await self._handle_auth_command(
            cmd=cmd,
            args=args,
            chat_id=chat_id,
            acct_id=acct_id,
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
        if not await self._ensure_provider_auth(
            acct_id=ctx.acct_id,
            chat_id=ctx.chat_id,
            text=ctx.text,
            reply_to=ctx.reply_to,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
        ):
            return

        settings = self._launch_settings()
        if settings.uses_local_llm():
            if not ctx.text and not ctx.attachments:
                return
            await self._enqueue_message(
                raw_text=ctx.text,
                combined_text=ctx.text,
                attachments=ctx.attachments,
                reply_with_voice=False,
                chat_id=ctx.chat_id,
                acct_id=ctx.acct_id,
                platform=ctx.platform,
                chat_type=ctx.chat_type,
            )
            return

        voice_input = await self._prepare_voice_input(attachments=ctx.attachments)
        combined_text = self._combine_user_text(text=ctx.text, voice_input=voice_input)
        routed_attachments = voice_input.remaining_attachments
        reply_with_voice = self._should_send_voice_reply(incoming_had_audio=voice_input.had_audio_input)

        if not combined_text and not routed_attachments:
            if voice_input.had_audio_input:
                await self.send_outbound(
                    text="Hermes could not transcribe the audio message. Check the STT provider or send a clearer recording.",
                    chat_id=ctx.chat_id,
                    acct_id=ctx.acct_id,
                    request_id=ctx.request_id,
                    platform=ctx.platform,
                    chat_type=ctx.chat_type,
                )
            return

        await self._enqueue_message(
            raw_text=ctx.text,
            combined_text=combined_text,
            attachments=routed_attachments,
            reply_with_voice=reply_with_voice,
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
        )
