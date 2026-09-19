import os

from station import logger
from station.prototypes.gateway_lifecycle import PrototypeGateway
from station.prototypes.prototype import Prototype

from .attachments import OpenCodeAttachments
from .auth_flow import OpenCodeAuthFlow
from .config import OpenCodeLaunchSettings, load_workspace_override, write_local_llm_opencode_config
from .server_client import OpenCodeServerProcess
from .worker import OpenCodeWorker
from .workspace import OpenCodeWorkspace

class OpenCodePrototype(OpenCodeAuthFlow, OpenCodeWorkspace, OpenCodeWorker, OpenCodeAttachments, PrototypeGateway, Prototype):
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
            workspace_override = load_workspace_override(self.storage_dir)
        return OpenCodeLaunchSettings.from_model_settings(
            self.model_settings,
            default_workspace=self.storage_dir,
            config_file=self.config_file,
            workspace_override=workspace_override,
        )

    async def _start_gateway_locked(self):
        settings = self._build_settings()
        os.makedirs(settings.workspace_dir, exist_ok=True)
        if settings.uses_local_llm():
            if not settings.local_llm_base_url:
                raise RuntimeError(
                    "OpenCode local LLM requires [local_llm].base_url or LOCAL_LLM_BASE_URL "
                    "(OpenAI-compatible, ending in /v1)"
                )
            config_path = os.path.join(self.storage_dir, "opencode.local.json")
            write_local_llm_opencode_config(config_path, settings=settings)
            settings.extra_env["OPENCODE_CONFIG"] = config_path
            settings.extra_env.setdefault("LOCAL_LLM_API_KEY", settings.local_llm_api_key or "local")
            settings.extra_env.setdefault("OPENAI_API_KEY", settings.local_llm_api_key or "local")
            # `opencode serve` reads project config from cwd and $HOME/.config/opencode.
            # OPENCODE_CONFIG alone is not enough when cwd is the user workspace.
            config_home = os.path.join(self.storage_dir, "opencode_home")
            config_dir = os.path.join(config_home, ".config", "opencode")
            os.makedirs(config_dir, exist_ok=True)
            with open(config_path, "r", encoding="utf-8") as src:
                payload = src.read()
            with open(os.path.join(config_dir, "opencode.json"), "w", encoding="utf-8") as dst:
                dst.write(payload)
            workspace_config = os.path.join(settings.workspace_dir, "opencode.json")
            with open(workspace_config, "w", encoding="utf-8") as dst:
                dst.write(payload)
            settings.extra_env["HOME"] = config_home
            settings.extra_env["XDG_CONFIG_HOME"] = os.path.join(config_home, ".config")
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

    def _launch_settings(self):
        if self._settings is not None:
            return self._settings
        return self._build_settings()

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
            reply_to=ctx.reply_to,
            platform=ctx.platform,
            chat_type=ctx.chat_type,
        ):
            return

        parts = await self._build_prompt_parts(
            text=ctx.text,
            attachments=ctx.attachments,
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
        )
