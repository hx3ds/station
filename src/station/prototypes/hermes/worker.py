import asyncio
from dataclasses import dataclass, field

from station import logger
from station.prototypes.boundary import ext_mapping_get, ext_str
from station.prototypes.bridge_worker import ChatBridgeState, PrototypeBridgeWorker

from .usage import USAGE_EXHAUSTED_REPLY, is_usage_exhausted

@dataclass(slots=True)
class HermesChatBridgeState(ChatBridgeState):
    reply_with_voice: bool = False
    pending_request: dict | None = None

@dataclass(slots=True)
class PendingHermesMessage:
    raw_text: str
    combined_text: str
    attachments: list = field(default_factory=list)
    reply_with_voice: bool = False
    preserve_slash_commands: bool = False

class HermesWorker(PrototypeBridgeWorker):
    def _new_chat_state(self):
        return HermesChatBridgeState()

    def _on_bridge_worker_error(self, state):
        state.busy = False
        state.reply_with_voice = False
        state.pending_request = None

    def _bridge_worker_should_respawn(self, state):
        return state.pending_request is None and (not state.busy) and bool(state.pending_messages)

    def _bridge_worker_error_text(self, error):
        return "Hermes bridge error: %s" % error

    async def _enqueue_message(
        self,
        *,
        raw_text,
        combined_text,
        attachments,
        reply_with_voice,
        chat_id,
        acct_id,
        platform="",
        chat_type="",
    ):
        state = await self._get_chat_state(acct_id=acct_id, chat_id=chat_id)
        gateway = await self._ensure_gateway()

        async with state.guard:
            self._touch_chat_meta(state, platform=platform, chat_type=chat_type)
            if state.pending_request is not None:
                await self._resolve_pending_request(
                    gateway=gateway,
                    state=state,
                    incoming_text=combined_text,
                )
                self._ensure_worker(state=state, gateway=gateway, chat_id=chat_id, acct_id=acct_id)
                return
            session_id = state.session_id
            busy = state.busy

        if busy and session_id and combined_text and not attachments:
            try:
                await gateway.dispatch_command(session_id=session_id, name="steer", arg=combined_text)
            except Exception:
                logger.info(
                    "Hermes steer failed; queueing follow-up model_id=%s acct_id=%s chat_id=%s",
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
            item=PendingHermesMessage(
                raw_text=raw_text,
                combined_text=combined_text,
                attachments=attachments,
                reply_with_voice=reply_with_voice,
                preserve_slash_commands=not state.busy,
            ),
            platform=platform,
            chat_type=chat_type,
        )

    async def _bridge_worker_loop(self, *, gateway, state, chat_id, acct_id):
        while True:
            if state.pending_request is not None:
                return

            if state.busy:
                await self._drain_session_events(gateway=gateway, state=state, chat_id=chat_id, acct_id=acct_id)
                continue

            batch = await self._pop_pending_messages(state=state)
            if not batch:
                return

            if not state.session_id:
                state.session_id = await gateway.session_create()

            await self._submit_batch(
                gateway=gateway,
                state=state,
                chat_id=chat_id,
                acct_id=acct_id,
                batch=batch,
            )

    async def _submit_batch(self, *, gateway, state, chat_id, acct_id, batch):
        message = batch[0] if len(batch) == 1 else None
        if message is not None and message.preserve_slash_commands and self._parse_slash_command(message.raw_text):
            await self._handle_curated_slash_command(
                gateway=gateway,
                state=state,
                chat_id=chat_id,
                acct_id=acct_id,
                text=message.raw_text,
                attachments=message.attachments,
            )
            return

        combined_attachments = []
        for item in batch:
            combined_attachments.extend(item.attachments)

        native_attachments = await self._attach_attachments_to_hermes(
            gateway=gateway,
            session_id=state.session_id,
            attachments=combined_attachments,
        )
        prompt = self._build_batch_prompt_text(batch=batch, native_attachments=native_attachments)
        state.busy = True
        state.reply_with_voice = any(item.reply_with_voice for item in batch)
        await gateway.submit_prompt(session_id=state.session_id, text=prompt)
        await self._drain_session_events(gateway=gateway, state=state, chat_id=chat_id, acct_id=acct_id)

    async def _resolve_pending_request(self, *, gateway, state, incoming_text):
        pending = state.pending_request
        req_type = pending["type"]
        request_id = pending["request_id"]
        session_id = state.session_id

        if req_type == "clarify.request":
            await gateway.respond(
                method="clarify.respond",
                session_id=session_id,
                payload={"request_id": request_id, "answer": incoming_text},
            )
        elif req_type == "secret.request":
            await gateway.respond(
                method="secret.respond",
                session_id=session_id,
                payload={"request_id": request_id, "value": incoming_text},
            )
        elif req_type == "sudo.request":
            await gateway.respond(
                method="sudo.respond",
                session_id=session_id,
                payload={"request_id": request_id, "password": incoming_text},
            )
        else:
            raise RuntimeError("Unsupported pending request type: %s" % req_type)

        state.pending_request = None

    async def _drain_session_events(self, *, gateway, state, chat_id, acct_id):
        queue = gateway.session_queue(state.session_id)
        delta_parts = []

        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1800.0)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Timed out waiting for hermes-agent turn completion") from exc

            if event.type == "message.delta":
                text = ext_mapping_get(event.payload, "text", (str,), "")
                if text:
                    delta_parts.append(text)
                continue

            if event.type == "message.complete":
                if await self._notify_gateway_failure(
                    event=event,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    platform=state.platform,
                    chat_type=state.chat_type,
                ):
                    state.busy = False
                    state.reply_with_voice = False
                    return
                text = ext_mapping_get(event.payload, "text", (str,), "").strip() or "".join(delta_parts).strip()
                if text:
                    await self._send_reply(
                        chat_id=chat_id,
                        acct_id=acct_id,
                        text=text,
                        include_voice=state.reply_with_voice,
                        platform=state.platform,
                        chat_type=state.chat_type,
                    )
                state.busy = False
                state.reply_with_voice = False
                return

            if event.type == "approval.request":
                await gateway.respond(
                    method="approval.respond",
                    session_id=state.session_id,
                    payload={"choice": "allow", "all": True},
                )
                continue

            if event.type in {"clarify.request", "secret.request", "sudo.request"}:
                request_id = ext_str("request_id", event.payload["request_id"])
                pending = dict(event.payload)
                pending["type"] = event.type
                pending["request_id"] = request_id
                state.pending_request = pending
                await self.send_outbound(
                    text=self._pending_prompt_text(event),
                    chat_id=chat_id,
                    acct_id=acct_id,
                    platform=state.platform,
                    chat_type=state.chat_type,
                )
                return

            if event.type == "error":
                state.busy = False
                state.reply_with_voice = False
                await self._notify_gateway_failure(
                    event=event,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    platform=state.platform,
                    chat_type=state.chat_type,
                )
                return

    async def _notify_gateway_failure(self, *, event, chat_id, acct_id, platform="", chat_type=""):
        payload = event.payload if event.payload is not None else {}
        status = ext_mapping_get(payload, "status", (str,), "")
        structured = is_usage_exhausted(payload) and (
            payload.get("billing")
            or str(payload.get("failure_reason") or payload.get("reason") or "").strip().lower()
            in {"billing", "usage_exhausted"}
        )
        if event.type != "error" and status != "error" and not structured:
            return False
        message = (
            ext_mapping_get(payload, "message", (str,), "")
            or ext_mapping_get(payload, "error", (str,), "")
            or ext_mapping_get(payload, "text", (str,), "")
        )
        if self._is_missing_provider_error(message):
            await self._send_login_picker(
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return True
        if structured or is_usage_exhausted(message, payload):
            await self.send_outbound(
                text=USAGE_EXHAUSTED_REPLY,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return True
        await self.send_outbound(
            text="Hermes error: %s" % (message or "Hermes gateway error"),
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )
        return True

    def _pending_prompt_text(self, event):
        if event.type == "clarify.request":
            question = ext_mapping_get(event.payload, "question", (str,), "Hermes needs clarification.").strip()
            return "%s\nReply with your answer to continue." % question
        if event.type == "secret.request":
            prompt = ext_mapping_get(event.payload, "prompt", (str,), "Hermes requested a secret value.").strip()
            env_var = ext_mapping_get(event.payload, "env_var", (str,), "").strip()
            suffix = " (%s)" % env_var if env_var else ""
            return "%s%s\nReply with the secret value to continue." % (prompt, suffix)
        return "Hermes requested a sudo password.\nReply with the password to continue."
