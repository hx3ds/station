import asyncio
import contextlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from station import logger
from station.prototypes.boundary import ext_bool, ext_mapping_get, ext_str
from station.prototypes.bridge_worker import ChatBridgeState, PrototypeBridgeWorker
from station.prototypes.voice import PrototypeVoice
from station.prototypes.voice_policy import (
    voice_delivery_mime,
    voice_delivery_suffix,
)


USAGE_EXHAUSTED_REPLY = (
    "Hermes usage is not enough. Credits or subscription for this model are exhausted.\n"
    "Add credits at https://grok.com/?_s=usage or upgrade at https://grok.com/supergrok."
)


def is_usage_exhausted(*parts):
    for part in parts:
        if isinstance(part, dict):
            reason = str(part.get("failure_reason") or part.get("reason") or "").strip().lower()
            if reason in {"billing", "usage_exhausted"} or part.get("billing"):
                return True
            part = str(part.get("message") or part.get("error") or part.get("text") or "")
        text = str(part or "").lower()
        if "credits" in text or "usage not enough" in text or "payment_required" in text or "spending-limit" in text:
            return True
    return False


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


class HermesWorker(PrototypeVoice, PrototypeBridgeWorker):
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
        message_id=None,
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
            busy = state.busy

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
                preserve_slash_commands=not busy,
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
        if message is not None and message.preserve_slash_commands:
            cmd, _args = self._slash_command(message.raw_text)
            if cmd and cmd not in {"/login", "/logout", "/start"}:
                await self._handle_slash_command(
                    gateway=gateway,
                    state=state,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    text=message.raw_text,
                )
                return

        combined_attachments = []
        for item in batch:
            combined_attachments.extend(item.attachments)

        await self._attach_attachments_to_hermes(
            gateway=gateway,
            session_id=state.session_id,
            attachments=combined_attachments,
        )
        prompt = self._build_batch_prompt_text(batch)
        state.busy = True
        state.reply_with_voice = any(item.reply_with_voice for item in batch)
        await gateway.submit_prompt(session_id=state.session_id, text=prompt)
        await self._drain_session_events(gateway=gateway, state=state, chat_id=chat_id, acct_id=acct_id)

    def _build_batch_prompt_text(self, batch):
        return self._merge_followup_texts(
            batch,
            text_of=lambda message: message.combined_text,
            empty="The user sent attachments without additional text.",
        )

    async def _attach_attachments_to_hermes(self, *, gateway, session_id, attachments):
        for attachment in attachments:
            local_path = self._attachment_str(attachment, "local_path")
            display_name = self._attachment_display_name(attachment, local_path=local_path)
            if not local_path or not Path(local_path).exists():
                continue
            try:
                if self._is_pdf_attachment(attachment, local_path=local_path):
                    await gateway.request("pdf.attach", {"session_id": session_id, "path": local_path})
                    continue
                if self._is_image_attachment(attachment, local_path=local_path):
                    if self._launch_settings().uses_local_llm():
                        continue
                    await gateway.request("image.attach", {"session_id": session_id, "path": local_path})
                    continue
                await gateway.request(
                    "file.attach",
                    {"session_id": session_id, "path": local_path, "name": display_name},
                )
            except (OSError, RuntimeError, TypeError, ValueError):
                logger.exception(
                    "Hermes native attachment failed model_id=%s attachment=%s",
                    self.model_id,
                    display_name,
                )

    async def _handle_slash_command(self, *, gateway, state, chat_id, acct_id, text):
        result = await gateway.request(
            "slash.exec",
            {"session_id": state.session_id, "command": text.strip()},
        )
        if result is None:
            result = {}
        result = result if isinstance(result, dict) else {}
        await self._apply_slash_result(
            gateway=gateway,
            state=state,
            chat_id=chat_id,
            acct_id=acct_id,
            result=result,
        )

    async def _apply_slash_result(self, *, gateway, state, chat_id, acct_id, result):
        result_type = ext_mapping_get(result, "type", (str,), "").strip().lower()
        notice = ext_mapping_get(result, "notice", (str,), "").strip()
        warning = ext_mapping_get(result, "warning", (str,), "").strip()
        output = ext_mapping_get(result, "output", (str,), "").strip()
        if warning:
            await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=warning)
        if result_type in {"send", "skill"}:
            if notice:
                await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text=notice)
            message = ext_mapping_get(result, "message", (str,), "").strip()
            if not message:
                return
            state.busy = True
            state.reply_with_voice = False
            await gateway.submit_prompt(session_id=state.session_id, text=message)
            await self._drain_session_events(gateway=gateway, state=state, chat_id=chat_id, acct_id=acct_id)
            return
        text_parts = [part for part in (notice, output) if part]
        if text_parts:
            await self._outbound(state=state, chat_id=chat_id, acct_id=acct_id, text="\n\n".join(text_parts))

    async def _outbound(self, *, state, chat_id, acct_id, text):
        await self.send_outbound(
            text=text,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=state.platform,
            chat_type=state.chat_type,
        )

    async def _resolve_pending_request(self, *, gateway, state, incoming_text):
        pending = state.pending_request
        req_type = pending["type"]
        request_id = pending["request_id"]
        session_id = state.session_id
        field = {"clarify.request": "answer", "secret.request": "value", "sudo.request": "password"}.get(req_type)
        if field is None:
            raise RuntimeError("Unsupported pending request type: %s" % req_type)
        method = req_type.replace(".request", ".respond")
        await gateway.respond(
            method=method,
            session_id=session_id,
            payload={"request_id": request_id, field: incoming_text},
        )
        state.pending_request = None

    def _session_reply_dest(self, *, state, chat_id, acct_id):
        return {
            "chat_id": chat_id,
            "acct_id": acct_id,
            "platform": state.platform,
            "chat_type": state.chat_type,
        }

    async def _on_session_message_complete(self, *, gateway, state, event, delta_parts, chat_id, acct_id, dest):
        if await self._notify_gateway_failure(
            event=event,
            chat_id=dest["chat_id"],
            acct_id=dest["acct_id"],
            platform=dest["platform"],
            chat_type=dest["chat_type"],
        ):
            state.busy = False
            state.reply_with_voice = False
            return True
        text = ext_mapping_get(event.payload, "text", (str,), "").strip() or "".join(delta_parts).strip()
        if text:
            await self._send_reply(
                chat_id=dest["chat_id"],
                acct_id=dest["acct_id"],
                text=text,
                include_voice=state.reply_with_voice,
                platform=dest["platform"],
                chat_type=dest["chat_type"],
            )
        state.busy = False
        state.reply_with_voice = False
        return True

    async def _on_session_error(self, *, gateway, state, event, chat_id, acct_id, dest):
        state.busy = False
        state.reply_with_voice = False
        await self._notify_gateway_failure(
            event=event,
            chat_id=dest["chat_id"],
            acct_id=dest["acct_id"],
            platform=dest["platform"],
            chat_type=dest["chat_type"],
        )
        return True

    async def _drain_session_events(self, *, gateway, state, chat_id, acct_id):
        queue = gateway.session_queue(state.session_id)
        delta_parts = []
        dest = self._session_reply_dest(state=state, chat_id=chat_id, acct_id=acct_id)

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
                if await self._on_session_message_complete(
                    gateway=gateway,
                    state=state,
                    event=event,
                    delta_parts=delta_parts,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    dest=dest,
                ):
                    return
                delta_parts = []
                continue

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
                    chat_id=dest["chat_id"],
                    acct_id=dest["acct_id"],
                    platform=dest["platform"],
                    chat_type=dest["chat_type"],
                )
                return

            if event.type == "error":
                if await self._on_session_error(
                    gateway=gateway,
                    state=state,
                    event=event,
                    chat_id=chat_id,
                    acct_id=acct_id,
                    dest=dest,
                ):
                    return
                delta_parts = []
                continue

    async def _notify_gateway_failure(self, *, event, chat_id, acct_id, platform="", chat_type=""):
        payload = event.payload if event.payload is not None else {}
        status = ext_mapping_get(payload, "status", (str,), "")
        structured = is_usage_exhausted(payload)
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
        if structured or is_usage_exhausted(message):
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

    async def _prepare_voice_input(self, *, attachments):
        async def transcribe(_attachment, local_path, display_name):
            gateway = await self._ensure_gateway()
            result = await gateway.request("audio.transcribe", {"file_path": local_path}, timeout_s=300.0)
            result = result if isinstance(result, dict) else {}
            transcript = ext_mapping_get(result, "transcript", (str,), "").strip()
            if result.get("success") and transcript:
                return transcript
            logger.warning(
                "Hermes voice transcription failed model_id=%s attachment=%s error=%s",
                self.model_id,
                display_name,
                ext_mapping_get(result, "error", (str,), "no transcript returned").strip(),
            )
            return ""

        return await PrototypeVoice._prepare_voice_input(self, attachments, transcribe=transcribe)

    def _should_send_voice_reply(self, *, incoming_had_audio, reply_mode="voice_only"):
        settings = self._settings
        if settings is not None:
            reply_mode = settings.voice_reply_mode
        return super()._should_send_voice_reply(incoming_had_audio=incoming_had_audio, reply_mode=reply_mode)

    async def _send_reply(self, *, chat_id, acct_id, text, include_voice, platform="", chat_type=""):
        await self._send_text_and_maybe_voice(
            chat_id=chat_id,
            acct_id=acct_id,
            text=text,
            include_voice=include_voice,
            synthesize=lambda reply: self._send_voice(
                chat_id=chat_id,
                acct_id=acct_id,
                text=reply,
                platform=platform,
                chat_type=chat_type,
            ),
            platform=platform,
            chat_type=chat_type,
        )

    async def _send_voice(self, *, chat_id, acct_id, text, platform="", chat_type=""):
        settings = self._launch_settings()
        delivery = settings.voice_delivery_method
        output_path = Path(self.storage_dir) / "voice_replies" / ("%s%s" % (uuid.uuid4().hex, voice_delivery_suffix(delivery)))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        gateway = await self._ensure_gateway()
        result = await gateway.request("audio.synthesize", {"text": text, "output_path": str(output_path)}, timeout_s=300.0)
        result = result if isinstance(result, dict) else {}
        if not result.get("success"):
            raise RuntimeError(ext_mapping_get(result, "error", (str,), "unknown TTS error"))
        file_path_value = ext_mapping_get(result, "file_path", (str,), None, allow_none=True)
        file_path = Path(file_path_value if file_path_value else str(output_path)).expanduser()
        if not file_path.exists():
            raise FileNotFoundError("Voice reply file not found: %s" % file_path)
        try:
            audio_bytes = file_path.read_bytes()
        finally:
            with contextlib.suppress(OSError):
                file_path.unlink()
        voice_compatible = result.get("voice_compatible")
        if voice_compatible is not None:
            voice_compatible = ext_bool("voice_compatible", voice_compatible)
        use_voice = delivery == "voice" and bool(voice_compatible)
        att_type = "voice" if use_voice else "audio"
        meta = self.save_temp(
            data=audio_bytes,
            original_name=file_path.name,
            mime_type=voice_delivery_mime(
                "voice" if use_voice or file_path.suffix.lower() == ".ogg" else "audio"
            ),
            ext=file_path.suffix.lstrip(".") or ("ogg" if use_voice else "mp3"),
        )
        await self.send_outbound(
            attachments=[{"type": att_type, "file_id": meta["file_id"]}],
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )
