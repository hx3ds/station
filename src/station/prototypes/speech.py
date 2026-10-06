import asyncio
import json
import os

import aiohttp

from station import logger
from station.prototypes.boundary import ext_dict, ext_mapping_get, ext_str
from station.prototypes.launch_settings import normalize_openai_base_url
from station.prototypes.voice import PrototypeVoice


class SpeechSettings:
    __slots__ = ("kind", "base_url", "api_key", "stt_model", "tts_model", "tts_voice")

    def __init__(self, *, kind="", base_url="", api_key="", stt_model="", tts_model="", tts_voice=""):
        self.kind = kind
        self.base_url = base_url
        self.api_key = api_key
        self.stt_model = stt_model
        self.tts_model = tts_model
        self.tts_voice = tts_voice

    def enabled(self):
        return bool(self.kind and self.base_url)


def resolve_speech_settings(*, voice=None, extra_env=None, api_key="", base_url=""):
    voice = voice if voice is not None else {}
    extra_env = extra_env if extra_env is not None else {}
    explicit_base = ext_mapping_get(voice, "base_url", (str,), "")
    explicit_key = ext_mapping_get(voice, "api_key", (str,), "")
    stt_model = ext_mapping_get(voice, "stt_model", (str,), "")
    tts_model = ext_mapping_get(voice, "tts_model", (str,), "")
    tts_voice = ext_mapping_get(voice, "tts_voice_id", (str,), "") or ext_mapping_get(voice, "voice", (str,), "")
    ds_base = (extra_env.get("DS_BASE_URL") or "").strip()
    ds_key = (extra_env.get("DS_API_KEY") or "").strip()
    xai_key = (
        (extra_env.get("XAI_API_KEY") or "").strip()
        or (extra_env.get("GROK_API_KEY") or "").strip()
        or (api_key or "").strip()
    )
    xai_base = (extra_env.get("XAI_BASE_URL") or "").strip() or "https://api.x.ai/v1"
    openai_key = (extra_env.get("OPENAI_API_KEY") or "").strip()
    openai_base = (
        (extra_env.get("OPENAI_BASE_URL") or "").strip()
        or (extra_env.get("LOCAL_LLM_BASE_URL") or "").strip()
        or (base_url or "").strip()
        or "https://api.openai.com/v1"
    )
    if explicit_base:
        return SpeechSettings(
            kind="openai",
            base_url=normalize_openai_base_url(explicit_base),
            api_key=explicit_key or openai_key or xai_key,
            stt_model=stt_model or "whisper-1",
            tts_model=tts_model or "tts-1",
            tts_voice=tts_voice or "alloy",
        )
    if ds_base:
        return SpeechSettings(
            kind="ds",
            base_url=ds_base.rstrip("/"),
            api_key=explicit_key or ds_key,
            stt_model=stt_model,
            tts_model=tts_model,
            tts_voice=tts_voice,
        )
    if xai_key and not openai_key:
        return SpeechSettings(
            kind="xai",
            base_url=xai_base.rstrip("/"),
            api_key=xai_key,
            stt_model=stt_model,
            tts_model=tts_model,
            tts_voice=tts_voice or "eve",
        )
    if openai_key or explicit_key:
        return SpeechSettings(
            kind="openai",
            base_url=normalize_openai_base_url(openai_base),
            api_key=explicit_key or openai_key,
            stt_model=stt_model or "whisper-1",
            tts_model=tts_model or "tts-1",
            tts_voice=tts_voice or "alloy",
        )
    return SpeechSettings()


def _auth_headers(token, *, content_type="application/json"):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    if content_type:
        headers["Content-Type"] = content_type
    return headers


async def transcribe_file(settings, local_path, *, filename=""):
    if settings is None or not settings.enabled() or not local_path or not os.path.isfile(local_path):
        return ""
    with open(local_path, "rb") as f:
        audio_bytes = f.read()
    if not audio_bytes:
        return ""
    name = filename or os.path.basename(local_path) or "audio.wav"
    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_connect=20, sock_read=150)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            if settings.kind == "ds":
                return await _transcribe_ds(session, settings, audio_bytes, name)
            if settings.kind == "xai":
                return await _transcribe_xai(session, settings, audio_bytes, name)
            return await _transcribe_openai(session, settings, audio_bytes, name)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("speech STT failed kind=%s error=%s", settings.kind, e)
        return ""


async def synthesize_text(settings, text):
    prompt = (text or "").strip()
    if settings is None or not settings.enabled() or not prompt:
        return b"", ""
    timeout = aiohttp.ClientTimeout(total=120, connect=20, sock_connect=20, sock_read=90)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            if settings.kind == "ds":
                return await _synthesize_ds(session, settings, prompt)
            if settings.kind == "xai":
                return await _synthesize_xai(session, settings, prompt)
            return await _synthesize_openai(session, settings, prompt)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("speech TTS failed kind=%s error=%s", settings.kind, e)
        return b"", ""


async def _transcribe_openai(session, settings, audio_bytes, filename):
    url = "%s/audio/transcriptions" % settings.base_url.rstrip("/")
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type="application/octet-stream")
    form.add_field("model", settings.stt_model or "whisper-1")
    headers = _auth_headers(settings.api_key, content_type=None)
    async with session.post(url, headers=headers, data=form) as resp:
        raw = await resp.read()
        if resp.status >= 400:
            logger.warning("speech STT openai failed status=%s body=%r", resp.status, raw[:400])
            return ""
        data = json.loads(raw.decode("utf-8"))
        data = ext_dict("stt response", data)
        return ext_str("stt text", data.get("text"), strip=False).strip()


