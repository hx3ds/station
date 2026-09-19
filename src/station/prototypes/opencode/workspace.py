import asyncio
import os

from station import logger

from station.errors import ExternalError

from .config import (
    clear_workspace_override,
    is_windows_interop_path,
    load_workspace_override,
    save_workspace_override,
)

class OpenCodeWorkspace:
    async def _handle_workspace_command(self, *, args, chat_id, acct_id, platform="", chat_type=""):
        raw = args.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1].strip()
        if not raw:
            await self.send_outbound(
                text=self._workspace_status_text(),
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return

        previous = load_workspace_override(self.storage_dir)
        if raw.lower() in {"reset", "default", "clear"}:
            clear_workspace_override(self.storage_dir)
            if not await self._apply_workspace_change(
                previous=previous,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            ):
                return
            await self.send_outbound(
                text="Workspace reset to %s" % self._workspace_root(),
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return

        try:
            resolved = self._resolve_workspace_path(raw)
        except (ValueError, OSError) as exc:
            await self.send_outbound(
                text="Invalid workspace: %s" % exc,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return
        if resolved == self._workspace_root() and previous == resolved:
            await self.send_outbound(
                text="Workspace is already %s" % resolved,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return
        save_workspace_override(self.storage_dir, resolved)
        if not await self._apply_workspace_change(
            previous=previous,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        ):
            return
        logger.info("OpenCode workspace set model_id=%s workspace=%s", self.model_id, resolved)
        await self.send_outbound(
            text="Workspace set to %s" % resolved,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )

    async def _apply_workspace_change(self, *, previous, chat_id, acct_id, platform="", chat_type=""):
        try:
            await self._restart_gateway()
        except (OSError, RuntimeError, asyncio.TimeoutError) as e:
            if previous:
                save_workspace_override(self.storage_dir, previous)
            else:
                clear_workspace_override(self.storage_dir)
            logger.error("OpenCode workspace change failed model_id=%s error=%s", self.model_id, e)
            await self.send_outbound(
                text="Failed to change workspace: %s" % e,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return False
        return True

    def _workspace_status_text(self):
        current = self._workspace_root()
        default = self._default_workspace_dir()
        override = load_workspace_override(self.storage_dir)
        lines = ["Workspace: %s" % current]
        if override:
            lines.append("Saved override: %s" % override)
            if default != current:
                lines.append("Default: %s" % default)
            lines.append("Send /workspace reset to restore the default.")
        else:
            lines.append("Send /workspace <path> to change it.")
        return "\n".join(lines)

    def _default_workspace_dir(self):
        return self._build_settings(workspace_override="").workspace_dir

    def _resolve_workspace_path(self, raw):
        text = raw.strip()
        if not text:
            raise ExternalError("path is required")
        if is_windows_interop_path(text):
            raise ExternalError("Windows/interop paths are not allowed; use a Linux path")
        path = os.path.expanduser(text)
        if not os.path.isabs(path):
            path = os.path.join(self._workspace_root(), path)
        resolved = os.path.realpath(path)
        if is_windows_interop_path(resolved):
            raise ExternalError("Windows/interop paths are not allowed; use a Linux path")
        if os.path.exists(resolved) and not os.path.isdir(resolved):
            raise ExternalError("%s is not a directory" % resolved)
        os.makedirs(resolved, exist_ok=True)
        return resolved

    def _workspace_root(self):
        override = load_workspace_override(self.storage_dir)
        if override:
            return os.path.expanduser(override)
        if self._settings is not None:
            return self._settings.workspace_dir
        return self._default_workspace_dir()
