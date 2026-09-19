from pathlib import Path

from station import logger
from station.prototypes.gateway_lifecycle import PrototypeGateway
from station.prototypes.prototype import Prototype

from .attachments import PiAttachments
from .config import PiLaunchSettings
from .gateway_client import PiGatewayProcess
from .worker import PiWorker

class PiPrototype(PiAttachments, PiWorker, PrototypeGateway, Prototype):
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
        )

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
        message, images = await self._build_prompt_payload(
            text=ctx.text,
            attachments=ctx.attachments,
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
        )
