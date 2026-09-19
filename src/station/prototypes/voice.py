import os

from station import logger
from station.prototypes.voice_policy import (
    should_send_voice_reply,
    voice_delivery_attachment_type,
    voice_delivery_mime,
    voice_delivery_suffix,
)


class PrototypeVoice:
    async def _prepare_voice_input(self, attachments, *, transcribe):
        remaining = []
        transcripts = []
        had_audio = False
        audio_count = sum(
            1
            for attachment in attachments
            if self._is_audio_attachment(attachment, local_path=self._attachment_str(attachment, "local_path"))
        )
        for attachment in attachments:
            local_path = self._attachment_str(attachment, "local_path")
            if not self._is_audio_attachment(attachment, local_path=local_path):
                remaining.append(attachment)
                continue
            had_audio = True
            display_name = self._attachment_display_name(attachment, local_path=local_path)
            if not local_path or not os.path.exists(local_path):
                logger.warning(
                    "voice input missing local audio path model_id=%s attachment=%s",
                    self.model_id,
                    display_name,
                )
                continue
            transcript = (await transcribe(attachment, local_path, display_name) or "").strip()
            if not transcript:
                continue
            if audio_count > 1:
                transcripts.append("[voice message: %s]\n%s" % (display_name, transcript))
            else:
                transcripts.append(transcript)
        return remaining, "\n\n".join(transcripts), had_audio

    def _combine_user_text(self, *, text, transcript_text):
        parts = []
        if text:
            parts.append(text)
        if transcript_text:
            parts.append(("[voice_transcript]\n" + transcript_text) if text else transcript_text)
        return "\n\n".join(parts).strip()

    def _should_send_voice_reply(self, *, incoming_had_audio, reply_mode="voice_only"):
        return should_send_voice_reply(reply_mode=reply_mode, incoming_had_audio=incoming_had_audio)

    async def _send_voice_bytes(
        self,
        *,
        chat_id,
        acct_id,
        audio_bytes,
        delivery_method,
        content_type="",
        name="voice",
        platform="",
        chat_type="",
    ):
        if not audio_bytes:
            return
        att_type = voice_delivery_attachment_type(delivery_method)
        suffix = voice_delivery_suffix(delivery_method)
        mime = voice_delivery_mime(delivery_method, content_type)
        meta = self.save_temp(
            data=audio_bytes,
            original_name="%s%s" % (name, suffix),
            mime_type=mime,
            ext=suffix.lstrip("."),
        )
        await self.send_outbound(
            attachments=[{"type": att_type, "file_id": meta["file_id"]}],
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )

    async def _send_text_and_maybe_voice(
        self,
        *,
        chat_id,
        acct_id,
        text,
        include_voice,
        synthesize,
        platform="",
        chat_type="",
    ):
        await self.send_outbound(
            text=text,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )
        if not include_voice:
            return
        try:
            await synthesize(text)
        except Exception as e:
            logger.error(
                "voice reply failed model_id=%s chat_id=%s acct_id=%s error=%s",
                self.model_id,
                chat_id,
                acct_id,
                e,
                exc_info=e,
            )
