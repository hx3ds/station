import base64
import mimetypes
import os

from station import logger
from station.prototypes.voice import PrototypeVoice

from . import auth_store
from .client import complete_chat, fetch_usage, generate_image, synthesize_speech, transcribe_audio

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
            chat_type=chat_type,
        )

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
            delivery_method=settings.voice_delivery_method,
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
                chat_type=chat_type,
            )
            return
        auth_store.save_credentials(self.storage_dir, acct_id, creds)
        reply = await fetch_usage(session=None, access_token=creds.access)
        await self.send_outbound(
            text=reply,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )

    async def _handle_generate_command(self, *, prompt, chat_id, acct_id, settings, reply_to=None, platform="", chat_type=""):
        if not prompt:
            await self.send_outbound(
                text="Usage: /generate <prompt>",
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
            return

        busy_key = (acct_id, chat_id)
        if busy_key in self._active_chat_requests:
            await self.send_outbound(
                text=BUSY_REPLY_TEXT,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
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
                    chat_type=chat_type,
                )
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
                    chat_type=chat_type,
                )
                return

            await self._send_photo(
                chat_id=chat_id,
                acct_id=acct_id,
                image_bytes=image_bytes,
                mime=mime,
                caption=prompt[:1024],
                platform=platform,
                chat_type=chat_type,
            )
        finally:
            self._active_chat_requests.discard(busy_key)

    async def _run_chat_turn(self, *, chat_id, acct_id, settings, text, photo_attachment, voice_attachment, reply_to=None, platform="", chat_type=""):
        busy_key = (acct_id, chat_id)
        if busy_key in self._active_chat_requests:
            await self.send_outbound(
                text=BUSY_REPLY_TEXT,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )
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
                    chat_type=chat_type,
                )
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
                content = prompt_text
                if photo_data_url:
                    content = [
                        {"type": "text", "text": prompt_text},
                        {"type": "image_url", "image_url": {"url": photo_data_url}},
                    ]
                reply_text = await complete_chat(
                    session=None,
                    settings=settings,
                    access_token=access_token,
                    user_content=content,
                )

            await self.send_outbound(
                text=reply_text,
                chat_id=chat_id,
                acct_id=acct_id,
                platform=platform,
                chat_type=chat_type,
            )

            send_voice = self._should_send_voice_reply(
                incoming_had_audio=voice_attachment is not None,
                reply_mode=settings.voice_reply_mode,
            )
            if send_voice:
                if reply_text and not reply_text.startswith("Grok request failed:") and "Sign in to Grok" not in reply_text:
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
