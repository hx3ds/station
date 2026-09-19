import asyncio
import json
import os
import random
import re
import socket
from urllib.parse import quote

import aiohttp

from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_bool, ext_dict, ext_list, ext_str
from station.prototypes.gateway_process import SessionEvent, SessionEventBus, close_subprocess, gateway_subprocess_kwargs
from station.prototypes.launch_settings import apply_local_provider_env

LISTENING_RE = re.compile(r"opencode server listening on (https?://\S+)", re.IGNORECASE)
PORT_RANGE_START = 19200
PORT_RANGE_END = 19990
AUTO_APPROVE_MODES = {"always", "allow", "auto"}

def _port_open(hostname, port):
    host = "127.0.0.1" if hostname in {"0.0.0.0", "::", ""} else hostname
    try:
        with socket.create_connection((host, port), timeout=0.15):
            return True
    except OSError:
        return False

def _iter_candidate_ports(hostname, *, preferred=0):
    if preferred > 0:
        return [preferred]

    ports = list(range(PORT_RANGE_START, PORT_RANGE_END))
    random.shuffle(ports)
    available = []
    for port in ports:
        if _port_open(hostname, port):
            continue
        available.append(port)
        if len(available) >= 24:
            break
    if not available:
        raise RuntimeError(
            "No free OpenCode serve port found in %d-%d" % (PORT_RANGE_START, PORT_RANGE_END - 1)
        )
    return available

