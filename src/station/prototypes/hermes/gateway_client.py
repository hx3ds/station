import asyncio
import os

from station import logger
from station.prototypes.boundary import ext_dict, ext_str
from station.prototypes.gateway_process import SessionEvent, SessionEventBus, gateway_subprocess_kwargs
from station.prototypes.jsonrpc_process import JsonLineRpcProcess

from .usage import stderr_usage_exhausted_abort

class HermesGatewayProcess(JsonLineRpcProcess):
    def __init__(self, *, settings, hermes_home):
        super().__init__(request_id_prefix="hb", stop_error="Hermes gateway stopped")
        self.settings = settings
        self.hermes_home = hermes_home
        self._events = SessionEventBus()
        self._ready = asyncio.Event()
        self._usage_abort_signaled = False

    async def start(self, *, timeout_s=20.0):
        if self.proc is not None and self.proc.returncode is None:
            return

        hermes_home = os.path.abspath(str(self.hermes_home))
        os.makedirs(hermes_home, exist_ok=True)
        env = self.settings.apply_process_env(os.environ)
        env["HERMES_HOME"] = hermes_home
        env.setdefault("HERMES_TUI_TOOLSETS", "file,terminal,web,memory")
        env["HERMES_PYTHON_SRC_ROOT"] = str(self.settings.hermes_root)
        py_path = env.get("PYTHONPATH", "").strip()
        env["PYTHONPATH"] = (
            "%s%s%s" % (self.settings.hermes_root, os.pathsep, py_path)
            if py_path
            else str(self.settings.hermes_root)
        )

        self.proc = await asyncio.create_subprocess_exec(
            self.settings.hermes_python,
            "-m",
            "tui_gateway.entry",
            cwd=str(self.settings.hermes_root),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **gateway_subprocess_kwargs(),
        )
        self._ready.clear()
        self._stdout_task = asyncio.create_task(self._read_stdout(), name="hermes-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr_lines(label="hermes-agent stderr"), name="hermes-stderr")
        await asyncio.wait_for(self._ready.wait(), timeout=timeout_s)

    async def close(self):
        await super().close()

    async def session_create(self):
        params = {"cwd": self.settings.workspace_dir, "source": "station"}
        provider = self.settings.gateway_provider()
        model = self.settings.model
        if self.settings.uses_xai():
            from .config import xai_model_or_default

            model = xai_model_or_default(model)
        if model:
            params["model"] = model
        if provider:
            params["provider"] = provider
        if self.settings.uses_local_llm() and self.settings.local_llm_base_url:
            params["base_url"] = self.settings.local_llm_base_url
        if self.settings.uses_local_llm() and self.settings.local_llm_api_key:
            params["api_key"] = self.settings.local_llm_api_key
        if self.settings.reasoning_effort:
            params["reasoning_effort"] = self.settings.reasoning_effort
        elif self.settings.uses_local_llm():
            params["reasoning_effort"] = "none"
        if self.settings.fast:
            params["fast"] = True

        result = await self._rpc_dict("session.create", params)
        session_id = ext_str("session.create session_id", result.get("session_id"))
        if not session_id:
            raise RuntimeError("Hermes session.create did not return a session_id")
        return session_id

    async def submit_prompt(self, *, session_id, text):
        self._usage_abort_signaled = False
        self._events.queue(session_id)
        self._events.drop_queued(session_id)
        await self.request("prompt.submit", {"session_id": session_id, "text": text})

    async def interrupt_session(self, *, session_id):
        return await self._rpc_dict("session.interrupt", {"session_id": session_id})

    async def attach_image(self, *, session_id, path):
        return await self._rpc_dict("image.attach", {"session_id": session_id, "path": path})

    async def attach_pdf(self, *, session_id, path):
        return await self._rpc_dict("pdf.attach", {"session_id": session_id, "path": path})

    async def attach_file(self, *, session_id, path, name=""):
        params = {"session_id": session_id, "path": path}
        if name:
            params["name"] = name
        return await self._rpc_dict("file.attach", params)

    async def resolve_command(self, *, name):
        return await self._rpc_dict("command.resolve", {"name": name})

    async def dispatch_command(self, *, session_id, name, arg=""):
        return await self._rpc_dict(
            "command.dispatch",
            {"session_id": session_id, "name": name, "arg": arg},
        )

    async def exec_slash(self, *, session_id, command):
        return await self._rpc_dict("slash.exec", {"session_id": session_id, "command": command})

    async def respond(self, *, method, session_id, payload):
        params = {"session_id": session_id}
        params.update(payload)
        await self.request(method, params)

    async def _rpc_dict(self, method, params):

        return ext_dict("%s result" % method, await self.request(method, params))

    async def request(self, method, params):
        if self.proc is None or self.proc.returncode is not None or self.proc.stdin is None:
            raise RuntimeError("Hermes gateway is not running")
        return await self._request_future(
            {"jsonrpc": "2.0", "method": method, "params": params},
        )

    def session_queue(self, session_id):
        return self._events.queue(session_id)

    async def _read_stderr_lines(self, *, label):
        proc = self.proc
        recent = []
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if not text:
                continue
            logger.info("%s: %s", label, text)
            recent.append(text)
            if len(recent) > 24:
                del recent[:-24]
            if self._usage_abort_signaled:
                continue
            if not stderr_usage_exhausted_abort(text, recent):
                continue
            self._usage_abort_signaled = True
            await self._events.broadcast(
                "error",
                {"message": "usage not enough", "reason": "usage_exhausted"},
            )

    async def _read_stdout(self):
        async def on_line(raw):
            frame = self._parse_json_line(raw, label="Hermes gateway")
            if frame is None:
                return
            try:
                await self._handle_stdout_frame(frame)
            except (TypeError, RuntimeError) as e:
                logger.warning("Hermes gateway frame rejected: %s raw=%s", e, raw[:500])

        await self._read_stdout_lines(on_line=on_line)
        self._ready.set()
        self._fail_pending("Hermes gateway stdout closed")

    async def _handle_stdout_frame(self, frame):

        frame = ext_dict("hermes frame", frame)

        frame_id = frame.get("id")
        if frame_id is not None:
            frame_id = ext_str("hermes frame id", frame_id)
            if frame_id in self._pending:
                if "error" in frame:
                    error = ext_dict("hermes frame error", frame["error"])
                    message = ext_str("hermes frame error.message", error.get("message"), default="request failed")
                    self._resolve_pending(frame_id, error=RuntimeError(message))
                else:
                    self._resolve_pending(frame_id, result=frame.get("result"))
                return

        if frame.get("method") != "event":
            return
        params = ext_dict("hermes event params", frame.get("params"))
        event_type = ext_str("hermes event type", params.get("type"))
        if not event_type:
            raise TypeError("hermes event type must be non-empty")
        session_id = ext_str("hermes event session_id", params.get("session_id"))
        payload = params.get("payload")
        if payload is None:
            payload = {}
        payload = ext_dict("hermes event payload", payload)
        event = SessionEvent(type=event_type, session_id=session_id, payload=payload)
        if event.type == "gateway.ready":
            self._ready.set()
        await self._events.put(event)
