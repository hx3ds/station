import asyncio
from dataclasses import dataclass, field

from station import logger


@dataclass(slots=True)
class DeviceAuthState:
    pending: object = None
    poll_task: object = None
    reply_to: object = None
    extra: dict = field(default_factory=dict)
    guard: asyncio.Lock = field(default_factory=asyncio.Lock)


class PrototypeDeviceAuth:
    def _init_device_auth(self):
        self._auth_states = {}
        self._auth_states_guard = asyncio.Lock()

    async def _get_auth_state(self, acct_id):
        async with self._auth_states_guard:
            state = self._auth_states.get(acct_id)
            if state is None:
                state = DeviceAuthState()
                self._auth_states[acct_id] = state
            return state

    async def _await_cancelled_task(self, task):
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def _reset_auth_state(self, state):
        old_task = state.poll_task
        state.poll_task = None
        state.pending = None
        state.reply_to = None
        state.extra.clear()
        return old_task

    async def _cancel_device_auth(self, *, acct_id=None):
        if acct_id is None:
            async with self._auth_states_guard:
                states = list(self._auth_states.values())
        else:
            states = [await self._get_auth_state(acct_id)]
        for state in states:
            async with state.guard:
                old_task = self._reset_auth_state(state)
            await self._await_cancelled_task(old_task)

    def _device_auth_in_progress(self, state):
        task = state.poll_task
        return task is not None and not task.done()

    async def _run_device_auth(
        self,
        *,
        acct_id,
        chat_id="",
        reply_to=None,
        platform="",
        chat_type="",
        name,
        begin,
        poll,
        on_success,
        start_message,
        errors=(OSError, RuntimeError, TypeError, ValueError),
        extra=None,
    ):
        state = await self._get_auth_state(acct_id)
        async with state.guard:
            old_task = self._reset_auth_state(state)
        await self._await_cancelled_task(old_task)
        try:
            pending = await begin()
        except errors as e:
            logger.error("%s device OAuth login start failed acct_id=%s error=%s", name, acct_id, e)
            return "%s OAuth login could not start: %s" % (name, e)
        async with state.guard:
            state.pending = pending
            state.reply_to = reply_to
            if extra:
                state.extra.update(extra)
            state.poll_task = asyncio.create_task(
                self._poll_device_auth(
                    acct_id=acct_id,
                    chat_id=chat_id,
                    pending=pending,
                    reply_to=reply_to,
                    platform=platform,
                    chat_type=chat_type,
                    name=name,
                    poll=poll,
                    on_success=on_success,
                    errors=errors,
                )
            )
        return start_message(pending)

    async def _poll_device_auth(
        self,
        *,
        acct_id,
        chat_id,
        pending,
        reply_to,
        platform,
        chat_type,
        name,
        poll,
        on_success,
        errors,
    ):
        try:
            result = await poll(pending)
        except asyncio.CancelledError:
            raise
        except errors as e:
            logger.error("%s device OAuth poll failed acct_id=%s error=%s", name, acct_id, e)
            state = await self._get_auth_state(acct_id)
            async with state.guard:
                if state.pending is pending:
                    self._reset_auth_state(state)
            if chat_id:
                await self.send_outbound(
                    text="%s OAuth failed: %s\nSend /login to try again." % (name, e),
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
            self._reset_auth_state(state)
        await on_success(result)


def format_device_login_start(*, title, verification_uri, user_code, verification_uri_complete="", commands="/login · /logout"):
    lines = [
        title,
        "Open %s on any device and enter code: %s" % (verification_uri, user_code),
    ]
    if verification_uri_complete:
        lines.append("Or open: %s" % verification_uri_complete)
    lines.extend(
        [
            "",
            "Waiting for authorization...",
            "Commands: %s" % commands,
        ]
    )
    return "\n".join(lines)


def local_llm_auth_reply(product, *, logout, base_url, provider, model):
    if logout:
        return "%s local LLM uses an API key. There is no cloud session to sign out of." % product
    return (
        "%s is using a local LLM at %s (provider %s, model %s). No cloud login is required."
        % (product, base_url or "LOCAL_LLM_BASE_URL", provider, model)
    )