class OpenCodeServerProcess:
    def __init__(self, *, settings):
        self.settings = settings
        self.base_url = ""
        self.proc = None
        self._session = None
        self._stdout_task = None
        self._stderr_task = None
        self._event_task = None
        self._ready = asyncio.Event()
        self._events = SessionEventBus()
        self._chat_by_session = {}

    async def start(self, *, timeout_s=30.0):
        if self.proc is not None and self.proc.returncode is None and self.base_url:
            return

        workspace = os.path.expanduser(self.settings.workspace_dir)
        os.makedirs(workspace, exist_ok=True)

        env = os.environ.copy()
        env.update(self.settings.extra_env)
        if self.settings.uses_local_llm():
            env = apply_local_provider_env(env, base_url=self.settings.local_llm_base_url)
        if self.settings.server_password:
            env["OPENCODE_SERVER_PASSWORD"] = self.settings.server_password
            env["OPENCODE_SERVER_USERNAME"] = self.settings.server_username

        candidates = _iter_candidate_ports(self.settings.hostname, preferred=self.settings.port)
        last_error = None
        for port in candidates:
            try:
                await self._start_on_port(port=port, workspace=workspace, env=env, timeout_s=timeout_s)
                logger.info("OpenCode serve started base_url=%s workspace=%s", self.base_url, workspace)
                return
            except (OSError, RuntimeError, asyncio.TimeoutError) as exc:
                last_error = exc
                logger.warning("OpenCode serve failed on port %s: %s", port, exc)
                await self._reset_process()
        raise RuntimeError("OpenCode serve failed to start: %s" % last_error)

    async def _start_on_port(self, *, port, workspace, env, timeout_s):
        command = [
            *self.settings.command,
            "serve",
            "--hostname",
            self.settings.hostname,
            "--port",
            str(port),
            *self.settings.serve_args,
        ]
        self._ready.clear()
        self.base_url = "http://%s:%d" % (self.settings.hostname, port)
        self.proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=workspace,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **gateway_subprocess_kwargs(),
        )
        self._stdout_task = asyncio.create_task(self._read_stdout(), name="opencode-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="opencode-stderr")
        await self._wait_listening(port=port, timeout_s=min(timeout_s, 12.0))
        await self._wait_healthy(timeout_s=min(timeout_s, 12.0))
        if self.proc.returncode is not None:
            raise RuntimeError("OpenCode serve exited early with code %s" % self.proc.returncode)
        self._ready.set()
        self._session = aiohttp.ClientSession(
            auth=self._basic_auth(),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=None),
        )
        self._event_task = asyncio.create_task(self._read_events(), name="opencode-events")

    async def _reset_process(self):
        tasks = [self._stdout_task, self._stderr_task, self._event_task]
        self._stdout_task = None
        self._stderr_task = None
        self._event_task = None
        session = self._session
        self._session = None
        if session is not None:
            await session.close()
        proc = self.proc
        self.proc = None
        self.base_url = ""
        await close_subprocess(proc, tasks, terminate_timeout_s=2.0)

    async def close(self):
        await self._reset_process()
        self._events.clear()
        self._chat_by_session.clear()

    def bind_session(self, *, session_id, acct_id, chat_id):
        self._chat_by_session[session_id] = (acct_id, chat_id)

    def session_queue(self, session_id):
        return self._events.queue(session_id)

    async def session_create(self, *, title=""):
        body = {}
        if title:
            body["title"] = title
        if self.settings.agent:
            body["agent"] = self.settings.agent
        model = self.settings.model_payload()
        if model:
            body["model"] = {"id": model["modelID"], "providerID": model["providerID"]}
            if "variant" in model:
                body["model"]["variant"] = model["variant"]
        if self.settings.approvals_mode in AUTO_APPROVE_MODES:
            body["permission"] = [{"permission": "*", "pattern": "*", "action": "allow"}]

        result = await self._request_dict("POST", "/session", json_body=body)
        session_id = ext_str("OpenCode session id", result.get("id"))
        if not session_id:
            raise ExternalError("OpenCode session create must return non-empty str id")
        logger.info("OpenCode session created session_id=%s", session_id)
        return session_id

    async def prompt_async(self, *, session_id, parts):
        await self._request_json(
            "POST",
            "/session/%s/prompt_async" % quote(session_id, safe=""),
            json_body=self._prompt_body(parts),
            expect_json=False,
        )

    async def prompt(self, *, session_id, parts):
        return await self._request_dict(
            "POST",
            "/session/%s/message" % quote(session_id, safe=""),
            json_body=self._prompt_body(parts),
        )

    def _prompt_body(self, parts):
        body = {"parts": parts}
        model = self.settings.message_model_payload()
        if model:
            body["model"] = model
        if self.settings.agent:
            body["agent"] = self.settings.agent
        if self.settings.variant:
            body["variant"] = self.settings.variant
        return body

    async def reply_permission(self, *, request_id, reply="always"):
        await self._request_json(
            "POST",
            "/permission/%s/reply" % quote(request_id, safe=""),
            json_body={"reply": reply},
            expect_json=False,
        )

    async def auth_set(self, *, provider_id, body):
        await self._request_json(
            "PUT",
            "/auth/%s" % quote(provider_id, safe=""),
            json_body=body,
            expect_json=False,
        )

    async def auth_remove(self, *, provider_id):
        await self._request_json(
            "DELETE",
            "/auth/%s" % quote(provider_id, safe=""),
            expect_json=False,
        )

    async def provider_list(self):
        return await self._request_dict("GET", "/provider")

    async def provider_auth_methods(self):
        return await self._request_dict("GET", "/provider/auth")

    async def oauth_authorize(self, *, provider_id, method, inputs=None):
        body = {"method": method}
        if inputs:
            body["inputs"] = inputs
        return await self._request_dict(
            "POST",
            "/provider/%s/oauth/authorize" % quote(provider_id, safe=""),
            json_body=body,
        )

    async def oauth_callback(self, *, provider_id, method, code=None):
        body = {"method": method}
        if code is not None:
            body["code"] = code
        return await self._request_json(
            "POST",
            "/provider/%s/oauth/callback" % quote(provider_id, safe=""),
            json_body=body,
            expect_json=False,
        )

    async def provider_connected(self, provider_id):

        payload = await self.provider_list()
        connected = payload.get("connected")
        if connected is None:
            return False
        connected = ext_list("OpenCode provider connected", connected)
        needle = provider_id.strip().lower()
        for item in connected:
            item = ext_str("OpenCode provider connected item", item)
            if item.strip().lower() == needle:
                return True
        return False

    async def resolve_oauth_method_index(self, *, provider_id):

        methods_by_provider = await self.provider_auth_methods()
        methods = methods_by_provider.get(provider_id)
        if methods is None:
            methods = methods_by_provider.get(provider_id.strip().lower())
        if methods is None:
            raise RuntimeError("OpenCode has no auth methods for provider %s" % provider_id)
        methods = ext_list("OpenCode provider auth methods", methods)

        first_oauth = -1
        for index, method in enumerate(methods):
            method = ext_dict("OpenCode provider auth method", method)
            method_type = ext_str("OpenCode provider auth method type", method.get("type"))
            if method_type.strip().lower() != "oauth":
                continue
            if first_oauth < 0:
                first_oauth = index
            label = ext_str("OpenCode provider auth method label", method.get("label")).lower()
            if "supergrok" in label or "subscription" in label or "headless" in label:
                return index
        if first_oauth >= 0:
            return first_oauth
        raise RuntimeError("OpenCode has no OAuth method for provider %s" % provider_id)

    def _basic_auth(self):
        if not self.settings.server_password:
            return None
        return aiohttp.BasicAuth(self.settings.server_username or "opencode", self.settings.server_password)

    async def _wait_listening(self, *, port, timeout_s):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        needle = ":%d" % port
        while loop.time() < deadline:
            if self.proc.returncode is not None:
                raise RuntimeError("OpenCode serve exited early with code %s" % self.proc.returncode)
            if self._ready.is_set() and needle in self.base_url:
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("OpenCode serve did not publish listening URL for port %d" % port)

    async def _wait_healthy(self, *, timeout_s):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        last_error = None
        while loop.time() < deadline:
            if self.proc.returncode is not None:
                raise RuntimeError(
                    "OpenCode serve exited early with code %s: %s" % (self.proc.returncode, last_error)
                )
            try:
                async with aiohttp.ClientSession(auth=self._basic_auth()) as session:
                    async with session.get("%s/global/health" % self.base_url) as resp:
                        if resp.status < 400:
                            payload = ext_dict("OpenCode health payload", await resp.json(content_type=None))
                            healthy = payload.get("healthy")
                            if healthy is None:
                                continue
                            if ext_bool("OpenCode health.healthy", healthy):
                                return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
            await asyncio.sleep(0.1)
        raise RuntimeError("OpenCode server did not become healthy: %s" % last_error)

    async def _request_json(self, method, path, *, json_body=None, expect_json=True):
        if self._session is None or not self.base_url:
            raise RuntimeError("OpenCode server is not running")
        async with self._session.request(method, "%s%s" % (self.base_url, path), json=json_body) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise RuntimeError("OpenCode %s %s failed (%d): %s" % (method, path, resp.status, text[:500]))
            if not expect_json or not text.strip():
                return None
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise ExternalError("OpenCode %s %s returned non-JSON: %s" % (method, path, text[:500])) from exc

    async def _request_dict(self, method, path, *, json_body=None):

        return ext_dict("OpenCode %s %s" % (method, path), await self._request_json(method, path, json_body=json_body))

    def _note_listening(self, text):
        match = LISTENING_RE.search(text)
        if not match:
            return
        url = match.group(1).rstrip("/")
        if self.base_url and url != self.base_url.rstrip("/"):
            logger.warning("OpenCode listening URL mismatch expected=%s actual=%s", self.base_url, url)
            return
        self.base_url = url
        self._ready.set()

    async def _read_stdout(self):
        stream = self.proc.stdout
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.info("opencode serve stdout: %s", text)
                self._note_listening(text)

    async def _read_stderr(self):
        stream = self.proc.stderr
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.info("opencode serve stderr: %s", text)
                self._note_listening(text)

    async def _read_events(self):
        url = "%s/event" % self.base_url
        while True:
            try:
                async with self._session.get(url, headers={"Accept": "text/event-stream"}) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise RuntimeError("OpenCode event stream failed (%d): %s" % (resp.status, body[:300]))
                    buffer = ""
                    async for raw in resp.content.iter_any():
                        if not raw:
                            continue
                        buffer += raw.decode("utf-8", "replace")
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            await self._handle_sse_line(line.rstrip("\r"))
                    if buffer.strip():
                        await self._handle_sse_line(buffer.rstrip("\r"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("unexpected where=opencode_event_stream url=%s error=%s", url, e, exc_info=e)
                await asyncio.sleep(1.0)

    async def _handle_sse_line(self, line):
        if not line.startswith("data:"):
            return
        raw = line[5:].strip()
        if not raw or raw == "[DONE]":
            return

        try:
            frame = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExternalError("OpenCode SSE malformed JSON: %s" % raw[:400]) from exc
        frame = ext_dict("OpenCode SSE frame", frame)

        event_type = ext_str("OpenCode SSE type", frame.get("type"))
        props = frame.get("properties")
        if props is None:
            props = {}
        props = ext_dict("OpenCode SSE properties", props)

        if event_type in {"permission.asked", "permission.updated"} or event_type.endswith("permission.asked"):
            request_id = props.get("id")
            if request_id is None:
                request_id = props.get("requestID")
            request_id = ext_str("OpenCode permission id", request_id)
            if request_id and self.settings.approvals_mode in AUTO_APPROVE_MODES:
                logger.info("Auto-approving OpenCode permission request_id=%s type=%s", request_id, event_type)
                try:
                    await self.reply_permission(request_id=request_id, reply="always")
                except (aiohttp.ClientError, RuntimeError, TypeError) as e:
                    logger.error("OpenCode permission auto-approve failed request_id=%s error=%s", request_id, e)
                return

        session_id = props.get("sessionID")
        if session_id is None:
            session_id = props.get("session_id")
        session_id = ext_str("OpenCode SSE sessionID", session_id)
        if not session_id:
            part = props.get("part")
            if part is not None:
                part = ext_dict("OpenCode SSE part", part)
                session_id = ext_str("OpenCode SSE part.sessionID", part.get("sessionID"))
        if not session_id:
            return
        await self._events.put(SessionEvent(type=event_type, session_id=session_id, payload=props))
