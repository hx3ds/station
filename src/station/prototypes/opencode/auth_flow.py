import aiohttp

from station import logger
from station.prototypes.boundary import ext_str
from station.prototypes.device_auth import PrototypeDeviceAuth, local_llm_auth_reply

from .config import OPENCODE_LOCAL_PROVIDER

XAI_PROVIDER_ID = "xai"
OAUTH_ERRORS = (aiohttp.ClientError, OSError, RuntimeError, TypeError)


class OpenCodeAuthFlow(PrototypeDeviceAuth):
    async def _handle_auth_command(self, *, cmd, chat_id, acct_id, reply_to=None, platform="", chat_type=""):
        settings = self._launch_settings()
        if settings.uses_local_llm():
            await self.send_outbound(
                text=local_llm_auth_reply(
                    "OpenCode",
                    logout=cmd == "/logout",
                    base_url=settings.local_llm_base_url,
                    provider=settings.gateway_provider(),
                    model=settings.model or settings.provider,
                ),
                chat_id=chat_id,
                acct_id=acct_id,
                reply_to=reply_to,
                platform=platform,
                chat_type=chat_type,
            )
            return
        if cmd == "/logout":
            await self._cancel_device_auth(acct_id=acct_id)
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
        state = await self._get_auth_state(acct_id)
        async with state.guard:
            in_progress = self._device_auth_in_progress(state)
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

    async def _start_login(self, *, acct_id, chat_id="", reply_to=None, platform="", chat_type=""):
        gateway_holder = {}

        async def begin():
            gateway = await self._ensure_gateway()
            gateway_holder["gateway"] = gateway
            method_index = await gateway.resolve_oauth_method_index(provider_id=XAI_PROVIDER_ID)
            auth = await gateway.oauth_authorize(provider_id=XAI_PROVIDER_ID, method=method_index)
            return {
                "method_index": method_index,
                "url": ext_str("oauth authorize url", auth.get("url")),
                "instructions": ext_str("oauth authorize instructions", auth.get("instructions")),
                "method": ext_str("oauth authorize method", auth.get("method")).lower() or "auto",
            }

        async def poll(pending):
            await gateway_holder["gateway"].oauth_callback(
                provider_id=XAI_PROVIDER_ID,
                method=pending["method_index"],
            )

        async def on_success(_result):
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

        def start_message(pending):
            lines = ["Sign in to OpenCode xAI (SuperGrok / device login):"]
            instructions = pending["instructions"]
            url = pending["url"]
            if instructions:
                lines.append(instructions)
            if url and (not instructions or url not in instructions):
                lines.append("Open: %s" % url)
            if pending["method"] == "code":
                lines.append("After approving, reply with the authorization code if OpenCode asks for one.")
            lines.extend(
                [
                    "",
                    "Waiting for OpenCode to finish OAuth...",
                    "Commands: /login · /logout",
                ]
            )
            return "\n".join(lines)

        return await self._run_device_auth(
            acct_id=acct_id,
            chat_id=chat_id,
            reply_to=reply_to,
            platform=platform,
            chat_type=chat_type,
            name="OpenCode xAI",
            begin=begin,
            poll=poll,
            on_success=on_success,
            start_message=start_message,
            errors=OAUTH_ERRORS,
        )
