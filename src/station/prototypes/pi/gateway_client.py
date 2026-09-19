import asyncio
import os
from pathlib import Path

from station import logger
from station.prototypes.boundary import ext_dict, ext_str
from station.prototypes.fs_paths import sanitize_path_component
from station.prototypes.gateway_process import gateway_subprocess_kwargs
from station.prototypes.jsonrpc_process import JsonLineRpcProcess

class PiRpcProcess(JsonLineRpcProcess):
    def __init__(self, *, settings, acct_id, chat_id):
        super().__init__(request_id_prefix="pi", stop_error="Pi RPC process stopped")
        self.settings = settings
        self.acct_id = acct_id
        self.chat_id = chat_id
        self.session_dir = (
            settings.session_root
            / sanitize_path_component(acct_id)
            / sanitize_path_component(chat_id)
        )
        self._events = asyncio.Queue()

    async def start(self):
        if self.proc is not None and self.proc.returncode is None:
            return

        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.settings.agent_home.mkdir(parents=True, exist_ok=True)
        Path(self.settings.workspace_dir).expanduser().mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env.update(self.settings.extra_env)
        env.setdefault("PI_CODING_AGENT_DIR", str(self.settings.agent_home))
        env.setdefault("PI_CODING_AGENT_SESSION_DIR", str(self.session_dir))

        command = [
            *self.settings.command,
            "--mode",
            "rpc",
            "--name",
            "station:%s:%s" % (self.acct_id, self.chat_id),
            *self.settings.rpc_args,
        ]
        if self.settings.no_session:
            command.append("--no-session")
        else:
            command.extend(["--session-dir", str(self.session_dir)])
        if self.settings.provider:
            command.extend(["--provider", self.settings.provider])
        if self.settings.model:
            command.extend(["--model", self.settings.model])

        self.proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(self.settings.command_cwd),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **gateway_subprocess_kwargs(),
        )
        self._stdout_task = asyncio.create_task(self._read_stdout(), name="pi-rpc-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr_lines(label="pi rpc stderr"), name="pi-rpc-stderr")
        await asyncio.sleep(0.15)
        if self.proc.returncode is not None:
            raise RuntimeError("Pi RPC process exited early with code %s" % self.proc.returncode)
        if self.settings.thinking_level:
            await self.set_thinking_level(self.settings.thinking_level)
        logger.info(
            "Pi RPC started acct_id=%s chat_id=%s command=%s workspace=%s session_dir=%s",
            self.acct_id,
            self.chat_id,
            command,
            self.settings.workspace_dir,
            self.session_dir,
        )

    async def close(self):
        await super().close()
        while not self._events.empty():
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                break

    def event_queue(self):
        return self._events

    async def prompt(self, message, *, images=None, streaming_behavior=None):
        body = {"type": "prompt", "message": message}
        if images:
            body["images"] = images
        if streaming_behavior:
            body["streamingBehavior"] = streaming_behavior
        await self._command(body)

    async def steer(self, message, *, images=None):
        body = {"type": "steer", "message": message}
        if images:
            body["images"] = images
        await self._command(body)

    async def get_last_assistant_text(self):
        data = await self._command({"type": "get_last_assistant_text"})
        if data is None:
            return ""
        data = ext_dict("pi get_last_assistant_text data", data)
        text = data.get("text")
        if text is None:
            return ""
        return ext_str("pi last assistant text", text, strip=False)

    async def set_thinking_level(self, level):
        await self._command({"type": "set_thinking_level", "level": level})

    async def _command(self, body):

        response = ext_dict("Pi RPC response", await self._request_future(body))
        if not response.get("success"):
            error = response.get("error")
            if error is None:
                error = response.get("message")
            if error is None:
                error = "command failed"
            raise RuntimeError("Pi RPC %s failed: %s" % (body.get("type"), error))
        return response.get("data")

    async def _read_stdout(self):
        async def on_frame(frame):
            await self._handle_frame(ext_dict("Pi RPC frame", frame))

        await self._pump_stdout(label="Pi RPC", on_frame=on_frame)

    async def _handle_frame(self, frame):
        if ext_str("Pi RPC frame type", frame.get("type")) == "response":
            request_id = ext_str("Pi RPC frame id", frame.get("id"))
            if request_id:
                self._resolve_pending(request_id, result=frame)
            return
        await self._events.put(frame)

class PiGatewayProcess:
    def __init__(self, *, settings):
        self.settings = settings
        self.proc = None
        self._chats = {}
        self._lock = asyncio.Lock()

    async def start(self):
        Path(self.settings.workspace_dir).expanduser().mkdir(parents=True, exist_ok=True)
        self.settings.session_root.mkdir(parents=True, exist_ok=True)
        self.settings.agent_home.mkdir(parents=True, exist_ok=True)

    async def close(self):
        async with self._lock:
            processes = list(self._chats.values())
            self._chats.clear()
            self.proc = None
        for process in processes:
            await process.close()

    async def ensure_chat(self, *, acct_id, chat_id):
        key = "%s:%s" % (acct_id, chat_id)
        async with self._lock:
            process = self._chats.get(key)
            if process is not None and process.proc is not None and process.proc.returncode is None:
                return process
            if process is not None:
                await process.close()
            process = PiRpcProcess(settings=self.settings, acct_id=acct_id, chat_id=chat_id)
            await process.start()
            self._chats[key] = process
            if self.proc is None:
                self.proc = process.proc
            return process

    def get_chat(self, *, acct_id, chat_id):
        return self._chats.get("%s:%s" % (acct_id, chat_id))
