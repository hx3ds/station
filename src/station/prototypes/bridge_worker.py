import asyncio
import contextlib
from dataclasses import dataclass, field

from station import logger

@dataclass(slots=True)
class ChatBridgeState:
    session_id: str = ""
    busy: bool = False
    pending_messages: list = field(default_factory=list)
    guard: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker_task: object = None
    platform: str = ""
    chat_type: str = ""

class PrototypeBridgeWorker:
    def _init_bridge_worker(self, *, worker_name):
        self._chat_states = {}
        self._chat_states_guard = asyncio.Lock()
        self._bridge_worker_name = worker_name

    def _new_chat_state(self):
        return ChatBridgeState()

    async def _get_chat_state(self, *, acct_id, chat_id):
        key = "%s:%s" % (acct_id, chat_id)
        async with self._chat_states_guard:
            state = self._chat_states.get(key)
            if state is None:
                state = self._new_chat_state()
                self._chat_states[key] = state
            return state

    def _touch_chat_meta(self, state, *, platform="", chat_type=""):
        if platform:
            state.platform = platform
        if chat_type:
            state.chat_type = chat_type

    def _ensure_worker(self, *, state, gateway, chat_id, acct_id):
        task = state.worker_task
        if task is not None and not task.done():
            return
        state.worker_task = asyncio.create_task(
            self._run_worker(gateway=gateway, state=state, chat_id=chat_id, acct_id=acct_id),
            name="%s:%s:%s" % (self._bridge_worker_name, acct_id, chat_id),
        )

    async def _cancel_chat_workers(self):
        async with self._chat_states_guard:
            states = list(self._chat_states.values())
            self._chat_states.clear()
        for state in states:
            task = state.worker_task
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _pop_pending_messages(self, *, state):
        async with state.guard:
            if not state.pending_messages:
                return []
            messages = list(state.pending_messages)
            state.pending_messages.clear()
            return messages

    async def _run_worker(self, *, gateway, state, chat_id, acct_id):
        current_task = asyncio.current_task()
        try:
            await self._bridge_worker_loop(gateway=gateway, state=state, chat_id=chat_id, acct_id=acct_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._on_bridge_worker_error(state)
            logger.error(
                "unexpected where=%s model_id=%s acct_id=%s chat_id=%s error=%s",
                self._bridge_worker_name,
                self.model_id,
                acct_id,
                chat_id,
                e,
                exc_info=e,
            )
            await self.send_outbound(
                text=self._bridge_worker_error_text(e),
                chat_id=chat_id,
                acct_id=acct_id,
                platform=state.platform,
                chat_type=state.chat_type,
            )
        finally:
            async with state.guard:
                if state.worker_task is current_task:
                    state.worker_task = None
                if self._bridge_worker_should_respawn(state):
                    self._ensure_worker(state=state, gateway=gateway, chat_id=chat_id, acct_id=acct_id)

    def _on_bridge_worker_error(self, state):
        state.busy = False

    def _bridge_worker_should_respawn(self, state):
        return (not state.busy) and bool(state.pending_messages)

    def _bridge_worker_error_text(self, error):
        raise NotImplementedError

    async def _bridge_worker_loop(self, *, gateway, state, chat_id, acct_id):
        raise NotImplementedError

    async def _enqueue_pending(self, *, state, gateway, chat_id, acct_id, item, platform="", chat_type=""):
        async with state.guard:
            self._touch_chat_meta(state, platform=platform, chat_type=chat_type)
            state.pending_messages.append(item)
            self._ensure_worker(state=state, gateway=gateway, chat_id=chat_id, acct_id=acct_id)

    @staticmethod
    def _queued_followup_label(index, body, *, empty="(attachment-only follow-up)"):
        text = (body or "").strip() or empty
        return "[Queued follow-up %d]\n%s" % (index, text)

    def _merge_followup_texts(self, items, *, text_of, empty="(attachment-only follow-up)"):
        if len(items) == 1:
            return text_of(items[0])
        return "\n\n".join(
            self._queued_followup_label(index, text_of(item), empty=empty)
            for index, item in enumerate(items, start=1)
        )

    async def _pop_batched_items(self, *, state, merge_one, merge_many):
        messages = await self._pop_pending_messages(state=state)
        if not messages:
            return None
        if len(messages) == 1:
            return merge_one(messages[0])
        return merge_many(messages)
