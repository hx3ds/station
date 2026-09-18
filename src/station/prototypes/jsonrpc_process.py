import asyncio
import json

from station import logger
from station.prototypes.gateway_process import close_subprocess

class JsonLineRpcProcess:
    def __init__(self, *, request_id_prefix, stop_error):
        self.proc = None
        self._next_request_id = 0
        self._pending = {}
        self._stdout_task = None
        self._stderr_task = None
        self._write_lock = asyncio.Lock()
        self._request_id_prefix = request_id_prefix
        self._stop_error = stop_error
        self._line_buf = b""

    async def close(self):
        proc = self.proc
        self.proc = None
        tasks = [self._stdout_task, self._stderr_task]
        self._stdout_task = None
        self._stderr_task = None
        await close_subprocess(proc, tasks, terminate_timeout_s=5.0)
        self._fail_pending(self._stop_error)

    def _new_request_id(self):
        self._next_request_id += 1
        return "%s%d" % (self._request_id_prefix, self._next_request_id)

    async def _write_line(self, message):
        if self.proc is None or self.proc.returncode is not None or self.proc.stdin is None:
            raise RuntimeError(self._stop_error)
        line = json.dumps(message, ensure_ascii=True)
        async with self._write_lock:
            self.proc.stdin.write((line + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def _request_future(self, message, *, timeout_s=None):
        if self.proc is None or self.proc.returncode is not None or self.proc.stdin is None:
            raise RuntimeError(self._stop_error)
        request_id = self._new_request_id()
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = dict(message)
        payload["id"] = request_id
        await self._write_line(payload)
        try:
            if timeout_s is None:
                return await future
            return await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(request_id, None)

    def _resolve_pending(self, request_id, *, result=None, error=None):
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        if error is not None:
            if not isinstance(error, BaseException):
                raise TypeError("error must be BaseException")
            future.set_exception(error)
        else:
            future.set_result(result)
        return True

    def _fail_pending(self, message):
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(RuntimeError(message))
        self._pending.clear()

    async def _read_stdout_lines(self, *, on_line, buffered=False):
        proc = self.proc
        if buffered:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                self._line_buf += chunk
                while True:
                    idx = self._line_buf.find(b"\n")
                    if idx < 0:
                        break
                    raw_line = self._line_buf[:idx]
                    self._line_buf = self._line_buf[idx + 1 :]
                    if raw_line.endswith(b"\r"):
                        raw_line = raw_line[:-1]
                    if not raw_line:
                        continue
                    await on_line(raw_line.decode("utf-8", "replace"))
        else:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                raw = line.decode("utf-8", "replace").strip()
                if raw:
                    await on_line(raw)

    async def _read_stderr_lines(self, *, label):
        proc = self.proc
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                logger.info("%s: %s", label, text)

    def _parse_json_line(self, text, *, label):

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("%s malformed JSON: %s", label, text[:500])
            return None
