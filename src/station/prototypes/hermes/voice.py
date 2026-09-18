import asyncio
import contextlib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from station import logger
from station.prototypes.boundary import ext_bool, ext_dict, ext_mapping_get, ext_str
from station.prototypes.voice_policy import (
    should_send_voice_reply,
    voice_delivery_mime,
    voice_delivery_suffix,
)

STATION_SRC = Path(__file__).resolve().parents[3]

@dataclass(slots=True)
class VoiceInputContext:
    remaining_attachments: list = field(default_factory=list)
    transcript_parts: list = field(default_factory=list)
    had_audio_input: bool = False

    @property
    def transcript_text(self):
        return "\n\n".join(part for part in self.transcript_parts if part).strip()

class HermesVoice:
    async def _prepare_voice_input(self, *, attachments):
        context = VoiceInputContext()
        audio_count = sum(
            1
            for attachment in attachments
            if self._is_audio_attachment(attachment, local_path=self._attachment_str(attachment, "local_path"))
        )

        for attachment in attachments:
            local_path = self._attachment_str(attachment, "local_path")
            if not self._is_audio_attachment(attachment, local_path=local_path):
                context.remaining_attachments.append(attachment)
                continue

            context.had_audio_input = True
            display_name = self._attachment_display_name(attachment, local_path=local_path)
            if not local_path or not os.path.exists(local_path):
                logger.warning(
                    "Hermes voice input missing local audio path model_id=%s attachment=%s",
                    self.model_id,
                    display_name,
                )
                continue

            result = await self._invoke_speech_runtime(
                command="transcribe",
                payload={"file_path": local_path},
            )
            transcript = ext_mapping_get(result, "transcript", (str,), "").strip()
            if result.get("success") and transcript:
                if audio_count > 1:
                    context.transcript_parts.append("[voice message: %s]\n%s" % (display_name, transcript))
                else:
                    context.transcript_parts.append(transcript)
                continue

            logger.warning(
                "Hermes voice transcription failed model_id=%s attachment=%s error=%s",
                self.model_id,
                display_name,
                ext_mapping_get(result, "error", (str,), "no transcript returned").strip(),
            )
        return context

    def _combine_user_text(self, *, text, voice_input):
        parts = []
        if text:
            parts.append(text)
        transcript_text = voice_input.transcript_text
        if transcript_text:
            parts.append(("[voice_transcript]\n" + transcript_text) if text else transcript_text)
        return "\n\n".join(parts).strip()

    def _should_send_voice_reply(self, *, incoming_had_audio):
        reply_mode = self._settings.voice_reply_mode if self._settings is not None else "voice_only"
        return should_send_voice_reply(reply_mode=reply_mode, incoming_had_audio=incoming_had_audio)

    async def _send_reply(self, *, chat_id, acct_id, text, include_voice, platform="", chat_type=""):
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
            await self._send_voice(
                chat_id=chat_id,
                acct_id=acct_id,
                text=text,
                platform=platform,
                chat_type=chat_type,
            )
        except Exception as e:
            logger.error(
                "Hermes voice reply failed model_id=%s chat_id=%s acct_id=%s error=%s",
                self.model_id,
                chat_id,
                acct_id,
                e,
                exc_info=e,
            )

    async def _send_voice(self, *, chat_id, acct_id, text, platform="", chat_type=""):
        settings = self._settings
        if settings is None:
            raise RuntimeError("Hermes voice reply settings are not initialized")

        delivery = settings.voice_delivery_method
        output_path = Path(self.storage_dir) / "voice_replies" / ("%s%s" % (uuid.uuid4().hex, voice_delivery_suffix(delivery)))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        result = await self._invoke_speech_runtime(
            command="synthesize",
            payload={"text": text, "output_path": str(output_path)},
        )
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

    async def _invoke_speech_runtime(self, *, command, payload, timeout_s=300.0):
        settings = self._settings
        hermes_home = self._hermes_home
        if settings is None or hermes_home is None:
            raise RuntimeError("Hermes speech runtime is not initialized")

        env = os.environ.copy()
        env["HERMES_HOME"] = str(hermes_home)
        env["HERMES_PYTHON_SRC_ROOT"] = str(settings.hermes_root)
        env.update(settings.extra_env)

        python_paths = [str(STATION_SRC), str(settings.hermes_root)]
        existing = env.get("PYTHONPATH", "").strip()
        if existing:
            python_paths.extend(part for part in existing.split(os.pathsep) if part)
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(python_paths))

        proc = await asyncio.create_subprocess_exec(
            settings.hermes_python,
            "-m",
            "station.prototypes.hermes.speech_runtime",
            command,
            cwd=str(settings.hermes_root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate((json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise RuntimeError("Hermes speech runtime timed out while running %s" % command) from exc

        stdout_text = stdout.decode("utf-8", "replace").strip()
        stderr_text = stderr.decode("utf-8", "replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(
                "Hermes speech runtime failed for %s: %s"
                % (command, stderr_text or stdout_text or ("exit code %s" % proc.returncode))
            )

        lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
        if not lines:
            raise RuntimeError("Hermes speech runtime produced no output for %s" % command)

        return ext_dict("Hermes speech runtime payload", json.loads(lines[-1]))
