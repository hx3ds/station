import base64
import json
import mimetypes
import os

from station import logger
from station.errors import ExternalError
from station.prototypes.chat_history import (
    apply_compact_summary,
    format_transcript,
    history_sender,
    load_messages,
    needs_compact,
    remember_chat_route,
    save_messages,
    split_for_compact,
)
from station.prototypes.grok.tools import CHAT_TOOLS, record_outbound, tool_result
from station.prototypes.voice import PrototypeVoice

from . import auth_store
from .client import complete_chat, fetch_usage, generate_image, generate_video, synthesize_speech, transcribe_audio

BUSY_REPLY_TEXT = "I'm still processing your previous message. Please wait."

class GrokTurn(PrototypeVoice):
    async def _send_photo(self, *, chat_id, acct_id, image_bytes, mime="image/png", caption="", platform="", chat_type=""):
        if not image_bytes:
            return
        content_type = (mime or "image/png").split(";")[0].strip().lower() or "image/png"
        suffix = ".jpg" if content_type in {"image/jpeg", "image/jpg"} else ".webp" if content_type == "image/webp" else ".png"
        meta = self.save_temp(
            data=image_bytes,
            original_name="grok_img%s" % suffix,
            mime_type=content_type,
        )
        await self.send_outbound(
            text=caption or "",
            attachments=[{"type": "photo", "file_id": meta["file_id"]}],
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type)

    async def _send_video(self, *, chat_id, acct_id, video_bytes, mime="video/mp4", caption="", platform="", chat_type=""):
        if not video_bytes:
            return
        content_type = (mime or "video/mp4").split(";")[0].strip().lower() or "video/mp4"
        suffix = ".webm" if content_type == "video/webm" else ".mov" if content_type == "video/quicktime" else ".mp4"
        meta = self.save_temp(
            data=video_bytes,
            original_name="grok_vid%s" % suffix,
            mime_type=content_type,
        )
        await self.send_outbound(
            text=caption or "",
            attachments=[{"type": "video", "file_id": meta["file_id"]}],
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type)

    async def _photo_attachment_to_data_url(self, *, attachment):
        local_path = self._attachment_str(attachment, "local_path")
        if not local_path:
            local_path = await self._ensure_attachment_ready(attachment)
        if not local_path:
            return ""
        with open(local_path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("ascii")
        content_type = (attachment.get("content_type") or "").strip().lower()
        if content_type.startswith("image/"):
            mime_type = content_type
        else:
            guessed, _ = mimetypes.guess_type(local_path)
            mime_type = guessed if guessed and guessed.startswith("image/") else "image/jpeg"
        return "data:%s;base64,%s" % (mime_type, image_b64)

    async def _transcribe_audio_attachment(self, *, attachment, settings, access_token):
        local_path = self._attachment_str(attachment, "local_path")
        if not local_path:
            local_path = await self._ensure_attachment_ready(attachment)
        if not local_path:
            return None
        with open(local_path, "rb") as f:
            audio_data = f.read()
        filename = (
            (attachment.get("file_name") or "").strip()
            or (attachment.get("filename") or "").strip()
            or os.path.basename(local_path)
            or "audio.wav"
        )
        return await transcribe_audio(
            session=None,
            settings=settings,
            access_token=access_token,
            audio_bytes=audio_data,
            filename=filename,
        )

    async def _send_voice_reply(self, *, chat_id, acct_id, text, settings, access_token, platform="", chat_type=""):
        audio, content_type = await synthesize_speech(
            session=None,
            settings=settings,
            access_token=access_token,
            text=text,
        )
        if not audio:
            logger.warning("Grok TTS produced no audio chat_id=%s", chat_id)
            return
        await self._send_voice_bytes(
            chat_id=chat_id,
            acct_id=acct_id,
            audio_bytes=audio,
            content_type=content_type,
            name="grok_tts",
            platform=platform,
            chat_type=chat_type,
        )

    async def _handle_usage_command(self, *, chat_id, acct_id, settings, reply_to=None, platform="", chat_type=""):
        creds = await self._load_oauth_credentials(acct_id=acct_id, settings=settings)
        if creds is None:
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
                chat_type=chat_type)
            return
        auth_store.save_credentials(self.storage_dir, acct_id, creds)
        reply = await fetch_usage(session=None, access_token=creds.access)
        await self.send_outbound(
            text=reply,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type)

    async def _handle_generate_command(self, *, prompt, chat_id, acct_id, settings, reply_to=None, platform="", chat_type=""):
        if not prompt:
            await self.send_outbound(
                text="Usage: /image <prompt>",
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type)
            return

        busy_key = (acct_id, chat_id)
        if busy_key in self._active_chat_requests:
            await self.send_outbound(
                text=BUSY_REPLY_TEXT,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type)
            return

        self._active_chat_requests.add(busy_key)
        try:
            access_token = await self._resolve_access_token(acct_id=acct_id, settings=settings)
            if not access_token:
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
                    chat_type=chat_type)
                return

            image_bytes, mime, err = await generate_image(
                session=None,
                settings=settings,
                access_token=access_token,
                prompt=prompt,
            )
            if err or not image_bytes:
                await self.send_outbound(
                    text=err or "Grok image generation failed.",
                    chat_id=chat_id,
                    acct_id=acct_id,
                    platform=platform,
                    chat_type=chat_type)
                return

            await self._send_photo(
                chat_id=chat_id,
                acct_id=acct_id,
                image_bytes=image_bytes,
                mime=mime,
                caption=prompt,
                platform=platform,
                chat_type=chat_type,
            )
        finally:
            self._active_chat_requests.discard(busy_key)

    async def _handle_generate_video_command(self, *, prompt, chat_id, acct_id, settings, reply_to=None, platform="", chat_type=""):
        if not prompt:
            await self.send_outbound(
                text="Usage: /video <prompt>",
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type)
            return

        busy_key = (acct_id, chat_id)
        if busy_key in self._active_chat_requests:
            await self.send_outbound(
                text=BUSY_REPLY_TEXT,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type)
            return

        self._active_chat_requests.add(busy_key)
        try:
            access_token = await self._resolve_access_token(acct_id=acct_id, settings=settings)
            if not access_token:
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
                    chat_type=chat_type)
                return

            video_bytes, mime, err = await generate_video(
                session=None,
                settings=settings,
                access_token=access_token,
                prompt=prompt,
            )
            if err or not video_bytes:
                await self.send_outbound(
                    text=err or "Grok video generation failed.",
                    chat_id=chat_id,
                    acct_id=acct_id,
                    platform=platform,
                    chat_type=chat_type)
                return

            await self._send_video(
                chat_id=chat_id,
                acct_id=acct_id,
                video_bytes=video_bytes,
                mime=mime,
                caption=prompt,
                platform=platform,
                chat_type=chat_type,
            )
        finally:
            self._active_chat_requests.discard(busy_key)

    async def _run_chat_turn(self, *, chat_id, acct_id, settings, text, photo_attachment, voice_attachment, reply_to=None, platform="", chat_type="", user_id="", sender=None):
        busy_key = (acct_id, chat_id)
        if busy_key in self._active_chat_requests:
            await self.send_outbound(
                text=BUSY_REPLY_TEXT,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type)
            return

        self._active_chat_requests.add(busy_key)
        try:
            active_state = await self._active_realtime_for_chat(chat_id)
            if active_state is not None and text and not photo_attachment and not voice_attachment:
                if text.strip() == "__FEED_VOICE__":
                    await self._feed_voice_sample(active_state)
                    return
                await active_state.session.send_text(text)
                return

            access_token = await self._resolve_access_token(acct_id=acct_id, settings=settings)
            if not access_token:
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
                    chat_type=chat_type)
                return

            photo_data_url = ""
            transcript = ""
            reply_text = ""

            if photo_attachment:
                photo_data_url = await self._photo_attachment_to_data_url(attachment=photo_attachment)
                if not photo_data_url:
                    reply_text = "Image not supported."

            if not reply_text and voice_attachment:
                transcript_or_none = await self._transcribe_audio_attachment(
                    attachment=voice_attachment,
                    settings=settings,
                    access_token=access_token,
                )
                if transcript_or_none is None:
                    reply_text = "Voice input not supported."
                else:
                    transcript = transcript_or_none
                    logger.info("Grok STT transcript_len=%d", len(transcript))

            if not reply_text:
                prompt_parts = []
                if text:
                    prompt_parts.append(text)
                if transcript:
                    if prompt_parts:
                        prompt_parts.append("")
                    prompt_parts.append(
                        "The following transcript is authoritative evidence of what was spoken in the audio attachment."
                    )
                    prompt_parts.append(transcript)
                elif voice_attachment:
                    prompt_parts.append("[NO SPEECH]")
                if photo_data_url and not prompt_parts:
                    prompt_parts.append("Please analyze the attached image and respond to the user.")
                prompt_text = "\n".join(prompt_parts).strip()
                if not prompt_text:
                    return
                model_text = prompt_text
                if acct_id or chat_id:
                    model_text = "Current chat acct_id=%s chat_id=%s.\n%s" % (acct_id, chat_id, prompt_text)
                content = model_text
                if photo_data_url:
                    content = [
                        {"type": "text", "text": model_text},
                        {"type": "image_url", "image_url": {"url": photo_data_url}},
                    ]
                turn_sender = history_sender(user_id, sender)
                reply_text = await complete_chat(
                    session=None,
                    settings=settings,
                    access_token=access_token,
                    user_content=content,
                    history=await self._load_compacted_history(
                        acct_id=acct_id,
                        chat_id=chat_id,
                        settings=settings,
                        access_token=access_token,
                    ),
                    sender=turn_sender,
                    tools=CHAT_TOOLS,
                    run_tool=self._run_grok_tool,
                )
                if self._grok_reply_ok(reply_text):
                    self._append_chat_turn(
                        acct_id=acct_id,
                        chat_id=chat_id,
                        user_text=prompt_text,
                        reply_text=reply_text,
                        sender=turn_sender,
                        platform=platform,
                        chat_type=chat_type,
                    )

            await self.send_outbound(
                text=reply_text,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type)

            send_voice = self._should_send_voice_reply(
                incoming_had_audio=voice_attachment is not None,
                reply_mode=settings.voice_reply_mode,
            )
            if send_voice:
                if self._grok_reply_ok(reply_text):
                    await self._send_voice_reply(
                        chat_id=chat_id,
                        acct_id=acct_id,
                        text=reply_text,
                        settings=settings,
                        access_token=access_token,
                        platform=platform,
                        chat_type=chat_type,
                    )
        finally:
            self._active_chat_requests.discard(busy_key)

    def _grok_reply_ok(self, reply_text):
        text = (reply_text or "").strip()
        if not text:
            return False
        if text.startswith("Grok request failed:") or text.startswith("Grok not configured"):
            return False
        if "Sign in to Grok" in text:
            return False
        return True

    async def _load_compacted_history(self, *, acct_id, chat_id, settings, access_token):
        history = load_messages(self.storage_dir, acct_id, chat_id)
        if not needs_compact(history):
            return history
        head, tail = split_for_compact(history)
        transcript = format_transcript(head)
        if not transcript:
            save_messages(self.storage_dir, acct_id, chat_id, tail)
            return tail
        summary = await complete_chat(
            session=None,
            settings=settings,
            access_token=access_token,
            user_content=(
                "Summarize this conversation for later turns. "
                "Keep names, decisions, files, and open tasks. Be concise.\n\n%s" % transcript
            ),
        )
        if self._grok_reply_ok(summary):
            compacted = apply_compact_summary(summary, tail)
        else:
            compacted = tail
        save_messages(self.storage_dir, acct_id, chat_id, compacted)
        return compacted

    async def _run_grok_tool(self, name, arguments):
        try:
            result = tool_result(self.storage_dir, name, arguments)
        except ExternalError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        send = result.get("send")
        if not send:
            return json.dumps(result)
        attachments = self._attachments_from_refs(send.get("files") or [])
        if (send.get("files") or []) and not attachments:
            return json.dumps({"ok": False, "error": "files could not be sent"})
        sent = await self.send_outbound(
            text=send["text"],
            attachments=attachments or None,
            chat_id=send["chat_id"],
            acct_id=send["acct_id"],
            platform=send["platform"],
            chat_type=send["chat_type"])
        if not sent:
            return json.dumps({"ok": False, "error": "send failed"})
        record_outbound(self.storage_dir, send["acct_id"], send["chat_id"], send["text"])
        return json.dumps({
            "ok": True,
            "sent": True,
            "acct_id": send["acct_id"],
            "chat_id": send["chat_id"],
            "user_id": send["user_id"],
        })

    def _append_chat_turn(self, *, acct_id, chat_id, user_text, reply_text, sender=None, platform="", chat_type=""):
        history = load_messages(self.storage_dir, acct_id, chat_id)
        if not history or history[-1].get("role") != "user" or history[-1].get("content") != user_text:
            user_message = {"role": "user", "content": user_text}
            if sender:
                user_message["sender"] = sender
            history.append(user_message)
        history.append({"role": "assistant", "content": reply_text})
        save_messages(self.storage_dir, acct_id, chat_id, history)
        remember_chat_route(self.storage_dir, acct_id, chat_id, platform=platform, chat_type=chat_type)
