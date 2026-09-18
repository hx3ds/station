from dataclasses import dataclass, field

from station import logger
from station.prototypes.boundary import ext_dict, ext_list, ext_str
from station.prototypes.bridge_worker import PrototypeBridgeWorker

@dataclass(slots=True)
class PendingTurnMessage:
    message: str
    images: list = field(default_factory=list)

class PiWorker(PrototypeBridgeWorker):
    def _bridge_worker_error_text(self, error):
        return "Pi bridge error: %s" % error

    async def _enqueue_turn(self, *, message, images, chat_id, acct_id, platform="", chat_type=""):
        state = await self._get_chat_state(acct_id=acct_id, chat_id=chat_id)
        gateway = await self._ensure_gateway()

        async with state.guard:
            self._touch_chat_meta(state, platform=platform, chat_type=chat_type)
            busy = state.busy

        if busy:
            rpc = gateway.get_chat(acct_id=acct_id, chat_id=chat_id)
            if rpc is not None:
                try:
                    if images:
                        await rpc.prompt(message, images=images, streaming_behavior="steer")
                    else:
                        await rpc.steer(message)
                except Exception:
                    logger.info(
                        "Pi steer failed; queueing follow-up model_id=%s acct_id=%s chat_id=%s",
                        self.model_id,
                        acct_id,
                        chat_id,
                        exc_info=True,
                    )
                else:
                    return

        await self._enqueue_pending(
            state=state,
            gateway=gateway,
            chat_id=chat_id,
            acct_id=acct_id,
            item=PendingTurnMessage(message=message, images=list(images or [])),
            platform=platform,
            chat_type=chat_type,
        )

    async def _bridge_worker_loop(self, *, gateway, state, chat_id, acct_id):
        while True:
            payload = await self._pop_batched_turn(state=state)
            if payload is None:
                return

            message, images = payload
            rpc = await gateway.ensure_chat(acct_id=acct_id, chat_id=chat_id)
            state.busy = True
            try:
                await rpc.prompt(message, images=images or None)
                reply = await self._drain_events(rpc=rpc)
            finally:
                state.busy = False

            if reply:
                await self.send_outbound(
                    text=reply,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    platform=state.platform,
                    chat_type=state.chat_type,
                )

    async def _pop_batched_turn(self, *, state):
        messages = await self._pop_pending_messages(state=state)
        if not messages:
            return None
        if len(messages) == 1:
            return messages[0].message, list(messages[0].images)

        text_parts = []
        images = []
        for index, item in enumerate(messages, start=1):
            text_parts.append(self._queued_followup_label(index, item.message))
            images.extend(item.images)
        return "\n\n".join(text_parts), images

    async def _drain_events(self, *, rpc):

        queue = rpc.event_queue()
        text_parts = []
        current = []
        extra_lines = []

        while True:
            event = ext_dict("pi event", await queue.get())
            event_type = ext_str("pi event.type", event.get("type"))

            if event_type == "message_update":
                delta = event.get("assistantMessageEvent")
                if delta is None:
                    continue
                delta = ext_dict("pi assistantMessageEvent", delta)
                if delta.get("type") == "text_delta":
                    chunk = delta.get("delta")
                    if chunk is not None:
                        chunk = ext_str("pi text_delta", chunk, strip=False)
                        if chunk:
                            current.append(chunk)
                continue

            if event_type == "message_end":
                message = event.get("message")
                if message is not None:
                    message = ext_dict("pi message", message)
                    if message.get("role") == "assistant":
                        text = self._message_text(message)
                        if text:
                            text_parts.append(text)
                        elif current:
                            text_parts.append("".join(current))
                current = []
                continue

            if event_type == "extension_error":
                err = event.get("error")
                if err is None:
                    err = event.get("message")
                if err is None:
                    err = "extension error"
                else:
                    err = ext_str("pi extension error", err)
                extra_lines.append("Pi extension error: %s" % err)
                continue

            if event_type == "agent_end":
                continue

            if event_type == "agent_settled":
                reply = self._join_reply(text_parts, extra_lines)
                if reply:
                    return reply
                try:
                    fallback = await rpc.get_last_assistant_text()
                except Exception:
                    fallback = ""
                return fallback.strip() or "Pi turn finished."

    def _message_text(self, message):
        content = message.get("content")
        if content is None:
            return ""
        if type(content) is str:
            return content.strip()
        content = ext_list("pi message.content", content)
        pieces = []
        for part in content:
            part = ext_dict("pi content part", part)
            if part.get("type") == "text":
                text = part.get("text")
                if text is not None:
                    text = ext_str("pi content text", text)
                    if text:
                        pieces.append(text)
        return "\n".join(pieces).strip()

    def _join_reply(self, text_parts, extra_lines):
        parts = [part for part in text_parts if part and part.strip()]
        parts.extend(line for line in extra_lines if line and line.strip())
        return "\n\n".join(parts).strip()
