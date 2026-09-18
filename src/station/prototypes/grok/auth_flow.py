import asyncio
import os
from dataclasses import dataclass, field

from station import logger

from . import auth_store
from .oauth import begin_device_login, ensure_fresh_credentials, poll_device_code_token

@dataclass(slots=True)
class ChatAuthState:
    pending: object = None
    poll_task: object = None
    reply_to: object = None
    guard: asyncio.Lock = field(default_factory=asyncio.Lock)

class GrokAuthFlow:
    def _resolve_api_key(self, settings):
        if settings.api_key:
            return settings.api_key
        value = os.getenv("XAI_API_KEY")
        if value and value.strip():
            return value.strip()
        return ""

    async def _resolve_access_token(self, *, acct_id, settings):
        api_key = self._resolve_api_key(settings)
        if api_key:
            return api_key
        creds = await self._load_oauth_credentials(acct_id=acct_id, settings=settings)
        if creds is None:
            return ""
        auth_store.save_credentials(self.storage_dir, acct_id, creds)
        return creds.access

    async def _load_oauth_credentials(self, *, acct_id, settings):
        creds = auth_store.load_credentials(self.storage_dir, acct_id)
        if creds is None and settings.reuse_grok_cli_auth:
            creds = auth_store.load_grok_cli_credentials()
            if creds is not None:
                auth_store.save_credentials(self.storage_dir, acct_id, creds)
        if creds is None:
            return None
        try:
            return await asyncio.to_thread(ensure_fresh_credentials, creds)
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error("Grok OAuth refresh failed acct_id=%s error=%s", acct_id, e)
            return None

    async def _await_cancelled_task(self, task):
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _get_auth_state(self, acct_id):
        async with self._auth_states_guard:
            state = self._auth_states.get(acct_id)
            if state is None:
                state = ChatAuthState()
                self._auth_states[acct_id] = state
            return state

    async def _poll_device_login(self, *, acct_id, chat_id, pending, reply_to=None, platform="", chat_type=""):
        try:
            creds = await asyncio.to_thread(poll_device_code_token, pending)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error("Grok device OAuth poll failed acct_id=%s error=%s", acct_id, e)
            state = await self._get_auth_state(acct_id)
            async with state.guard:
                if state.pending is pending:
                    state.pending = None
                    state.poll_task = None
                    state.reply_to = None
            if chat_id:
                await self.send_outbound(
                    text="Grok OAuth failed: %s\nSend /login to try again." % e,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                )
            return

        state = await self._get_auth_state(acct_id)
        async with state.guard:
            if state.pending is not pending:
                return
            auth_store.save_credentials(self.storage_dir, acct_id, creds)
            state.pending = None
            state.poll_task = None
            state.reply_to = None
        if chat_id:
            await self.send_outbound(
                text="Signed in to Grok. Send a message to get a completion.",
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )

    async def _start_login(self, *, acct_id, chat_id="", reply_to=None, platform="", chat_type=""):
        state = await self._get_auth_state(acct_id)
        async with state.guard:
            old_task = state.poll_task
            state.poll_task = None
            state.pending = None
            state.reply_to = None
        await self._await_cancelled_task(old_task)
        try:
            pending = await asyncio.to_thread(begin_device_login)
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            logger.error("Grok device OAuth login start failed acct_id=%s error=%s", acct_id, e)
            return "Grok OAuth login could not start: %s" % e
        async with state.guard:
            state.pending = pending
            state.reply_to = reply_to
            state.poll_task = asyncio.create_task(
                self._poll_device_login(
                    acct_id=acct_id,
                    chat_id=chat_id,
                    pending=pending,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                )
            )
        lines = [
            "Sign in to Grok (xAI device login):",
            "Open %s on any device and enter code: %s" % (pending.verification_uri, pending.user_code),
        ]
        if pending.verification_uri_complete:
            lines.append("Or open: %s" % pending.verification_uri_complete)
        lines.extend(
            [
                "",
                "Waiting for authorization...",
                "Commands: /login · /logout · /usage · /generate",
            ]
        )
        return "\n".join(lines)

    async def _handle_auth_command(self, *, cmd, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        if cmd == "/logout":
            auth_store.delete_credentials(self.storage_dir, acct_id)
            state = await self._get_auth_state(acct_id)
            async with state.guard:
                old_task = state.poll_task
                state.poll_task = None
                state.pending = None
                state.reply_to = None
            await self._await_cancelled_task(old_task)
            await self.send_outbound(
                text="Signed out of Grok OAuth for this account.",
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return
        reply = await self._start_login(
            acct_id=acct_id,
            chat_id=chat_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )
        await self.send_outbound(
            text=reply,
            chat_id=chat_id,
            acct_id=acct_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )
