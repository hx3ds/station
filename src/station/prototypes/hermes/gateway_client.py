import asyncio
import os
from pathlib import Path

from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_str
from station.prototypes.gateway_process import SessionEvent, SessionEventBus, gateway_subprocess_kwargs
from station.prototypes.jsonrpc_process import JsonLineRpcProcess


def hermes_child_env(settings, hermes_home, env=None):
    merged = settings.apply_process_env(env or os.environ)
    hermes_home = os.path.abspath(str(hermes_home))
    os.makedirs(hermes_home, exist_ok=True)
    merged["HERMES_HOME"] = hermes_home
    merged["HERMES_PYTHON_SRC_ROOT"] = str(settings.hermes_root)
    py_path = merged.get("PYTHONPATH", "").strip()
    root = str(settings.hermes_root)
    merged["PYTHONPATH"] = "%s%s%s" % (root, os.pathsep, py_path) if py_path else root
    return merged


class HermesGatewayProcess(JsonLineRpcProcess):
    def __init__(self, *, settings, hermes_home):
        super().__init__(request_id_prefix="hb", stop_error="Hermes gateway stopped")
        self.settings = settings
        self.hermes_home = hermes_home
        self._events = SessionEventBus()
        self._ready = asyncio.Event()

    async def start(self, *, timeout_s=20.0):
        if self.proc is not None and self.proc.returncode is None:
            return

        env = hermes_child_env(self.settings, self.hermes_home)
        toolsets = self.settings.tui_toolsets()
        if toolsets:
            env["HERMES_TUI_TOOLSETS"] = toolsets
        else:
            env.pop("HERMES_TUI_TOOLSETS", None)
        workspace = os.path.abspath(str(Path(self.settings.workspace_dir).expanduser()))
        os.makedirs(workspace, exist_ok=True)

        self.proc = await asyncio.create_subprocess_exec(
            self.settings.hermes_python,
            "-m",
            "tui_gateway.entry",
            cwd=workspace,
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

    async def session_create(self):
        params = {"cwd": self.settings.workspace_dir, "source": "station"}
        provider = self.settings.gateway_provider()
        model = self.settings.model
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

        result = ext_dict("session.create result", await self.request("session.create", params))
        session_id = ext_str("session.create session_id", result.get("session_id"))
        if not session_id:
            raise RuntimeError("Hermes session.create did not return a session_id")
        return session_id

    async def submit_prompt(self, *, session_id, text):
        self._events.queue(session_id)
        self._events.drop_queued(session_id)
        await self.request("prompt.submit", {"session_id": session_id, "text": text})

    async def respond(self, *, method, session_id, payload):
        params = {"session_id": session_id}
        params.update(payload)
        await self.request(method, params)

    async def request(self, method, params, *, timeout_s=None):
        if self.proc is None or self.proc.returncode is not None or self.proc.stdin is None:
            raise RuntimeError("Hermes gateway is not running")
        return await self._request_future(
            {"jsonrpc": "2.0", "method": method, "params": params},
            timeout_s=timeout_s,
        )

    def session_queue(self, session_id):
        return self._events.queue(session_id)

    async def _read_stdout(self):
        async def on_frame(frame):
            try:
                await self._handle_stdout_frame(frame)
            except (TypeError, RuntimeError) as e:
                logger.warning("Hermes gateway frame rejected: %s raw=%s", e, frame)

        def on_close():
            self._ready.set()
            self._fail_pending("Hermes gateway stdout closed")

        await self._pump_stdout(label="Hermes gateway", on_frame=on_frame, on_close=on_close)

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
            raise ExternalError("hermes event type must be non-empty")
        session_id = ext_str("hermes event session_id", params.get("session_id"))
        payload = params.get("payload")
        if payload is None:
            payload = {}
        payload = ext_dict("hermes event payload", payload)
        event = SessionEvent(type=event_type, session_id=session_id, payload=payload)
        if event.type == "gateway.ready":
            self._ready.set()
        await self._events.put(event)
