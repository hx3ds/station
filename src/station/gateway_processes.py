import asyncio
import ctypes
import os
import signal

from station import logger

PR_SET_PDEATHSIG = 1

def install_parent_death_signal():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM)) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
    if os.getppid() == 1:
        os.kill(os.getpid(), signal.SIGTERM)

class GatewayProcessRegistry:
    def __init__(self):
        self._entries = {}

    def register(self, *, model_id, kind, proc):
        if proc is None or proc.pid is None:
            return
        self._entries[proc.pid] = {
            "model_id": str(model_id),
            "kind": str(kind),
            "proc": proc,
        }
        logger.info(
            "gateway registered kind=%s model_id=%s pid=%s",
            kind,
            model_id,
            proc.pid,
        )

    def unregister(self, proc):
        if proc is None or proc.pid is None:
            return
        self.unregister_pid(proc.pid)

    def unregister_pid(self, pid):
        if pid is None:
            return
        entry = self._entries.pop(pid, None)
        if entry is None:
            return
        logger.info(
            "gateway unregistered kind=%s model_id=%s pid=%s",
            entry["kind"],
            entry["model_id"],
            pid,
        )

    async def close_all(self, *, terminate_timeout_s=5.0):
        entries = list(self._entries.values())
        self._entries.clear()
        for entry in entries:
            proc = entry["proc"]
            if proc is None or proc.returncode is not None:
                continue
            pid = proc.pid
            logger.info(
                "gateway cleanup kind=%s model_id=%s pid=%s",
                entry["kind"],
                entry["model_id"],
                pid,
            )
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=terminate_timeout_s)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
