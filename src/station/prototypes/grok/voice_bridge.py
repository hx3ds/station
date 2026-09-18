import asyncio
import audioop
import os
import wave
from dataclasses import dataclass

import aiohttp

from station import logger

from .realtime import GrokRealtimeSession

@dataclass(slots=True)
class RealtimeCallState:
    session: GrokRealtimeSession
    chat_id: str
    acct_id: str
    platform: str = ""
    chat_type: str = ""
    mute_live_audio: bool = False

class GrokVoiceBridge:
    async def _start_realtime(self, *, call_key, chat_key, acct_key, push_audio_pcm, platform="", chat_type=""):
        if not call_key or not chat_key or not acct_key:
            return None

        settings = self._build_settings()
        access_token = await self._resolve_access_token(acct_id=acct_key, settings=settings)
        if not access_token:
            await self.send_outbound(
                text="Grok realtime voice needs an API key or OAuth sign-in before the call can start.",
                chat_id=chat_key,
                acct_id=acct_key,
                platform=platform,
                chat_type=chat_type,
            )
            return None

        async def on_transcript(kind, text):
            prefix = "TRANSCRIPT_IN:" if kind == "input" else "TRANSCRIPT_OUT:"
            await self.send_outbound(
                text="%s %s" % (prefix, text),
                chat_id=chat_key,
                acct_id=acct_key,
                platform=platform,
                chat_type=chat_type,
            )

        session = GrokRealtimeSession(
            access_token=access_token,
            base_url=settings.base_url,
            model=settings.realtime_model,
            voice=settings.realtime_voice or settings.tts_voice_id,
            instructions=settings.system_prompt or "You are a concise voice assistant for automated tests.",
            sample_rate=settings.realtime_sample_rate,
            push_audio_pcm_48k=push_audio_pcm,
            on_transcript=on_transcript,
        )
        try:
            await session.start()
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError, RuntimeError) as e:
            logger.error("Grok realtime start failed call_id=%s error=%s", call_key, e)
            await self.send_outbound(
                text="Grok realtime voice failed to start.",
                chat_id=chat_key,
                acct_id=acct_key,
                platform=platform,
                chat_type=chat_type,
            )
            return None

        async with self._realtime_guard:
            old = self._realtime_by_call.pop(call_key, None)
            self._realtime_by_call[call_key] = RealtimeCallState(
                session=session,
                chat_id=chat_key,
                acct_id=acct_key,
                platform=platform or "",
                chat_type=chat_type or "",
            )
            self._realtime_by_chat[chat_key] = call_key
        if old is not None:
            await old.session.close()
        logger.info("Grok realtime started call_id=%s chat_id=%s", call_key, chat_key)
        return None

    async def _append_realtime_audio(self, *, call_key, pcm_s16le_mono_48k):
        if not call_key or not pcm_s16le_mono_48k:
            return None
        async with self._realtime_guard:
            state = self._realtime_by_call.get(call_key)
        if state is None or state.mute_live_audio:
            return None
        await state.session.append_pcm_48k_mono(pcm_s16le_mono_48k)
        return None

    async def _end_realtime(self, *, call_key):
        if not call_key:
            return None
        async with self._realtime_guard:
            state = self._realtime_by_call.pop(call_key, None)
            if state is not None and self._realtime_by_chat.get(state.chat_id) == call_key:
                self._realtime_by_chat.pop(state.chat_id, None)
        if state is not None:
            await state.session.close()
            logger.info("Grok realtime ended call_id=%s", call_key)
        return None

    async def on_webrtc_call_ready(self, *, call_id, chat_id, acct_id, model_id, push_audio_pcm):
        return await self._start_realtime(
            call_key=call_id,
            chat_key=chat_id,
            acct_key=acct_id,
            push_audio_pcm=push_audio_pcm,
        )

    async def on_webrtc_audio_in(self, *, call_id, chat_id, acct_id, model_id, pcm_s16le_mono_48k):
        return await self._append_realtime_audio(call_key=call_id, pcm_s16le_mono_48k=pcm_s16le_mono_48k)

    async def on_webrtc_call_ended(self, *, call_id, chat_id, acct_id, model_id):
        return await self._end_realtime(call_key=call_id)

    def _discord_voice_call_key(self, *, guild_id, channel_id, chat_id):
        if chat_id:
            return "discord:%s" % chat_id
        if guild_id and channel_id:
            return "discord:%s:%s" % (guild_id, channel_id)
        return ""

    async def on_discord_voice_ready(self, *, guild_id, channel_id, chat_id, acct_id, model_id, push_audio_pcm):
        call_key = self._discord_voice_call_key(guild_id=guild_id, channel_id=channel_id, chat_id=chat_id)
        return await self._start_realtime(
            call_key=call_key,
            chat_key=chat_id,
            acct_key=acct_id,
            push_audio_pcm=push_audio_pcm,
            platform="discord",
        )

    async def on_discord_voice_audio_in(self, *, guild_id, channel_id, chat_id, acct_id, model_id, pcm_s16le_mono_48k):
        call_key = self._discord_voice_call_key(guild_id=guild_id, channel_id=channel_id, chat_id=chat_id)
        return await self._append_realtime_audio(call_key=call_key, pcm_s16le_mono_48k=pcm_s16le_mono_48k)

    async def on_discord_voice_ended(self, *, guild_id, channel_id, chat_id, acct_id, model_id):
        call_key = self._discord_voice_call_key(guild_id=guild_id, channel_id=channel_id, chat_id=chat_id)
        return await self._end_realtime(call_key=call_key)

    async def _feed_voice_sample(self, state):
        path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "ds", "samples", "voice.wav")
        )
        if not os.path.isfile(path):
            await self.send_outbound(
                text="VOICE_SAMPLE_MISSING",
                chat_id=state.chat_id,
                acct_id=state.acct_id,
                platform=state.platform,
                chat_type=state.chat_type,
            )
            return
        state.mute_live_audio = True
        try:
            with wave.open(path, "rb") as wav:
                channels = wav.getnchannels()
                sample_width = wav.getsampwidth()
                rate = wav.getframerate()
                frames = wav.readframes(wav.getnframes())
            if sample_width != 2 or channels not in {1, 2}:
                await self.send_outbound(
                    text="VOICE_SAMPLE_UNSUPPORTED",
                    chat_id=state.chat_id,
                    acct_id=state.acct_id,
                    platform=state.platform,
                    chat_type=state.chat_type,
                )
                return
            pcm = frames
            if channels == 2:
                pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
            if rate != 48000:
                pcm, _ = audioop.ratecv(pcm, 2, 1, rate, 48000, None)
            await state.session.begin_manual_audio()
            await state.session.append_pcm_48k_mono(pcm)
            await state.session.commit_audio()
            logger.info("Grok realtime fed voice sample bytes=%d path=%s", len(pcm), path)
        finally:
            state.mute_live_audio = False

    async def _active_realtime_for_chat(self, chat_id):
        async with self._realtime_guard:
            active_call_id = self._realtime_by_chat.get(chat_id)
            if not active_call_id:
                return None
            return self._realtime_by_call.get(active_call_id)
