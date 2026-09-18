import asyncio
import audioop
import json
import socket
import struct
import threading
import time
from collections import deque

import aiohttp
from station import logger
from station.prototypes.boundary import ext_bool, ext_dict, ext_float, ext_int, ext_list, ext_require, ext_str

def _ext_str(value, name, *, default=None, required=False):
    if value is None:
        if required:
            raise TypeError("%s is required" % name)
        return default
    return ext_str(name, value)

def _ext_int(value, name, *, default=None, required=False):
    if value is None:
        if required:
            raise TypeError("%s is required" % name)
        return default
    return ext_int(name, value)

def _ext_number(value, name, *, default=None):
    if value is None:
        return default
    return ext_float(name, value)

def pcm_stereo_to_mono(pcm_stereo):
    if not pcm_stereo:
        return b""
    return audioop.tomono(pcm_stereo, 2, 0.5, 0.5)

def pcm_mono_to_stereo(pcm_mono):
    if not pcm_mono:
        return b""
    return audioop.tostereo(pcm_mono, 2, 1.0, 1.0)

class DiscordVoiceConnection:
    SAMPLE_RATE = 48000
    CHANNELS = 2
    FRAME_SAMPLES = 960
    FRAME_BYTES_STEREO = FRAME_SAMPLES * CHANNELS * 2
    PREFERRED_MODES = (
        "aead_xchacha20_poly1305_rtpsize",
        "xsalsa20_poly1305_lite",
        "xsalsa20_poly1305_suffix",
        "xsalsa20_poly1305",
    )

    def __init__(self, *, session, guild_id, channel_id, user_id, endpoint, token, session_id):
        self._http = session
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.session_id = session_id
        self._ws = None
        self._udp = None
        self._udp_addr = None
        self._mode = None
        self._secret_key = None
        self._ssrc = 0
        self._sequence = 0
        self._timestamp = 0
        self._incr_nonce = 0
        self._running = False
        self._paused = False
        self._tasks = []
        self._ssrc_to_user = {}
        self._decoders = {}
        self._lock = threading.Lock()
        self._out_queue = deque()
        self._out_lock = asyncio.Lock()
        self._speaking = False
        self.on_audio_in = None
        self.on_ready = None
        self.on_ended = None
        self._loop = None

    def map_ssrc(self, ssrc, user_id):
        with self._lock:
            self._ssrc_to_user[ssrc] = user_id

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    async def push_audio_pcm(self, pcm_s16le_mono_48k):
        if not pcm_s16le_mono_48k:
            return
        stereo = pcm_mono_to_stereo(bytes(pcm_s16le_mono_48k))
        async with self._out_lock:
            self._out_queue.append(stereo)

    async def start(self):
        if self._running:
            return
        if not (self.endpoint and self.token and self.session_id and self.user_id and self.guild_id):
            raise RuntimeError("missing voice credentials")
        self._loop = asyncio.get_running_loop()
        self._running = True
        host = self.endpoint
        scheme = "wss"
        if host.startswith("wss://"):
            host = host[6:]
        elif host.startswith("ws://"):
            scheme = "ws"
            host = host[5:]
        elif host.startswith("127.") or host.startswith("localhost") or host.startswith("0.0.0.0"):
            scheme = "ws"
        url = f"{scheme}://{host}/?v=4"
        self._ws = await self._http.ws_connect(url, heartbeat=None)
        self._tasks.append(asyncio.create_task(self._ws_loop()))

    async def stop(self):
        self._running = False
        for t in list(self._tasks):
            t.cancel()
        self._tasks.clear()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception as e:
                logger.error("unexpected where=discord_voice_ws_close error=%s", e, exc_info=e)
            self._ws = None
        if self._udp is not None:
            try:
                self._udp.close()
            except Exception as e:
                logger.error("unexpected where=discord_voice_udp_close error=%s", e, exc_info=e)
            self._udp = None
        cb = self.on_ended
        self.on_ended = None
        if cb is not None:
            try:
                result = cb()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("unexpected where=discord_voice_on_ended error=%s", e, exc_info=e)

    async def _send_json(self, payload):
        if self._ws is None:
            return
        await self._ws.send_str(json.dumps(payload))

    async def _ws_loop(self):
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    data = ext_dict('discord voice ws message', data)
                    try:
                        await self._handle_ws(data)
                    except TypeError as e:
                        logger.error(
                            "discord voice bad payload guild_id=%s channel_id=%s error=%s",
                            self.guild_id,
                            self.channel_id,
                            e,
                        )
                        continue
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "unexpected where=discord_voice_ws guild_id=%s channel_id=%s user_id=%s error=%s",
                self.guild_id,
                self.channel_id,
                self.user_id,
                e,
                exc_info=e,
            )
        finally:
            if self._running:
                await self.stop()

    async def _handle_ws(self, data):
        op = data.get("op")
        payload = data.get("d")
        if payload is None:
            payload = {}
        else:
            payload = ext_dict('discord voice payload', payload)
        if op == 8:
            interval_ms = _ext_number(payload.get("heartbeat_interval"), "heartbeat_interval", default=0)
            interval = interval_ms / 1000.0
            await self._send_json({
                "op": 0,
                "d": {
                    "server_id": self.guild_id,
                    "user_id": self.user_id,
                    "session_id": self.session_id,
                    "token": self.token,
                },
            })
            if interval > 0:
                self._tasks.append(asyncio.create_task(self._heartbeat_loop(interval)))
            return
        if op == 2:
            await self._on_ready(payload)
            return
        if op == 4:
            key = payload.get("secret_key")
            if key is not None:
                key = ext_list('secret_key', key)
                self._secret_key = bytes(key)
            mode = payload.get("mode")
            if mode is not None:
                mode = ext_str('mode', mode, strip=False)
                self._mode = mode
            await self._send_json({"op": 5, "d": {"speaking": 0, "delay": 0, "ssrc": self._ssrc}})
            self._tasks.append(asyncio.create_task(self._udp_recv_loop()))
            self._tasks.append(asyncio.create_task(self._sender_loop()))
            if self.on_ready is not None:
                result = self.on_ready(self.push_audio_pcm)
                if asyncio.iscoroutine(result):
                    await result
            return
        if op == 5:
            ssrc = payload.get("ssrc")
            user_id = payload.get("user_id")
            if ssrc is not None and user_id is not None:
                self.map_ssrc(_ext_int(ssrc, "ssrc"), _ext_str(user_id, "user_id"))
            return

    async def _heartbeat_loop(self, interval):
        try:
            while self._running:
                await asyncio.sleep(interval)
                await self._send_json({"op": 3, "d": int(time.time() * 1000)})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "unexpected where=discord voice heartbeat guild_id=%s channel_id=%s error=%s",
                self.guild_id,
                self.channel_id,
                e,
                exc_info=e,
            )

    async def _on_ready(self, payload):
        self._ssrc = _ext_int(payload.get("ssrc"), "ssrc", default=0)
        ip = _ext_str(payload.get("ip"), "ip", default="") or ""
        port = _ext_int(payload.get("port"), "port", default=0)
        modes = payload.get("modes")
        if modes is None:
            modes = []
        else:
            modes = ext_list('modes', modes)
        mode = None
        for preferred in self.PREFERRED_MODES:
            if preferred in modes:
                mode = preferred
                break
        if mode is None and modes:
            mode = modes[0]
        if not (ip and port and mode and self._ssrc):
            raise RuntimeError("invalid voice ready payload")
        self._mode = mode
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setblocking(False)
        self._udp_addr = (ip, port)
        await self._loop.sock_connect(self._udp, self._udp_addr)
        local_ip, local_port = await self._discover_ip()
        await self._send_select_protocol(local_ip, local_port, mode)

    async def _send_select_protocol(self, local_ip, local_port, mode):
        await self._send_json({
            "op": 1,
            "d": {
                "protocol": "udp",
                "data": {
                    "address": local_ip,
                    "port": local_port,
                    "mode": mode,
                },
            },
        })

    async def _discover_ip(self, *, attempts=8, per_attempt_timeout=0.75):
        if self._udp is None or self._loop is None:
            raise RuntimeError("udp socket not ready")
        last_err = None
        for attempt in range(1, max(1, attempts) + 1):
            packet = bytearray(74)
            struct.pack_into(">H", packet, 0, 1)
            struct.pack_into(">H", packet, 2, 70)
            struct.pack_into(">I", packet, 4, self._ssrc)
            try:
                await self._loop.sock_sendall(self._udp, packet)
            except OSError as exc:
                last_err = exc
                await asyncio.sleep(0.05 * attempt)
                continue
            deadline = time.monotonic() + per_attempt_timeout
            while time.monotonic() < deadline:
                timeout = max(0.05, deadline - time.monotonic())
                try:
                    data = await asyncio.wait_for(self._loop.sock_recv(self._udp, 128), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                except OSError as exc:
                    last_err = exc
                    break
                if len(data) == 74 and data[1] == 0x02:
                    ip_end = data.index(0, 8)
                    ip = data[8:ip_end].decode("ascii")
                    port = struct.unpack_from(">H", data, len(data) - 2)[0]
                    return ip, port
            await asyncio.sleep(0.05 * attempt)
        raise RuntimeError(f"voice udp discovery failed after {attempts} attempts: {last_err}")

    async def _set_speaking(self, speaking):
        if speaking == self._speaking:
            return
        self._speaking = speaking
        await self._send_json({"op": 5, "d": {"speaking": 1 if speaking else 0, "delay": 0, "ssrc": self._ssrc}})

    def _encrypt(self, header, opus_packet):
        import nacl.secret
        import nacl.utils

        mode = self._mode or ""
        key = self._secret_key
        if not key:
            return None
        if mode == "aead_xchacha20_poly1305_rtpsize":
            box = nacl.secret.Aead(key)
            nonce = bytearray(24)
            nonce[:4] = struct.pack(">I", self._incr_nonce)
            self._incr_nonce = (self._incr_nonce + 1) & 0xFFFFFFFF
            return header + box.encrypt(bytes(opus_packet), bytes(header), bytes(nonce)).ciphertext + nonce[:4]
        box = nacl.secret.SecretBox(key)
        if mode == "xsalsa20_poly1305":
            nonce = bytearray(24)
            nonce[:12] = header
            return header + box.encrypt(bytes(opus_packet), bytes(nonce)).ciphertext
        if mode == "xsalsa20_poly1305_suffix":
            nonce = nacl.utils.random(nacl.secret.SecretBox.NONCE_SIZE)
            return header + box.encrypt(bytes(opus_packet), nonce).ciphertext + nonce
        if mode == "xsalsa20_poly1305_lite":
            nonce = bytearray(24)
            nonce[:4] = struct.pack(">I", self._incr_nonce)
            self._incr_nonce = (self._incr_nonce + 1) & 0xFFFFFFFF
            return header + box.encrypt(bytes(opus_packet), bytes(nonce)).ciphertext + nonce[:4]
        return None

    def _decrypt(self, data):
        import nacl.secret

        if len(data) < 16 or not self._secret_key:
            return None, None
        if (data[0] >> 6) != 2 or (data[1] & 0x7F) != 0x78:
            return None, None
        first_byte = data[0]
        _b0, _b1, _seq, _timestamp, ssrc = struct.unpack_from(">BBHII", data, 0)
        if ssrc == self._ssrc:
            return None, None
        cc = first_byte & 0x0F
        has_extension = bool(first_byte & 0x10)
        has_padding = bool(first_byte & 0x20)
        header_size = 12 + (4 * cc) + (4 if has_extension else 0)
        if len(data) < header_size + 4:
            return None, None
        ext_data_len = 0
        if has_extension:
            ext_preamble_offset = 12 + (4 * cc)
            ext_words = struct.unpack_from(">H", data, ext_preamble_offset + 2)[0]
            ext_data_len = ext_words * 4
        header = bytes(data[:header_size])
        payload_with_nonce = data[header_size:]
        mode = self._mode or ""
        try:
            if mode == "aead_xchacha20_poly1305_rtpsize":
                box = nacl.secret.Aead(self._secret_key)
                nonce = bytearray(24)
                nonce[:4] = payload_with_nonce[-4:]
                decrypted = box.decrypt(bytes(payload_with_nonce[:-4]), header, bytes(nonce))
            elif mode == "xsalsa20_poly1305":
                box = nacl.secret.SecretBox(self._secret_key)
                nonce = bytearray(24)
                nonce[:12] = header
                decrypted = box.decrypt(bytes(payload_with_nonce), bytes(nonce))
            elif mode == "xsalsa20_poly1305_suffix":
                box = nacl.secret.SecretBox(self._secret_key)
                nonce = payload_with_nonce[-24:]
                decrypted = box.decrypt(bytes(payload_with_nonce[:-24]), bytes(nonce))
            elif mode == "xsalsa20_poly1305_lite":
                box = nacl.secret.SecretBox(self._secret_key)
                nonce = bytearray(24)
                nonce[:4] = payload_with_nonce[-4:]
                decrypted = box.decrypt(bytes(payload_with_nonce[:-4]), bytes(nonce))
            else:
                return None, None
        except Exception as e:
            logger.debug("discord_voice decrypt failed error=%s", e)
            return None, None
        if ext_data_len and len(decrypted) > ext_data_len:
            decrypted = decrypted[ext_data_len:]
        if has_padding and decrypted:
            pad_len = decrypted[-1]
            if pad_len and pad_len <= len(decrypted):
                decrypted = decrypted[:-pad_len]
        return ssrc, decrypted

    async def _udp_recv_loop(self):
        import discord

        while self._running and self._udp is not None:
            try:
                data = await self._loop.sock_recv(self._udp, 4096)
                if self._paused:
                    continue
                ssrc, opus = self._decrypt(data)
                if ssrc is None or not opus:
                    continue
                with self._lock:
                    decoder = self._decoders.get(ssrc)
                    if decoder is None:
                        decoder = discord.opus.Decoder()
                        self._decoders[ssrc] = decoder
                pcm_stereo = decoder.decode(opus)
                pcm_mono = pcm_stereo_to_mono(pcm_stereo)
                if self.on_audio_in is None:
                    continue
                result = self.on_audio_in(pcm_mono)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "unexpected where=discord_voice_udp_recv guild_id=%s channel_id=%s error=%s",
                    self.guild_id,
                    self.channel_id,
                    e,
                    exc_info=e,
                )

    async def _sender_loop(self):
        import discord

        encoder = discord.opus.Encoder()
        leftover = bytearray()
        silence = b"\x00" * self.FRAME_BYTES_STEREO
        while self._running:
            try:
                started = time.monotonic()
                async with self._out_lock:
                    chunk = self._out_queue.popleft() if self._out_queue else None
                if chunk:
                    leftover.extend(chunk)
                if len(leftover) >= self.FRAME_BYTES_STEREO:
                    frame = bytes(leftover[: self.FRAME_BYTES_STEREO])
                    del leftover[: self.FRAME_BYTES_STEREO]
                    await self._set_speaking(True)
                    self.pause()
                else:
                    if self._speaking and not leftover and chunk is None:
                        await self._set_speaking(False)
                        self.resume()
                    frame = silence if self._speaking else None
                if frame is not None and self._udp is not None and self._secret_key is not None:
                    opus = encoder.encode(frame, self.FRAME_SAMPLES)
                    if opus:
                        header = bytearray(12)
                        header[0] = 0x80
                        header[1] = 0x78
                        struct.pack_into(">H", header, 2, self._sequence)
                        struct.pack_into(">I", header, 4, self._timestamp)
                        struct.pack_into(">I", header, 8, self._ssrc)
                        self._sequence = (self._sequence + 1) & 0xFFFF
                        self._timestamp = (self._timestamp + self.FRAME_SAMPLES) & 0xFFFFFFFF
                        packet = self._encrypt(bytes(header), opus)
                        if packet:
                            await self._loop.sock_sendall(self._udp, packet)
                elapsed = time.monotonic() - started
                await asyncio.sleep(max(0.0, 0.02 - elapsed))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "unexpected where=discord_voice_sender guild_id=%s channel_id=%s error=%s",
                    self.guild_id,
                    self.channel_id,
                    e,
                    exc_info=e,
                )

