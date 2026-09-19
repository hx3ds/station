import asyncio
import os

from station import logger
from station.prototypes.device_auth import PrototypeDeviceAuth, format_device_login_start

from . import auth_store
from .oauth import begin_device_login, ensure_fresh_credentials, poll_device_code_token


class GrokAuthFlow(PrototypeDeviceAuth):
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

    async def _start_login(self, *, acct_id, chat_id="", reply_to=None, platform="", chat_type=""):
        async def begin():
            return await asyncio.to_thread(begin_device_login)

        async def poll(pending):
            return await asyncio.to_thread(poll_device_code_token, pending)

        async def on_success(creds):
            auth_store.save_credentials(self.storage_dir, acct_id, creds)
            if chat_id:
                await self.send_outbound(
                    text="Signed in to Grok. Send a message to get a completion.",
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                )

        def start_message(pending):
            return format_device_login_start(
                title="Sign in to Grok (xAI device login):",
                verification_uri=pending.verification_uri,
                user_code=pending.user_code,
                verification_uri_complete=pending.verification_uri_complete,
                commands="/login · /logout · /usage · /generate",
            )

        return await self._run_device_auth(
            acct_id=acct_id,
            chat_id=chat_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
            name="Grok",
            begin=begin,
            poll=poll,
            on_success=on_success,
            start_message=start_message,
        )

    async def _handle_auth_command(self, *, cmd, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        if cmd == "/logout":
            auth_store.delete_credentials(self.storage_dir, acct_id)
            await self._cancel_device_auth(acct_id=acct_id)
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
