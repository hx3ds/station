from dataclasses import dataclass
import json

from station import logger
from station.prototypes.boundary import ext_dict, ext_int, ext_str
from station.prototypes.bridge_worker import PrototypeBridgeWorker

@dataclass(slots=True)
class PendingTurnMessage:
    parts: list

class OpenCodeWorker(PrototypeBridgeWorker):
    def _bridge_worker_error_text(self, error):
        return "OpenCode bridge error: %s" % error

    async def _enqueue_parts(self, *, parts, chat_id, acct_id, platform="", chat_type=""):
        state = await self._get_chat_state(acct_id=acct_id, chat_id=chat_id)
        gateway = await self._ensure_gateway()
        await self._enqueue_pending(
            state=state,
            gateway=gateway,
            chat_id=chat_id,
            acct_id=acct_id,
            item=PendingTurnMessage(parts=parts),
            platform=platform,
            chat_type=chat_type,
        )

    async def _bridge_worker_loop(self, *, gateway, state, chat_id, acct_id):
        while True:
            parts = await self._pop_batched_turn(state=state)
            if parts is None:
                return

            if not state.session_id:
                state.session_id = await gateway.session_create(title="station:%s:%s" % (acct_id, chat_id))
                gateway.bind_session(session_id=state.session_id, acct_id=acct_id, chat_id=chat_id)

            state.busy = True
            try:
                await gateway.prompt_async(session_id=state.session_id, parts=parts)
                reply = await self._drain_session_events(gateway=gateway, state=state)
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
        def merge_many(messages):
            merged = []
            for index, message in enumerate(messages, start=1):
                for item in message.parts:
                    copied = dict(item)
                    if copied.get("type") == "text":
                        copied["text"] = self._queued_followup_label(index, copied["text"]).strip()
                    merged.append(copied)
            return merged

        return await self._pop_batched_items(
            state=state,
            merge_one=lambda message: list(message.parts),
            merge_many=merge_many,
        )

    async def _drain_session_events(self, *, gateway, state):
        if not state.session_id:
            return ""

        queue = gateway.session_queue(state.session_id)
        text_parts = {}
        text_order = []
        extra_lines = []
        user_message_ids = set()
        part_message_ids = {}

        while True:
            event = await queue.get()
            reply = self._consume_event(
                event=event,
                text_parts=text_parts,
                text_order=text_order,
                extra_lines=extra_lines,
                user_message_ids=user_message_ids,
                part_message_ids=part_message_ids,
            )
            if reply is not None:
                return reply

    def _consume_event(self, *, event, text_parts, text_order, extra_lines, user_message_ids, part_message_ids):

        event_type = event.type
        payload = event.payload

        if event_type == "message.updated":
            info = payload.get("info")
            if info is None:
                return None
            info = ext_dict("message.updated info", info)
            role = ext_str("message.updated role", info.get("role"))
            if role.strip() == "user":
                message_id = ext_str("message.updated id", info.get("id"))
                if message_id:
                    user_message_ids.add(message_id)
            return None

        if event_type == "message.part.delta":
            message_id = ext_str("message.part.delta messageID", payload.get("messageID"))
            if message_id and message_id in user_message_ids:
                return None
            part_id = ext_str("message.part.delta partID", payload.get("partID"))
            field_name = ext_str("message.part.delta field", payload.get("field"))
            delta = ext_str("message.part.delta delta", payload.get("delta"), strip=False)
            if part_id and field_name in {"text", "content", ""} and delta:
                if message_id:
                    part_message_ids[part_id] = message_id
                if part_id not in text_parts:
                    text_parts[part_id] = ""
                    text_order.append(part_id)
                text_parts[part_id] += delta
            return None

        if event_type == "message.part.updated":
            part = payload.get("part")
            if part is None:
                return None
            part = ext_dict("message.part.updated part", part)
            part_type = ext_str("message.part.updated type", part.get("type"))
            if part_type != "text":
                return None
            if part.get("synthetic") is True or part.get("ignored") is True:
                return None
            message_id = ext_str("message.part.updated messageID", part.get("messageID"))
            if message_id and message_id in user_message_ids:
                return None
            part_id = ext_str("message.part.updated id", part.get("id"))
            if part_id:
                if message_id:
                    part_message_ids[part_id] = message_id
                if part_id not in text_parts:
                    text_order.append(part_id)
                text_parts[part_id] = ext_str("message.part.updated text", part.get("text"), strip=False)
            return None

        if event_type == "session.error":
            error = payload.get("error")
            try:
                logger.error("OpenCode session.error payload=%s", json.dumps(payload, ensure_ascii=False)[:4000])
            except (TypeError, ValueError):
                logger.error("OpenCode session.error payload=%r", payload)
            extra_lines.append("OpenCode error: %s" % self._format_opencode_error(error))
            return self._join_reply(text_parts, text_order, extra_lines, user_message_ids, part_message_ids)

        if event_type == "session.status":
            status = payload.get("status")
            if status is None:
                return None
            status = ext_dict("session.status status", status)
            status_type = ext_str("session.status type", status.get("type"))
            if status_type == "retry":
                message = ext_str("session.status message", status.get("message"))
                if message:
                    extra_lines.append("OpenCode retry: %s" % message)
            return None

        if event_type == "session.idle":
            return (
                self._join_reply(text_parts, text_order, extra_lines, user_message_ids, part_message_ids)
                or "OpenCode turn finished."
            )

        return None

    def _format_opencode_error(self, error):

        if error is None:
            return "OpenCode error"
        if type(error) is dict:
            name = ext_str("opencode error name", error.get("name"))
            data = error.get("data")
            extras = []
            if data is not None:
                data = ext_dict("opencode error data", data)
                message = ext_str("opencode error data.message", data.get("message"))
                pieces = [piece for piece in (name, message) if piece]
                provider = ext_str("opencode error data.providerID", data.get("providerID"))
                if provider:
                    pieces.append("provider=%s" % provider)
                status = data.get("statusCode")
                if status is not None:
                    status = ext_int("opencode error data.statusCode", status)
                    pieces.append("status=%s" % status)
                for key in ("cause", "error", "code", "statusText", "url", "responseBody"):
                    value = data.get(key)
                    if value not in (None, ""):
                        extras.append("%s=%s" % (key, value))
                if pieces or extras:
                    return " | ".join(pieces + extras)
            message = error.get("message")
            if message is not None:
                message = ext_str("opencode error message", message)
            dumped = ""
            try:
                dumped = json.dumps(error, ensure_ascii=False)[:800]
            except (TypeError, ValueError):
                dumped = ""
            text = ((message or name) or "OpenCode error").strip() or "OpenCode error"
            if dumped:
                return "%s | %s" % (text, dumped)
            return text
        return ext_str("opencode error", error) or "OpenCode error"

    def _join_reply(self, text_parts, text_order, extra_lines, user_message_ids, part_message_ids):
        parts = []
        for part_id in text_order:
            message_id = part_message_ids.get(part_id, "")
            if message_id and message_id in user_message_ids:
                continue
            text = text_parts.get(part_id, "").strip()
            if text:
                parts.append(text)
        for line in extra_lines:
            cleaned = line.strip()
            if cleaned:
                parts.append(cleaned)
        return "\n\n".join(parts).strip()