class DiscordVoiceService:
    def __init__(self, *, session=None):
        self._session = session
        self._lock = asyncio.Lock()
        self._connections = {}

    def _key(self, *, acct_id, guild_id):
        return f"{acct_id}:{guild_id}"

    async def handle_credentials(
        self,
        *,
        content,
        model_id,
        chat_id,
        acct_id,
        request_id=None,
        on_audio_in=None,
        on_ready=None,
        on_ended=None,
    ):
        content = ext_dict('discord voice content', content)
        for key in ("guild_id", "channel_id", "user_id", "endpoint", "token", "session_id"):
            val = content.get(key)
            val = ext_str('val', val, strip=False)
        guild_id = content["guild_id"].strip()
        channel_id = content["channel_id"].strip()
        user_id = content["user_id"].strip()
        endpoint = content["endpoint"].strip()
        token = content["token"].strip()
        session_id = content["session_id"].strip()
        if not guild_id:
            return False
        key = self._key(acct_id=acct_id, guild_id=guild_id)
        async with self._lock:
            old = self._connections.pop(key, None)
            if old is not None:
                await old.stop()
            conn = DiscordVoiceConnection(
                session=self._session,
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=user_id,
                endpoint=endpoint,
                token=token,
                session_id=session_id,
            )
            conn.on_audio_in = on_audio_in
            conn.on_ready = on_ready
            conn.on_ended = on_ended
            self._connections[key] = conn
        try:
            await conn.start()
        except Exception as e:
            logger.error(
                "unexpected where=discord_voice_start guild_id=%s channel_id=%s error=%s",
                guild_id,
                channel_id,
                e,
                exc_info=e,
            )
            async with self._lock:
                if self._connections.get(key) is conn:
                    self._connections.pop(key, None)
            try:
                await conn.stop()
            except Exception as stop_e:
                logger.error("unexpected where=discord_voice_start_stop error=%s", stop_e, exc_info=stop_e)
            return False
        return True

    async def handle_left(self, *, content, acct_id):
        content = ext_dict('discord voice content', content)
        try:
            guild_id = ext_str("guild_id", content.get("guild_id"), default="")
        except TypeError:
            return False
        if not guild_id:
            return False
        key = self._key(acct_id=acct_id, guild_id=guild_id)
        async with self._lock:
            conn = self._connections.pop(key, None)
        if conn is not None:
            await conn.stop()
        return True

    async def close(self):
        async with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()
        for conn in conns:
            try:
                await conn.stop()
            except Exception as e:
                logger.error("unexpected where=discord_voice_service_close error=%s", e, exc_info=e)
