import asyncio
import audioop
import base64
import json

import aiohttp

from station import logger

def pcm_mono_resample(pcm_s16le, src_rate, dst_rate):
    if not pcm_s16le or src_rate == dst_rate:
        return pcm_s16le
    converted, _ = audioop.ratecv(pcm_s16le, 2, 1, src_rate, dst_rate, None)
    return converted

class GrokRealtimeSession:
    def __init__(
        self,
        *,
        access_token,
        base_url="https://api.x.ai/v1",
        model="grok-voice-latest",
        voice="eve",
        instructions="",
        sample_rate=24000,
        push_audio_pcm_48k=None,
        on_transcript=None,
    ):
        self._token = access_token
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._voice = voice
        self._instructions = instructions
        self._sample_rate = sample_rate
        self._push_audio_pcm_48k = push_audio_pcm_48k
        self._on_transcript = on_transcript
        self._ws = None
        self._session = None
        self._reader_task = None
        self._session_ack = asyncio.Event()
        self._buffer_cleared = asyncio.Event()
        self._buffer_committed = asyncio.Event()
        self._send_lock = asyncio.Lock()

    def _ws_url(self):
        http = self._base_url
        if http.startswith("https://"):
            ws = "wss://" + http[len("https://") :]
        elif http.startswith("http://"):
            ws = "ws://" + http[len("http://") :]
        else:
            ws = http
        return "%s/realtime?model=%s" % (ws, self._model)

    async def start(self):
        if not self._token:
            raise RuntimeError("missing access token for Grok realtime")
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None)
        self._session = aiohttp.ClientSession(
            timeout=timeout,
            trust_env=True,
            connector=aiohttp.TCPConnector(ssl=True, force_close=True, limit=4),
        )
        headers = {"Authorization": "Bearer %s" % self._token}
        self._ws = await self._session.ws_connect(self._ws_url(), headers=headers, heartbeat=20)
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._update_session(
            {
                "voice": self._voice,
                "instructions": self._instructions or "You are a concise voice assistant.",
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "silence_duration_ms": 700,
                },
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self._sample_rate},
                        "transcription": {"model": "grok-transcribe"},
                    },
                    "output": {"format": {"type": "audio/pcm", "rate": self._sample_rate}},
                },
            },
            timeout=20,
        )

    async def _send(self, payload):
        if self._ws is None or self._ws.closed:
            return
        async with self._send_lock:
            await self._ws.send_json(payload)

    async def _update_session(self, session_payload, *, timeout=5):
        self._session_ack.clear()
        await self._send({"type": "session.update", "session": session_payload})
        try:
            await asyncio.wait_for(self._session_ack.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Grok realtime session.update ack timeout")

    async def begin_manual_audio(self):
        self._buffer_cleared.clear()
        await self._send({"type": "input_audio_buffer.clear"})
        try:
            await asyncio.wait_for(self._buffer_cleared.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Grok realtime input_audio_buffer.clear ack timeout")
        await self._update_session({"turn_detection": None})

    async def append_pcm_48k_mono(self, pcm_s16le_mono_48k):
        if not pcm_s16le_mono_48k:
            return
        pcm = pcm_mono_resample(pcm_s16le_mono_48k, 48000, self._sample_rate)
        if not pcm:
            return
        step = self._sample_rate * 2 // 5
        for i in range(0, len(pcm), step):
            chunk = pcm[i : i + step]
            await self._send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )

    async def commit_audio(self):
        self._buffer_committed.clear()
        await self._send({"type": "input_audio_buffer.commit"})
        try:
            await asyncio.wait_for(self._buffer_committed.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Grok realtime input_audio_buffer.commit ack timeout")
        await self._send({"type": "response.create"})
        await self._update_session(
            {
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "silence_duration_ms": 700,
                }
            }
        )

    async def send_text(self, text):
        prompt = text.strip()
        if not prompt:
            return
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def _emit_transcript(self, kind, text):
        cleaned = text.strip()
        if not cleaned or self._on_transcript is None:
            return
        await self._on_transcript(kind, cleaned)

    async def _handle_event(self, event):

        from station.prototypes.boundary import ext_dict, ext_str

        event = ext_dict("realtime event", event)
        etype = event.get("type")
        if etype is not None:
            etype = ext_str("realtime event type", etype)
        if etype in {"session.created", "session.updated"}:
            self._session_ack.set()
            return
        if etype == "input_audio_buffer.cleared":
            self._buffer_cleared.set()
            return
        if etype == "input_audio_buffer.committed":
            self._buffer_committed.set()
            return
        if etype == "conversation.item.input_audio_transcription.completed":
            text = ext_str("transcript", event.get("transcript"))
            if text.strip():
                await self._emit_transcript("input", text)
            return
        if etype == "response.output_audio_transcript.done":
            text = ext_str("transcript", event.get("transcript"))
            if text.strip():
                await self._emit_transcript("output", text)
            return
        if etype == "response.output_audio.delta":
            audio_b64 = event.get("delta")
            if audio_b64 is None:
                audio_b64 = event.get("audio")
            if audio_b64 is None:
                return
            audio_b64 = ext_str("audio delta", audio_b64)
            if not audio_b64 or self._push_audio_pcm_48k is None:
                return
            pcm_24k = base64.b64decode(audio_b64)
            pcm_48k = pcm_mono_resample(pcm_24k, self._sample_rate, 48000)
            if pcm_48k:
                await self._push_audio_pcm_48k(pcm_48k)
            return
        if etype == "error":
            logger.warning("Grok realtime error event: %s", event)

    async def _read_loop(self):
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_event(json.loads(msg.data))
                elif msg.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("unexpected where=grok_realtime_read error=%s", e, exc_info=e)

    async def close(self):
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
