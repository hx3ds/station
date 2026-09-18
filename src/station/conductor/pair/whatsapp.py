import asyncio
import json

from station.conductor.util import node_bin, whatsapp_bridge_dir
from station import logger
from station.prototypes.boundary import ext_bool, ext_dict, ext_float, ext_int, ext_list, ext_require, ext_str

class WhatsAppPairHandle:
    def __init__(self, proc):
        self.proc = proc
        self._stopped = False

    async def stop(self):
        self._stopped = True
        if self.proc is None or self.proc.returncode is not None:
            return
        try:
            self.proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass

class WhatsAppPairer:
    def __init__(self, local_conductor):
        self.lc = local_conductor

    async def start(self, *, acct_id, qr_timeout_ms, on_qr, on_linked, on_failed):
        session_dir = self.lc.pair_manager.session_dir(platform="whatsapp", acct_id=acct_id)
        bridge_dir = whatsapp_bridge_dir(need_pair=True)
        pair_js = bridge_dir / "pair.js"
        if not pair_js.is_file():
            await on_failed(f"whatsapp bridge missing: {pair_js}", status="failed")
            return WhatsAppPairHandle(None)
        cmd = [
            node_bin(),
            str(pair_js),
            "--session",
            session_dir,
            "--acct-id",
            acct_id,
        ]
        if qr_timeout_ms is not None:
            cmd.extend(["--qr-timeout-ms", str(int(qr_timeout_ms))])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(bridge_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        handle = WhatsAppPairHandle(proc)

        async def _drain_stderr():
            chunks = []
            if proc.stderr is None:
                return ""
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                chunks.append(line)
                if sum(len(c) for c in chunks) > 8000:
                    chunks = chunks[-20:]
            return b"".join(chunks).decode("utf-8", errors="replace")[:500]

        stderr_task = asyncio.create_task(_drain_stderr())

        async def _reader():
            finished = False
            try:
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        logger.warning("whatsapp pair non-json acct_id=%s", acct_id)
                        continue
                    try:
                        event = ext_dict('whatsapp pair event', event)
                        kind = event.get("event")
                        if kind is None:
                            continue
                        kind = ext_str('whatsapp pair event kind', kind, strip=False)
                        kind = kind.strip()
                        if kind == "qr":
                            qr = event.get("qr")
                            if qr is None:
                                qr = ""
                            else:
                                qr = ext_str('whatsapp pair qr', qr, strip=False)
                            await on_qr(qr, event.get("expires_at"))
                        elif kind == "linked":
                            finished = True
                            user_id = event.get("user_id")
                            if user_id is None:
                                user_id = ""
                            else:
                                user_id = ext_str('whatsapp pair user_id', user_id, strip=False)
                            await on_linked(user_id)
                            break
                        elif kind in ("failed", "expired"):
                            finished = True
                            reason = event.get("reason")
                            if reason is None:
                                reason = kind
                            else:
                                reason = ext_str('whatsapp pair reason', reason, strip=False)
                            await on_failed(reason, status=kind)
                            break
                    except TypeError as exc:
                        logger.error(
                            "whatsapp pair bad event acct_id=%s error=%s",
                            acct_id,
                            exc,
                        )
                        continue
                code = await proc.wait()
                err = ""
                try:
                    err = await asyncio.wait_for(asyncio.shield(stderr_task), timeout=1)
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                if err:
                    logger.warning("whatsapp pair stderr acct_id=%s", acct_id)
                # Intentional stop() must not mark the account outdated.
                if not finished and not handle._stopped and code not in (0, None):
                    await on_failed(err or f"bridge exit {code}", status="failed")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("unexpected where=whatsapp_pair_reader acct_id=%s error=%s", acct_id, exc, exc_info=exc)
                if not handle._stopped:
                    await on_failed(str(exc), status="failed")

        asyncio.create_task(_reader())
        return handle
