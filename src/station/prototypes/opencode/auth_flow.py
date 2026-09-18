import asyncio
from dataclasses import dataclass, field

import aiohttp

from station import logger
from station.prototypes.boundary import ext_str

from .config import OPENCODE_LOCAL_PROVIDER

XAI_PROVIDER_ID = "xai"

@dataclass(slots=True)
class OauthLoginState:
    method_index: int = -1
    poll_task: object = None
    chat_id: str = ""
    acct_id: str = ""
    reply_to: object = None
    guard: asyncio.Lock = field(default_factory=asyncio.Lock)

class OpenCodeAuthFlow:
    async def _handle_auth_command(self, *, cmd, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        settings = self._launch_settings()
        if settings.uses_local_llm():
            if cmd == "/logout":
                text = "OpenCode local LLM uses an API key. There is no cloud session to sign out of."
            else:
                text = (
                    "OpenCode is using a local LLM at %s (provider %s, model %s). No cloud login is required."
                    % (
                        settings.local_llm_base_url or "LOCAL_LLM_BASE_URL",
                        settings.gateway_provider(),
                        settings.model or settings.provider,
                    )
                )
            await self.send_outbound(
                text=text,
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return
        if cmd == "/logout":
            await self._cancel_oauth_login()
            gateway = await self._ensure_gateway()
            await gateway.auth_remove(provider_id=XAI_PROVIDER_ID)
            self._applied_provider_auth = ""
            logger.info("OpenCode xAI auth removed acct_id=%s", acct_id)
            await self.send_outbound(
                text="Signed out of OpenCode xAI auth.",
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

    async def _ensure_provider_auth(self, *, acct_id, chat_id, reply_to=None, platform="", chat_type=""):
        settings = self._launch_settings()
        if settings.uses_local_llm():
            gateway = await self._ensure_gateway()
            key = settings.local_llm_api_key or settings.api_key or "local"
            await self._apply_provider_api_key(gateway, settings.gateway_provider() or OPENCODE_LOCAL_PROVIDER, key)
            return True
        return await self._ensure_xai_auth(
            acct_id=acct_id,
            chat_id=chat_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
        )

    async def _ensure_xai_auth(self, *, acct_id, chat_id, reply_to=None, platform="", chat_type=""):
        settings = self._launch_settings()
        if not settings.uses_xai():
            return True
        gateway = await self._ensure_gateway()
        if settings.api_key:
            await self._apply_xai_api_key(gateway, settings.api_key)
            return True
        if await gateway.provider_connected(XAI_PROVIDER_ID):
            return True
        async with self._oauth_login.guard:
            task = self._oauth_login.poll_task
            in_progress = task is not None and not task.done()
        if in_progress:
            await self.send_outbound(
                text="OpenCode xAI login already in progress. Finish the browser approval, or send /login again.",
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return False
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
        return False

    async def _apply_provider_api_key(self, gateway, provider_id, api_key):
        marker = "%s:api:%s" % (provider_id, api_key)
        if self._applied_provider_auth == marker:
            return
        await gateway.auth_set(provider_id=provider_id, body={"type": "api", "key": api_key})
        self._applied_provider_auth = marker

    async def _apply_xai_api_key(self, gateway, api_key):
        await self._apply_provider_api_key(gateway, XAI_PROVIDER_ID, api_key)

    async def _cancel_oauth_login(self):
        async with self._oauth_login.guard:
            task = self._oauth_login.poll_task
            self._oauth_login.poll_task = None
            self._oauth_login.method_index = -1
            self._oauth_login.reply_to = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _await_oauth_callback(self, *, gateway, method_index, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        try:
            await gateway.oauth_callback(provider_id=XAI_PROVIDER_ID, method=method_index)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, RuntimeError, TypeError) as e:
            logger.error("OpenCode xAI OAuth callback failed acct_id=%s error=%s", acct_id, e)
            async with self._oauth_login.guard:
                if self._oauth_login.poll_task is asyncio.current_task():
                    self._oauth_login.poll_task = None
                    self._oauth_login.method_index = -1
                    self._oauth_login.reply_to = None
            if chat_id:
                await self.send_outbound(
                    text="OpenCode xAI OAuth failed: %s\nSend /login to try again." % e,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                )
            return

        async with self._oauth_login.guard:
            if self._oauth_login.poll_task is not asyncio.current_task():
                return
            self._oauth_login.poll_task = None
            self._oauth_login.method_index = -1
            self._oauth_login.reply_to = None
        logger.info("OpenCode xAI OAuth completed acct_id=%s", acct_id)
        if chat_id:
            await self.send_outbound(
                text="Signed in to OpenCode xAI. Send a message to use Grok models.",
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )

    async def _start_login(self, *, acct_id, chat_id="", reply_to=None, platform="", chat_type=""):
        await self._cancel_oauth_login()
        try:
            gateway = await self._ensure_gateway()
            method_index = await gateway.resolve_oauth_method_index(provider_id=XAI_PROVIDER_ID)
            auth = await gateway.oauth_authorize(provider_id=XAI_PROVIDER_ID, method=method_index)

            url = ext_str("oauth authorize url", auth.get("url"))
            instructions = ext_str("oauth authorize instructions", auth.get("instructions"))
            method = ext_str("oauth authorize method", auth.get("method")).lower() or "auto"
        except (aiohttp.ClientError, OSError, RuntimeError, TypeError) as e:
            logger.error("OpenCode xAI OAuth login start failed acct_id=%s error=%s", acct_id, e)
            return "OpenCode xAI OAuth login could not start: %s" % e
        async with self._oauth_login.guard:
            self._oauth_login.method_index = method_index
            self._oauth_login.chat_id = chat_id
            self._oauth_login.acct_id = acct_id
            self._oauth_login.reply_to = reply_to
            self._oauth_login.poll_task = asyncio.create_task(
                self._await_oauth_callback(
                    gateway=gateway,
                    method_index=method_index,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                ),
                name="opencode-oauth-callback:%s:%s" % (acct_id, chat_id),
            )

        lines = ["Sign in to OpenCode xAI (SuperGrok / device login):"]
        if instructions:
            lines.append(instructions)
        if url and (not instructions or url not in instructions):
            lines.append("Open: %s" % url)
        if method == "code":
            lines.append("After approving, reply with the authorization code if OpenCode asks for one.")
        lines.extend(
            [
                "",
                "Waiting for OpenCode to finish OAuth...",
                "Commands: /login · /logout",
            ]
        )
        return "\n".join(lines)
