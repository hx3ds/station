import asyncio
import time

from station import logger
from station.prototypes.boundary import ext_bool, ext_dict, ext_float, ext_int, ext_list, ext_require, ext_str

def _frame_to_s16le_mono_48k(frame, audioop):
    import numpy as np

    raw = frame.to_ndarray()

    if raw.ndim == 2:
        if raw.shape[0] <= 8 and raw.shape[0] < raw.shape[1]:
            channels = raw.shape[0]
            mono = raw[0] if channels == 1 else raw.mean(axis=0)
        else:
            channels = raw.shape[1]
            mono = raw[:, 0] if channels == 1 else raw.mean(axis=1)
    else:
        mono = raw

    mono = np.asarray(mono, dtype=np.float64)
    fmt = (frame.format.name if frame.format is not None else "").lower()
    if "flt" in fmt or "f32" in fmt or mono.dtype == np.float32 or abs(mono).max(initial=0) <= 1.5:
        pcm = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16).tobytes()
    else:
        pcm = np.clip(mono, -32768, 32767).astype(np.int16).tobytes()

    src_rate = frame.sample_rate or 48000
    if src_rate != 48000:
        pcm, _ = audioop.ratecv(pcm, 2, 1, src_rate, 48000, None)
    return pcm

class WebRTCService:
    def __init__(self, *, config=None, session=None):
        self._aiortc = None
        self._RTCPeerConnection = None
        self._RTCSessionDescription = None
        self._MediaStreamTrack = None
        self._MediaBlackhole = None
        self._RTCConfiguration = None
        self._RTCIceServer = None
        self._candidate_from_sdp = None
        self._candidate_to_sdp = None
        self._av = None
        self._Fraction = None
        self._audioop = None
        self._np = None
        self._pc_lock = asyncio.Lock()
        self._pcs = {}

    def _load_aiortc(self):
        if self._aiortc is not None:
            return
        import audioop
        import numpy as np
        import aiortc
        from aiortc import (
            MediaStreamTrack,
            RTCPeerConnection,
            RTCSessionDescription,
            RTCConfiguration,
            RTCIceServer,
        )
        from aiortc.contrib.media import MediaBlackhole
        from aiortc.sdp import candidate_from_sdp, candidate_to_sdp
        import av
        from fractions import Fraction
        self._aiortc = aiortc
        self._MediaStreamTrack = MediaStreamTrack
        self._RTCPeerConnection = RTCPeerConnection
        self._RTCSessionDescription = RTCSessionDescription
        self._MediaBlackhole = MediaBlackhole
        self._candidate_from_sdp = candidate_from_sdp
        self._candidate_to_sdp = candidate_to_sdp
        self._av = av
        self._Fraction = Fraction
        self._RTCConfiguration = RTCConfiguration
        self._RTCIceServer = RTCIceServer
        self._audioop = audioop
        self._np = np

    def _build_rtc_configuration(self, turn):
        turn = ext_dict('turn', turn)
        uris = turn.get("uris")
        username = turn.get("username")
        password = turn.get("password")
        ice_server = self._RTCIceServer(urls=uris, username=username, credential=password)
        return self._RTCConfiguration(iceServers=[ice_server])

    def _new_video_track(self):
        av = self._av
        Fraction = self._Fraction

        class SyntheticVideoTrack(self._MediaStreamTrack):
            kind = "video"

            def __init__(self):
                super().__init__()
                self._t0 = time.time()
                self._fps = 30
                self._time_base = Fraction(1, 90000)
                self._pts = 0

            async def recv(self):
                target = self._t0 + (self._pts / 90000.0)
                delay = target - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)

                frame = av.VideoFrame(width=640, height=480, format="yuv420p")
                for p in frame.planes:
                    p.update(bytes(p.buffer_size))
                frame.pts = self._pts
                frame.time_base = self._time_base
                self._pts += 90000 // self._fps
                return frame

        return SyntheticVideoTrack()

    def _new_outbound_audio_track(self):
        av = self._av
        Fraction = self._Fraction
        np = self._np

        class OutboundPCMTrack(self._MediaStreamTrack):
            kind = "audio"

            def __init__(self):
                super().__init__()
                self._sample_rate = 48000
                self._samples = 960
                self._channels = 2
                self._time_base = Fraction(1, self._sample_rate)
                self._pts = 0
                self._t0 = time.time()
                self._buf = bytearray()
                self._lock = asyncio.Lock()

            async def push_pcm_mono_s16le(self, pcm: bytes):
                if not pcm:
                    return
                async with self._lock:
                    self._buf.extend(pcm)

            async def recv(self):
                target = self._t0 + (self._pts / self._sample_rate)
                delay = target - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)

                need = self._samples * 2
                async with self._lock:
                    if len(self._buf) >= need:
                        chunk = bytes(self._buf[:need])
                        del self._buf[:need]
                    else:
                        chunk = bytes(self._samples * 2)

                mono = np.frombuffer(chunk, dtype=np.int16)
                if mono.size < self._samples:
                    mono = np.pad(mono, (0, self._samples - mono.size))
                stereo = np.column_stack((mono, mono)).reshape(-1)

                frame = av.AudioFrame(format="s16", layout="stereo", samples=self._samples)
                frame.planes[0].update(stereo.astype(np.int16).tobytes())
                frame.sample_rate = self._sample_rate
                frame.pts = self._pts
                frame.time_base = self._time_base
                self._pts += self._samples
                return frame

        return OutboundPCMTrack()

    def _encode_candidate(self, candidate):
        sdp_mid = candidate.sdpMid
        sdp_mline_index = candidate.sdpMLineIndex
        cand_sdp = "candidate:" + self._candidate_to_sdp(candidate)
        return {"candidate": cand_sdp, "sdpMid": sdp_mid, "sdpMLineIndex": sdp_mline_index}

    def _decode_candidate(self, cand_dict):
        cand_dict = ext_dict('candidate', cand_dict)
        candidate_raw = cand_dict.get("candidate")
        candidate_raw = ext_str('candidate', candidate_raw, strip=False)
        cand = candidate_raw.strip()
        if cand.startswith("candidate:"):
            cand = cand.split(":", 1)[1]
        parsed = self._candidate_from_sdp(cand)
        sdp_mid = cand_dict.get("sdpMid")
        if sdp_mid is not None:
            sdp_mid = ext_str('sdpMid', sdp_mid, strip=False)
        sdp_mline_index = cand_dict.get("sdpMLineIndex")
        if sdp_mline_index is not None:
            sdp_mline_index = ext_int("sdpMLineIndex", sdp_mline_index)
        parsed.sdpMid = sdp_mid
        parsed.sdpMLineIndex = sdp_mline_index
        return parsed

    async def _register_pc(self, key, pc, *, audio_out=None, tasks=None, on_ended=None):
        async with self._pc_lock:
            old = self._pcs.get(key)
            self._pcs[key] = {
                "pc": pc,
                "audio_out": audio_out,
                "tasks": list(tasks) if tasks is not None else [],
                "on_ended": on_ended,
                "ts": time.time(),
            }
        if old:
            await self._shutdown_entry(old)

    async def _shutdown_entry(self, entry):
        on_ended = entry["on_ended"]
        for task in entry["tasks"]:
            task.cancel()
        try:
            await entry["pc"].close()
        except Exception as e:
            logger.error("unexpected where=webrtc close pc error=%s", e, exc_info=e)
        if on_ended is not None:
            try:
                result = on_ended()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("unexpected where=webrtc on_ended error=%s", e, exc_info=e)

    async def _close_pc(self, key):
        async with self._pc_lock:
            entry = self._pcs.pop(key, None)
        if not entry:
            return
        await self._shutdown_entry(entry)

    async def handle_offer(
        self,
        *,
        offer_sdp,
        model_id,
        chat_id,
        acct_id,
        request_id,
        call_id=None,
        call_version=None,
        send_webrtc=None,
        turn=None,
        on_audio_in=None,
        on_ready=None,
        on_ended=None,
    ):
        self._load_aiortc()

        configuration = self._build_rtc_configuration(turn) if turn else None
        if configuration is not None:
            pc = self._RTCPeerConnection(configuration=configuration)
        else:
            pc = self._RTCPeerConnection()

        audio_out = self._new_outbound_audio_track()
        video_blackhole = self._MediaBlackhole()
        pc.addTrack(audio_out)
        pc.addTrack(self._new_video_track())

        key = call_id or request_id or "%s:%s" % (model_id, chat_id)
        pump_tasks = []

        async def push_audio(pcm):
            await audio_out.push_pcm_mono_s16le(pcm)

        await self._register_pc(key, pc, audio_out=audio_out, tasks=pump_tasks, on_ended=on_ended)

        candidate_buf = []
        flush_task = None
        sent_candidates = False
        audio_track_seen = False
        video_track_seen = False

        async def flush_candidates():
            nonlocal flush_task, sent_candidates
            await asyncio.sleep(0.05)
            batch = None
            if candidate_buf:
                batch = list(candidate_buf)
                candidate_buf.clear()
            flush_task = None
            if not batch:
                return
            if not call_id:
                return
            content = {"call_id": call_id, "candidates": batch}
            if call_version is not None:
                content["version"] = call_version
            await send_webrtc("m.call.candidates", content)
            sent_candidates = True

        async def flush_candidates_from_sdp():
            nonlocal sent_candidates
            await asyncio.sleep(0.15)
            if sent_candidates:
                return
            if not call_id:
                return
            sdp = pc.localDescription.sdp
            sdp = sdp.replace("\r\n", "\n").replace("\r", "\n")
            idx = -1
            mid = None
            out = []
            for line in sdp.split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.startswith("m="):
                    idx += 1
                    mid = None
                    continue
                if line.startswith("a=mid:"):
                    mid = line.split(":", 1)[1].strip() or None
                    continue
                if line.startswith("a=candidate:"):
                    cand = line[2:].strip()
                    entry = {"candidate": cand}
                    if mid is not None:
                        entry["sdpMid"] = mid
                    if idx >= 0:
                        entry["sdpMLineIndex"] = idx
                    out.append(entry)
            if not out:
                return
            content = {"call_id": call_id, "candidates": out}
            if call_version is not None:
                content["version"] = call_version
            try:
                await send_webrtc("m.call.candidates", content)
                sent_candidates = True
            except Exception as e:
                logger.error("unexpected where=webrtc flush_candidates call_id=%s error=%s", call_id, e, exc_info=e)

        async def report_inbound_media():
            if not call_id:
                return
            audio_ok = False
            video_ok = False

            try:
                deadline = time.time() + 45.0
                while time.time() < deadline:
                    stats = await pc.getStats()
                    for s in stats.values():
                        if s.type != "inbound-rtp":
                            continue
                        kind = s.kind
                        packets_received = s.packetsReceived
                        if packets_received is None:
                            packets_received = 0
                        if packets_received <= 0:
                            continue
                        if kind == "audio":
                            audio_ok = True
                        elif kind == "video":
                            video_ok = True
                    if audio_ok and video_ok:
                        content = {"call_id": call_id, "audio": True, "video": True}
                        if call_version is not None:
                            content["version"] = call_version
                        logger.debug(
                            "webrtc inbound media ok call_id=%s ice_state=%s pc_state=%s",
                            call_id,
                            pc.iceConnectionState,
                            pc.connectionState,
                        )
                        await send_webrtc("org.conductor.webrtc.inbound_media", content)
                        return
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=webrtc_report_inbound_media call_id=%s error=%s", call_id, e, exc_info=e)
            logger.warning(
                "webrtc inbound media timeout call_id=%s ice_state=%s pc_state=%s",
                call_id,
                pc.iceConnectionState,
                pc.connectionState,
            )

        async def pump_inbound_audio(track):
            while True:
                try:
                    frame = await track.recv()
                    if on_audio_in is None:
                        continue
                    pcm = _frame_to_s16le_mono_48k(frame, self._audioop)
                    result = on_audio_in(pcm)
                    if asyncio.iscoroutine(result):
                        await result
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error("unexpected where=webrtc_recv call_id=%s error=%s", call_id, e, exc_info=e)
                    return

        @pc.on("track")
        def on_track(track):
            nonlocal audio_track_seen, video_track_seen
            kind = track.kind
            if kind == "audio":
                audio_track_seen = True
                task = asyncio.create_task(pump_inbound_audio(track))
                pump_tasks.append(task)
            elif kind == "video":
                video_track_seen = True
                video_blackhole.addTrack(track)
            logger.debug("webrtc track received call_id=%s kind=%s", call_id, kind)

        @pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.debug("webrtc ice state call_id=%s state=%s", call_id, pc.iceConnectionState)

        @pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.debug("webrtc pc state call_id=%s state=%s", call_id, pc.connectionState)
            state = pc.connectionState
            if state in ("failed", "closed"):
                await self._close_pc(key)

        @pc.on("icecandidate")
        def on_icecandidate(candidate):
            nonlocal flush_task
            if candidate is None:
                return
            encoded = self._encode_candidate(candidate)
            candidate_buf.append(encoded)
            if flush_task is None or flush_task.done():
                flush_task = asyncio.create_task(flush_candidates())

        offer = self._RTCSessionDescription(sdp=offer_sdp, type="offer")
        await pc.setRemoteDescription(offer)
        await video_blackhole.start()

        answer = await pc.createAnswer()
        try:
            await pc.setLocalDescription(answer)
        except ValueError as e:
            await self._close_pc(key)
            raise RuntimeError("failed to set local description: %s" % e) from e
        except Exception as e:
            logger.error("unexpected where=webrtc setLocalDescription call_id=%s error=%s", call_id, e, exc_info=e)
            await self._close_pc(key)
            raise
        if send_webrtc is not None and call_id:
            asyncio.create_task(flush_candidates_from_sdp())
            asyncio.create_task(report_inbound_media())

        if on_ready is not None:
            result = on_ready(push_audio)
            if asyncio.iscoroutine(result):
                await result

        answer_sdp = pc.localDescription.sdp
        if not answer_sdp.strip():
            raise RuntimeError("failed to create answer")
        return answer_sdp

    async def handle_candidates(self, *, call_id, candidates, request_id=None):
        self._load_aiortc()
        key = call_id or request_id or ""
        if not key:
            return False
        candidates = ext_list('candidates', candidates)
        async with self._pc_lock:
            entry = self._pcs.get(key)
        if not entry:
            return False
        pc = entry["pc"]

        added = 0
        for c in candidates:
            parsed = self._decode_candidate(c)
            await pc.addIceCandidate(parsed)
            added += 1
        return added > 0

    async def handle_hangup(self, *, call_id, request_id=None):
        key = call_id or request_id or ""
        if not key:
            return False
        await self._close_pc(key)
        return True
