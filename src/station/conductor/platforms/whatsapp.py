import asyncio
import base64
import json
import os
import time

from station.conductor.platforms import guidance
from station.conductor.platforms.base import LocalPlatformAdapter
from station.conductor.util import node_bin, whatsapp_bridge_dir, ext_int, ext_str, ext_id
from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_list

class WhatsAppIO:
    def __init__(self, conductor):
        self.conductor = conductor
        self._serve_procs = {}
        self._serve_ports = {}
        self._serve_lock = asyncio.Lock()

    def session_dir(self, acct_id):
        return self.conductor.pair_manager.session_dir(platform="whatsapp", acct_id=acct_id)

    async def ensure_bridge(self, acct_id):
        if not acct_id:
            return None
        async with self._serve_lock:
            existing = self._serve_procs.get(acct_id)
            if existing and existing.returncode is None:
                return self._serve_ports.get(acct_id)
            session_dir = self.session_dir(acct_id)
            if not os.path.isdir(session_dir):
                return None
            bridge_dir = whatsapp_bridge_dir()
            serve_js = bridge_dir / "serve.js"
            if not serve_js.is_file():
                return None
            proc = await asyncio.create_subprocess_exec(
                node_bin(),
                serve_js,
                "--session",
                session_dir,
                "--port",
                "0",
                cwd=bridge_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            port = None
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=max(0.1, deadline - time.time()),
                    )
                except asyncio.TimeoutError:
                    break
                if not line:
                    break
                try:
                    evt = json.loads(line.decode("utf-8", errors="replace").strip())
                except json.JSONDecodeError:
                    continue
                evt = ext_dict('whatsapp bridge event', evt)
                if evt.get("event") == "listening":
                    port = ext_int(evt.get("port"), "port")
                    break
            if not port:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                return None
            self._serve_procs[acct_id] = proc
            self._serve_ports[acct_id] = port
            return port

    async def fetch_messages(self, port):
        session = self.conductor.session
        url = f"http://127.0.0.1:{int(port)}/messages"
        async with session.get(url) as resp:
            if resp.status != 200:
                return []
            body = await resp.json(content_type=None)
        body = ext_dict('whatsapp messages body', body)
        messages = body.get("messages")
        if messages is None:
            return []
        messages = ext_list('whatsapp messages', messages)
        return messages

    async def send_text(self, *, port, chat_id, text, reply_to=None):
        session = self.conductor.session
        url = f"http://127.0.0.1:{int(port)}/send"
        payload = {"chat_id": chat_id, "text": text}
        if reply_to:
            payload["reply_to"] = reply_to
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                return False
            body = await resp.json(content_type=None)
        body = ext_dict('whatsapp send body', body)
        return bool(body.get("ok"))

    async def send_media(self, *, port, chat_id, kind, file_bytes, filename="", mime="", caption="", reply_to=None):
        session = self.conductor.session
        url = f"http://127.0.0.1:{int(port)}/send-media"
        payload = {
            "chat_id": chat_id,
            "type": kind,
            "filename": filename or "file",
            "mime": mime or "",
            "caption": caption or "",
            "data": base64.b64encode(file_bytes or b"").decode("ascii"),
        }
        if reply_to:
            payload["reply_to"] = reply_to
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                return False
            body = await resp.json(content_type=None)
        body = ext_dict('whatsapp send-media body', body)
        return bool(body.get("ok"))

    async def download_media(self, *, port, media_id):
        media_id = (media_id or "").strip()
        if not media_id:
            return None
        session = self.conductor.session
        url = f"http://127.0.0.1:{int(port)}/media/{media_id}"
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            return await resp.read()

    async def close(self):
        async with self._serve_lock:
            for proc in list(self._serve_procs.values()):
                if proc and proc.returncode is None:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        continue
            self._serve_procs.clear()
            self._serve_ports.clear()

