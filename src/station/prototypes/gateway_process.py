import asyncio
import contextlib
from dataclasses import dataclass

from station.gateway_processes import install_parent_death_signal

@dataclass(slots=True)
class SessionEvent:
    type: str
    session_id: str
    payload: dict

class SessionEventBus:
    def __init__(self):
        self._queues = {}

    def queue(self, session_id):
        queue = self._queues.get(session_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[session_id] = queue
        return queue

    async def put(self, event):
        if event.session_id:
            await self.queue(event.session_id).put(event)

    async def broadcast(self, event_type, payload=None):
        payload = dict(payload or {})
        for sid in list(self._queues):
            await self.queue(sid).put(SessionEvent(type=event_type, session_id=sid, payload=dict(payload)))

    def drop_queued(self, session_id):
        queue = self._queues.get(session_id)
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def clear(self):
        self._queues.clear()

async def close_subprocess(proc, tasks, *, terminate_timeout_s=5.0):
    if proc is not None and proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=terminate_timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
    for task in tasks:
        if task is None:
            continue
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

def gateway_subprocess_kwargs():
    return {"preexec_fn": install_parent_death_signal}
