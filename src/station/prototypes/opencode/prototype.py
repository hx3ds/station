import os

from station import logger
from station.prototypes.gateway_lifecycle import PrototypeGateway
from station.prototypes.model_config import PrototypeModelConfig, load_overlay
from station.prototypes.prototype import Prototype
from station.prototypes.speech import PrototypeSpeech
from station.prototypes.workspace import PrototypeWorkspace

from .attachments import OpenCodeAttachments
from .auth_flow import OpenCodeAuthFlow
from .config import OpenCodeLaunchSettings, write_local_llm_opencode_config
from .server_client import OpenCodeServerProcess
from .worker import OpenCodeWorker

class OpenCodePrototype(PrototypeModelConfig, OpenCodeAuthFlow, PrototypeWorkspace, OpenCodeWorker, OpenCodeAttachments, PrototypeSpeech, PrototypeGateway, Prototype):
    WORKSPACE_OVERRIDE_NAME = "opencode_workspace"

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
        self._init_bridge_worker(worker_name="opencode-worker")
        self._init_device_auth()
        self._applied_provider_auth = ""

    def _gateway_label(self):
        return "OpenCode"

    async def _before_teardown_gateway(self):
        await self._cancel_device_auth()

    def _clear_gateway_state(self):
        self._applied_provider_auth = ""

    def _build_settings(self, *, workspace_override=None):
        if workspace_override is None:
            workspace_override = self._loaded_workspace_override()
        return OpenCodeLaunchSettings.from_model_settings(
            self.model_settings,
            default_workspace=self.storage_dir,
            config_file=self.config_file,
            secret_file=self.secret_file,
            workspace_override=workspace_override,
            overlay=load_overlay(self.storage_dir),
        )

    def _config_api_key_path(self):
        settings = self._launch_settings()
        if settings.uses_local_llm():
            return "local_llm.api_key"
        return "keys.xai_api_key"

    def _config_accepts_key_message(self, settings):
        if settings.uses_local_llm():
            return False
        return not settings.api_key

    async def _start_gateway_locked(self):
        settings = self._build_settings()
        os.makedirs(settings.workspace_dir, exist_ok=True)
        config_home = os.path.join(self.storage_dir, "opencode_home")
        config_dir = os.path.join(config_home, ".config", "opencode")
        os.makedirs(config_dir, exist_ok=True)
        settings.extra_env["HOME"] = config_home
        settings.extra_env["XDG_CONFIG_HOME"] = os.path.join(config_home, ".config")
        if settings.uses_local_llm() and settings.local_llm_base_url:
            config_path = os.path.join(self.storage_dir, "opencode.local.json")
            write_local_llm_opencode_config(config_path, settings=settings)
            settings.extra_env["OPENCODE_CONFIG"] = config_path
            settings.extra_env.setdefault("LOCAL_LLM_API_KEY", settings.local_llm_api_key or "local")
            settings.extra_env.setdefault("OPENAI_API_KEY", settings.local_llm_api_key or "local")
            with open(config_path, "r", encoding="utf-8") as src:
                payload = src.read()
            with open(os.path.join(config_dir, "opencode.json"), "w", encoding="utf-8") as dst:
                dst.write(payload)
            workspace_config = os.path.join(settings.workspace_dir, "opencode.json")
            with open(workspace_config, "w", encoding="utf-8") as dst:
                dst.write(payload)
            logger.info(
                "OpenCode local llm config written path=%s base_url=%s model=%s provider=%s",
                config_path,
                settings.local_llm_base_url,
                settings.model,
                settings.gateway_provider(),
            )

        gateway = OpenCodeServerProcess(settings=settings)
        await gateway.start()
        self._settings = settings
        self._applied_provider_auth = ""
        logger.info(
            "OpenCode server started model_id=%s base_url=%s workspace=%s provider=%s",
            self.model_id,
            gateway.base_url,
            settings.workspace_dir,
            settings.gateway_provider(),
        )
        return gateway

    async def _after_gateway_started(self, gateway):
        settings = self._settings
        if settings is None:
            return
        if settings.uses_local_llm():
            await self._apply_provider_api_key(
                gateway,
                settings.gateway_provider(),
                settings.local_llm_api_key or settings.api_key or "local",
            )
            return
        if settings.uses_xai() and settings.api_key:
            await self._apply_xai_api_key(gateway, settings.api_key)

    def _kind_help_commands(self):
        return [
            ("/login", "Sign in"),
            ("/logout", "Sign out"),
            ("/workspace", "Show or set workspace"),
            ("/cwd", "Show or set workspace"),
        ]

    async def _handle_kind_command(self, *, cmd, args, chat_id, acct_id, reply_to, model_settings, platform="", chat_type=""):
        if cmd == "/start":
            return True
        if cmd not in {"/login", "/logout", "/workspace", "/cwd"}:
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
        await self._handle_workspace_command(
            args=args,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )
        return True

    async def _handle_inbound_context(self, ctx):
        if not await self._ensure_provider_auth(
            acct_id=ctx.acct_id,
            chat_id=ctx.chat_id,
            reply_to=ctx.reply_to,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
        ):
            return

        remaining, transcript, had_audio = await self._prepare_hosted_voice_input(ctx.attachments)
        combined = self._combine_user_text(text=ctx.text, transcript_text=transcript)
        parts = await self._build_prompt_parts(
            text=combined,
            attachments=remaining,
            acct_id=ctx.acct_id,
            chat_id=ctx.chat_id,
        )
        if not parts:
            return
        await self._enqueue_parts(
            parts=parts,
            chat_id=ctx.chat_id,
            acct_id=ctx.acct_id,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
            reply_with_voice=self._should_send_voice_reply(
                incoming_had_audio=had_audio,
                reply_mode=self._voice_reply_mode(),
            ),
        )