async def _transcribe_xai(session, settings, audio_bytes, filename):
    url = "%s/stt" % settings.base_url.rstrip("/")
    form = aiohttp.FormData()
    form.add_field("file", audio_bytes, filename=filename, content_type="application/octet-stream")
    headers = _auth_headers(settings.api_key, content_type=None)
    async with session.post(url, headers=headers, data=form) as resp:
        raw = await resp.read()
        if resp.status >= 400:
            logger.warning("speech STT xai failed status=%s body=%r", resp.status, raw[:400])
            return ""
        data = json.loads(raw.decode("utf-8"))
        data = ext_dict("stt response", data)
        return ext_str("stt text", data.get("text"), strip=False).strip()


async def _transcribe_ds(session, settings, audio_bytes, filename):
    url = "%s/v1/asr/transcribe" % settings.base_url.rstrip("/")
    form = aiohttp.FormData()
    form.add_field("audio", audio_bytes, filename=filename)
    headers = _auth_headers(settings.api_key, content_type=None)
    params = {}
    if settings.stt_model:
        params["model"] = settings.stt_model
    async with session.post(url, headers=headers, params=params, data=form) as resp:
        raw = await resp.read()
        if resp.status >= 400:
            logger.warning("speech STT ds failed status=%s body=%r", resp.status, raw[:400])
            return ""
        data = json.loads(raw.decode("utf-8"))
        data = ext_dict("stt response", data)
        return ext_str("stt text", data.get("text") or data.get("transcript"), strip=False).strip()


async def _synthesize_openai(session, settings, text):
    url = "%s/audio/speech" % settings.base_url.rstrip("/")
    payload = {
        "model": settings.tts_model or "tts-1",
        "input": text[:15000],
        "voice": settings.tts_voice or "alloy",
    }
    headers = _auth_headers(settings.api_key)
    async with session.post(url, headers=headers, json=payload) as resp:
        raw = await resp.read()
        if resp.status >= 400:
            logger.warning("speech TTS openai failed status=%s body=%r", resp.status, raw[:400])
            return b"", ""
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if raw:
            return raw, content_type or "audio/mpeg"
        return b"", ""


async def _synthesize_xai(session, settings, text):
    url = "%s/tts" % settings.base_url.rstrip("/")
    payload = {
        "text": text[:15000],
        "voice_id": settings.tts_voice or "eve",
        "language": "en",
        "output_format": {"codec": "mp3", "sample_rate": 24000},
    }
    headers = _auth_headers(settings.api_key)
    async with session.post(url, headers=headers, json=payload) as resp:
        raw = await resp.read()
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if resp.status >= 400:
            logger.warning("speech TTS xai failed status=%s body=%r", resp.status, raw[:400])
            return b"", ""
        if "application/json" in content_type:
            data = json.loads(raw.decode("utf-8"))
            data = ext_dict("tts response", data)
            audio_b64 = ext_str("tts audio", data.get("audio"), strip=False)
            if not audio_b64:
                return b"", ""
            import base64

            mime = ext_str("tts content_type", data.get("content_type"), strip=False) or "audio/mpeg"
            return base64.b64decode(audio_b64), mime
        if raw:
            return raw, content_type or "audio/mpeg"
        return b"", ""


async def _synthesize_ds(session, settings, text):
    url = "%s/v1/tts/synthesize" % settings.base_url.rstrip("/")
    body = {"text": text[:15000]}
    if settings.tts_model:
        body["model"] = settings.tts_model
    if settings.tts_voice:
        body["voice"] = settings.tts_voice
    headers = _auth_headers(settings.api_key)
    async with session.post(url, headers=headers, json=body, params={"format": "wav"}) as resp:
        raw = await resp.read()
        if resp.status >= 400:
            logger.warning("speech TTS ds failed status=%s body=%r", resp.status, raw[:400])
            return b"", ""
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if raw:
            return raw, content_type or "audio/wav"
        return b"", ""


class PrototypeSpeech(PrototypeVoice):
    def _speech_settings(self):
        settings = self._launch_settings()
        return resolve_speech_settings(
            voice=settings.voice,
            extra_env=settings.extra_env,
            api_key=settings.api_key or settings.local_llm_api_key,
            base_url=settings.local_llm_base_url or settings.base_url,
        )

    def _voice_reply_mode(self):
        return self._launch_settings().voice_reply_mode

    async def _prepare_hosted_voice_input(self, attachments, *, keep_untranscribed=True):
        async def transcribe(_attachment, local_path, display_name):
            return await transcribe_file(self._speech_settings(), local_path, filename=display_name)

        return await self._prepare_voice_input(
            attachments,
            transcribe=transcribe,
            keep_untranscribed=keep_untranscribed,
        )

    async def _send_speech_reply(self, *, chat_id, acct_id, text, platform="", chat_type=""):
        audio, content_type = await synthesize_text(self._speech_settings(), text)
        if not audio:
            return
        await self._send_voice_bytes(
            chat_id=chat_id,
            acct_id=acct_id,
            audio_bytes=audio,
            content_type=content_type,
            name="tts",
            platform=platform,
            chat_type=chat_type,
        )

    async def _deliver_turn_reply(self, *, text, include_voice, chat_id, acct_id, platform="", chat_type=""):
        await self._send_text_and_maybe_voice(
            chat_id=chat_id,
            acct_id=acct_id,
            text=text,
            include_voice=include_voice,
            synthesize=lambda reply: self._send_speech_reply(
                chat_id=chat_id,
                acct_id=acct_id,
                text=reply,
                platform=platform,
                chat_type=chat_type,
            ),
            platform=platform,
            chat_type=chat_type,
        )
        await self._drain_station_outbound(
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type,
        )