class WhatsAppAdapter(LocalPlatformAdapter):
    acct_type = "whatsapp"
    capabilities = frozenset({"send_message", "send_media", "download_file"})

    def __init__(self, conductor):
        self.conductor = conductor
        self.io = WhatsAppIO(conductor)

    async def ensure_account_ready(self, acct_id):
        port = await self.io.ensure_bridge(acct_id)
        return bool(port)

    async def send_message(self, *, acct, token, chat_id, text, reply_to=None, keyboard=None):
        acct_id = acct.get("acct_id") or ""
        port = await self.io.ensure_bridge(acct_id)
        if not port:
            return False
        chunks = guidance.prepare_outbound_text("whatsapp", (text or ""))
        if not chunks:
            return False
        last_ok = False
        for idx, chunk in enumerate(chunks):
            ok = await self.io.send_text(
                port=port,
                chat_id=chat_id,
                text=chunk,
                reply_to=reply_to if idx == 0 else None,
            )
            if not ok:
                return False
            last_ok = True
        return last_ok

    def _kind_from_method(self, method):
        if method == "send_photo":
            return "photo"
        if method == "send_video":
            return "video"
        if method == "send_audio":
            return "audio"
        if method == "send_voice":
            return "audio"
        if method == "send_animation":
            return "animation"
        return "document"

    async def send_media(
        self,
        *,
        acct,
        token,
        chat_id,
        method,
        payload,
        file_bytes,
        caption,
        reply_to,
    ):
        acct_id = acct.get("acct_id") or ""
        port = await self.io.ensure_bridge(acct_id)
        if not port:
            return False
        if file_bytes is None:
            return False
        kind = self._kind_from_method(method)
        try:
            guidance.check_media_size("whatsapp", len(file_bytes), guidance.media_kind_from_method(method))
        except guidance.MediaTooLargeError:
            return False
        filename = ext_str(payload.get("file_name"), "file_name", default="file")
        mime = ext_str(payload.get("content_type"), "content_type")
        prepared = guidance.prepare_outbound_caption("whatsapp", (caption or ""))
        return await self.io.send_media(
            port=port,
            chat_id=chat_id,
            kind=kind,
            file_bytes=file_bytes,
            filename=filename,
            mime=mime,
            caption=prepared,
            reply_to=reply_to,
        )

    async def download_file(self, *, acct, token, file_id):
        acct_id = acct.get("acct_id") or ""
        port = await self.io.ensure_bridge(acct_id)
        if not port:
            return None
        return await self.io.download_media(port=port, media_id=file_id)

    async def poller_loop(self, acct_id):
        while not self.conductor.poller_stop.is_set():
            try:
                port = await self.io.ensure_bridge(acct_id)
                if not port:
                    await asyncio.sleep(5)
                    continue
                messages = await self.io.fetch_messages(port)
                for msg in messages:
                    msg = ext_dict('whatsapp message', msg)
                    chat_id = ext_id(msg.get("chat_id"), "chat_id").strip()
                    text = ext_str(msg.get("text"), "text")
                    user_id = ext_id(msg.get("user_id"), "user_id").strip()
                    if not user_id:
                        raise ExternalError("whatsapp message user_id must be non-empty")
                    message_id = ext_id(msg.get("message_id"), "message_id").strip()
                    media_id = ext_id(msg.get("media_id"), "media_id").strip()
                    if not chat_id or (not text and not media_id):
                        continue
                    model_id = await self.conductor.ensure_chat_mapping(
                        acct_id=acct_id,
                        chat_id=chat_id,
                        chat_type="whatsapp",
                        carrier_user_id=user_id,
                    )
                    if not model_id:
                        continue
                    attachments = []
                    if media_id:
                        attachments.append(
                            {
                                "type": ext_str(msg.get("media_kind"), "media_kind", default="document"),
                                "file_id": media_id,
                                "file_name": ext_str(msg.get("filename"), "filename"),
                                "content_type": ext_str(msg.get("mime"), "mime"),
                            }
                        )
                    body = {
                        "text": text,
                        "caption": ext_str(msg.get("caption"), "caption"),
                        "user_id": user_id,
                        "platform": "whatsapp",
                        "chat_id": chat_id,
                        "attachments": attachments,
                        "msg_id": message_id,
                    }
                    reply_to = ext_str(msg.get("reply_to"), "reply_to").strip()
                    if reply_to:
                        body["reply_to"] = reply_to
                    await self.conductor.deliver_inbound(
                        model_id=model_id,
                        acct_id=acct_id,
                        chat_id=chat_id,
                        request_id=message_id or media_id,
                        body=body,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=whatsapp_poller acct_id=%s error=%s", acct_id, e, exc_info=e)
            await asyncio.sleep(1)

    async def close(self):
        await self.io.close()
