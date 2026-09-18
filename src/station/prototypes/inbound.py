import asyncio
import uuid
from collections import deque
from dataclasses import dataclass

from station import logger
from station.prototypes.boundary import ext_dict, ext_mapping_get, ext_require, ext_str, validate_inbound_data

@dataclass(slots=True)
class InboundMessageContext:
    data: dict
    fields: dict
    attachments: list
    model_id: str
    model_settings: dict
    chat_id: str
    acct_id: str
    request_id: str | None

    @property
    def text(self):
        return self.fields["inbound_text"]

    @property
    def raw_text(self):
        return self.fields["text"]

    @property
    def caption(self):
        return self.fields["caption"]

    @property
    def reply_to(self):
        return self.fields["msg_id"]

    @property
    def platform(self):
        return self.fields["platform"]

    @property
    def chat_type(self):
        return self.fields["chat_type"]

_INBOUND_RETRY_BASE_SECONDS = 0.25
_INBOUND_RETRY_MAX_SECONDS = 30.0
_INBOUND_RETRY_MAX_ATTEMPTS = 32
_QUEUE_FULL_REPLY = "The message queue is full. Please try again later."

class PrototypeInbound:
    def _inbound_bind(self):
        server = self.app["config"].server
        self._inbound_workers = []
        self._started = False
        self._start_lock = asyncio.Lock()
        self._start_task = None
        self._worker_count = server.inbound_workers
        self._queue_size = server.inbound_queue
        self._batch_size = server.inbound_batch
        self._at_least_once = server.inbound_at_least_once
        self._inflight_job_ids = set()
        self._chat_queues = {}
        self._ready_chats = deque()
        self._ready_set = set()
        self._pending_count = 0
        self._sched_wakeup = None

    def _db(self):
        return self.app["db"]

    def _chat_key(self, chat_id, acct_id):
        acct = "" if acct_id is None else acct_id
        chat = "" if chat_id is None else chat_id
        return (acct, chat)

    def _job_from_parts(self, *, kind, data, model_id, model_settings, chat_id, acct_id, request_id, job_id=None, attempts=0):
        return {
            "job_id": job_id,
            "kind": kind,
            "data": data,
            "model_id": model_id,
            "model_settings": model_settings,
            "chat_id": chat_id,
            "acct_id": acct_id,
            "request_id": request_id,
            "attempts": attempts if attempts is not None else 0,
        }

    def _job_from_pending_row(self, row):
        payload = ext_dict("inbound_pending payload", row["payload"])
        data = ext_dict("inbound_pending data", payload["data"])
        model_settings = ext_dict("inbound_pending model_settings", payload["model_settings"])
        kind = row.get("kind")
        if kind is None:
            kind = payload.get("kind")
        if kind is None:
            kind = "message"
        else:
            kind = ext_require("inbound_pending kind", kind, (str,))
        model_id = row.get("model_id")
        if model_id is None:
            model_id = payload.get("model_id")
        if model_id is None:
            model_id = self.model_id
        attempts = ext_mapping_get(payload, "attempts", (int,), 0)
        return self._job_from_parts(
            kind=kind,
            data=data,
            model_id=model_id,
            model_settings=model_settings,
            chat_id=payload.get("chat_id"),
            acct_id=payload.get("acct_id"),
            request_id=payload.get("request_id"),
            job_id=row.get("job_id"),
            attempts=attempts,
        )

    async def _persist_inbound_job(self, job):
        db = self._db()
        job_id = job.get("job_id")
        if job_id is None or job_id == "":
            job_id = uuid.uuid4().hex
        job["job_id"] = job_id
        attempts = job.get("attempts")
        if attempts is None:
            attempts = 0
        payload = {
            "kind": job["kind"],
            "data": job["data"],
            "model_id": job["model_id"],
            "model_settings": job["model_settings"],
            "chat_id": job["chat_id"],
            "acct_id": job["acct_id"],
            "request_id": job["request_id"],
            "attempts": attempts,
        }
        model_id = job["model_id"]
        if model_id is None:
            model_id = self.model_id
        kind = job["kind"]
        if kind is None:
            kind = "message"
        await db.put_inbound_pending(
            job_id=job_id,
            model_id=model_id,
            kind=kind,
            payload=payload,
        )
        return job_id

    async def _complete_inbound_job(self, job_id):
        if not job_id or not self._at_least_once:
            return
        await self._db().delete_inbound_pending(job_id)

    async def _reply_queue_full(self, data, *, chat_id, acct_id, request_id):
        if not chat_id or not self.client_context:
            return
        await self.send_outbound(
            text=_QUEUE_FULL_REPLY,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
            platform=(data.get("platform") or ""),
            chat_type=(data.get("chat_type") or ""),
        )

    def _offer_job(self, job, *, force=False):
        if not self._started:
            return False
        if not force and self._pending_count >= self._queue_size:
            return False
        key = self._chat_key(job["chat_id"], job["acct_id"])
        q = self._chat_queues.get(key)
        if q is None:
            q = deque()
            self._chat_queues[key] = q
        q.append(job)
        self._pending_count += 1
        if key not in self._ready_set:
            self._ready_set.add(key)
            self._ready_chats.append(key)
        self._sched_wakeup.set()
        return True

    async def _claim_batch(self):
        while True:
            if self._ready_chats:
                key = self._ready_chats.popleft()
                self._ready_set.discard(key)
                q = self._chat_queues.get(key)
                if not q:
                    self._chat_queues.pop(key, None)
                    continue
                batch = []
                while q and len(batch) < self._batch_size:
                    batch.append(q.popleft())
                    self._pending_count -= 1
                if q:
                    self._ready_set.add(key)
                    self._ready_chats.append(key)
                else:
                    self._chat_queues.pop(key, None)
                return batch
            self._sched_wakeup.clear()
            if self._ready_chats:
                continue
            await self._sched_wakeup.wait()

    async def _enqueue_inbound(self, kind, data, model_id, model_settings, *, chat_id=None, acct_id=None, request_id=None):
        job = self._job_from_parts(
            kind=kind,
            data=data,
            model_id=model_id,
            model_settings=model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )

        if self._at_least_once:
            if self._started and self._pending_count >= self._queue_size:
                await self._reply_queue_full(data, chat_id=chat_id, acct_id=acct_id, request_id=request_id)
                return False
            await self._persist_inbound_job(job)
            if not self._started:
                return True
            self._offer_job(job, force=True)
            return True

        if not self._started:
            return False
        if not self._offer_job(job):
            await self._reply_queue_full(data, chat_id=chat_id, acct_id=acct_id, request_id=request_id)
            return False
        return True

    async def _requeue_inbound_job(self, job):
        if not self._started:
            return
        attempts = job.get("attempts")
        if attempts is None:
            attempts = 0
        attempts += 1
        job["attempts"] = attempts
        if self._at_least_once:
            await self._persist_inbound_job(job)
        if attempts > _INBOUND_RETRY_MAX_ATTEMPTS:
            logger.error(
                "inbound job exceeded retry budget model_id=%s job_id=%s attempts=%s",
                self.model_id,
                job.get("job_id"),
                attempts,
            )
            return
        delay = min(_INBOUND_RETRY_BASE_SECONDS * (2 ** max(attempts - 1, 0)), _INBOUND_RETRY_MAX_SECONDS)
        await asyncio.sleep(delay)
        if not self._started:
            return
        self._offer_job(job, force=True)

    async def _run_inbound_job(self, job):
        kind = job["kind"]
        data = job["data"]
        model_id = job["model_id"]
        model_settings = job["model_settings"]
        chat_id = job["chat_id"]
        acct_id = job["acct_id"]
        request_id = job["request_id"]
        if kind == "command":
            await self._handle_command_impl(
                data,
                model_id,
                model_settings,
                chat_id=chat_id,
                acct_id=acct_id,
                request_id=request_id,
            )
            return
        await self._handle_message_impl(
            data,
            model_id,
            model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )

    async def _handle_inbound_batch(self, batch):
        await self._handle_message_batch_impl(batch)

    async def _handle_message_batch_impl(self, batch):
        for job in batch:
            await self._run_inbound_job(job)

    async def _inbound_worker(self):
        while True:
            batch = await self._claim_batch()
            inflight = []
            try:
                for job in batch:
                    job_id = job.get("job_id")
                    if job_id == "":
                        job_id = None
                    if job_id:
                        self._inflight_job_ids.add(job_id)
                        inflight.append(job_id)
                await self._handle_inbound_batch(batch)
                for job in batch:
                    job_id = job.get("job_id")
                    if job_id == "":
                        job_id = None
                    await self._complete_inbound_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "unexpected where=inbound_worker model_id=%s batch=%s error=%s",
                    self.model_id,
                    len(batch),
                    e,
                    exc_info=e,
                )
                if self._at_least_once:
                    for job in batch:
                        await self._requeue_inbound_job(job)
            finally:
                for job_id in inflight:
                    self._inflight_job_ids.discard(job_id)

    async def _recover_inbound_pending(self):
        if not self._at_least_once or not self._started:
            return
        rows = await self._db().list_inbound_pending(self.model_id)
        recovered = 0
        for row in rows:
            job_id = row.get("job_id")
            if job_id is None or job_id == "":
                continue
            if job_id in self._inflight_job_ids:
                continue
            job = self._job_from_pending_row(row)
            self._offer_job(job, force=True)
            recovered += 1
        if recovered:
            logger.info("recovered inbound_pending model_id=%s count=%s", self.model_id, recovered)

    async def handle_message(self, data, model_id, model_settings, chat_id=None, acct_id=None, request_id=None):
        data = validate_inbound_data(data, label="message data")
        model_settings = ext_dict("model_settings", model_settings if model_settings is not None else {})
        return await self._enqueue_inbound(
            "message",
            data,
            model_id,
            model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )

    async def handle_command(self, data, model_id, model_settings, chat_id=None, acct_id=None, request_id=None):
        data = validate_inbound_data(data, label="command data")
        model_settings = ext_dict("model_settings", model_settings if model_settings is not None else {})
        return await self._enqueue_inbound(
            "command",
            data,
            model_id,
            model_settings,
            chat_id=chat_id,
            acct_id=acct_id,
            request_id=request_id,
        )

    async def start(self):
        async with self._start_lock:
            if self._started:
                return None
            task = self._start_task
            if task is None or task.done():
                task = asyncio.create_task(
                    self._start_body(),
                    name=f"prototype-start-{self.model_id}",
                )
                self._start_task = task
        try:
            return await task
        except Exception:
            async with self._start_lock:
                if self._start_task is task and task.done():
                    self._start_task = None
            raise

    async def _start_body(self):
        if self._started:
            return None
        self._chat_queues = {}
        self._ready_chats = deque()
        self._ready_set = set()
        self._pending_count = 0
        self._sched_wakeup = asyncio.Event()
        self._inbound_workers = [
            asyncio.create_task(self._inbound_worker(), name=f"inbound-{self.model_id}-{i}")
            for i in range(self._worker_count)
        ]
        self._started = True
        logger.debug(
            "inbound queue started model_id=%s workers=%s queue=%s batch=%s at_least_once=%s",
            self.model_id,
            self._worker_count,
            self._queue_size,
            self._batch_size,
            self._at_least_once,
        )
        await self._recover_inbound_pending()
        return None

    async def restart(self):
        await self.stop()
        await self.start()

    async def stop(self):
        async with self._start_lock:
            task = self._start_task
            self._start_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._started = False
        workers = self._inbound_workers
        self._inbound_workers = []
        if self._sched_wakeup is not None:
            self._sched_wakeup.set()
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._chat_queues = {}
        self._ready_chats = deque()
        self._ready_set = set()
        self._pending_count = 0
        self._sched_wakeup = None
        self._inflight_job_ids.clear()
        return None
