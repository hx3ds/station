import asyncio
import os
import time

from station.conductor.pair.whatsapp import WhatsAppPairer
from station.conductor.platform_types import strip_qr_prefix
from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_str

class PairManager:
    def __init__(self, local_conductor):
        self.lc = local_conductor
        self._sessions = {}
        self._lock = asyncio.Lock()
        self._pairers = {
            "whatsapp": WhatsAppPairer(local_conductor),
        }

    def _key(self, acct_id, platform):
        return f"{strip_qr_prefix(platform)}:{(acct_id or '').strip()}"

    async def start(self, *, acct_id, platform, qr_timeout_ms=None):
        if acct_id is None:
            acct_id = ""
        else:
            acct_id = ext_str('acct_id', acct_id, strip=False)
        acct_id = acct_id.strip()
        platform = ext_str('platform', platform, strip=False)
        platform = strip_qr_prefix(platform)
        if not acct_id:
            raise ExternalError("acct_id is required")
        if not platform:
            raise ExternalError("platform is required")
        pairer = self._pairers.get(platform)
        if pairer is None:
            raise ExternalError("unsupported platform: %s" % platform)
        key = self._key(acct_id, platform)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing and existing.get("status") == "pending":
                await self._cancel_locked(key)
            state = {
                "acct_id": acct_id,
                "platform": platform,
                "status": "pending",
                "qr": "",
                "expires_at": None,
                "user_id": "",
                "error": "",
                "started_at": time.time(),
            }
            self._sessions[key] = state

            async def on_qr(qr, expires_at):
                state["qr"] = qr or ""
                state["expires_at"] = expires_at
                state["status"] = "pending"

            async def on_linked(user_id):
                async with self._lock:
                    if self._sessions.get(key) is not state:
                        return
                    state["user_id"] = (user_id or "").strip()
                    state["status"] = "linked"
                    state["qr"] = ""
                await self._notify_consul(
                    acct_id=acct_id,
                    status="linked",
                    user_id=state["user_id"],
                )

            async def on_failed(reason, status="failed"):
                async with self._lock:
                    if self._sessions.get(key) is not state:
                        return
                    state["status"] = status
                    state["error"] = reason or status
                    state["qr"] = ""
                await self._notify_consul(
                    acct_id=acct_id,
                    status=status,
                    user_id="",
                    error=state["error"],
                )

            handle = await pairer.start(
                acct_id=acct_id,
                qr_timeout_ms=qr_timeout_ms,
                on_qr=on_qr,
                on_linked=on_linked,
                on_failed=on_failed,
            )
            state["handle"] = handle

        deadline = time.time() + 45
        while time.time() < deadline:
            if state.get("qr"):
                break
            if state.get("status") in ("linked", "failed", "expired"):
                break
            await asyncio.sleep(0.1)
        status = state.get("status") or "pending"
        qr = state.get("qr") or ""
        if not qr and status != "linked":
            async with self._lock:
                if self._sessions.get(key) is state:
                    await self._cancel_locked(key)
            raise ExternalError(state.get("error") or "qr not ready (%s)" % status)
        return {
            "acct_id": acct_id,
            "platform": platform,
            "status": status,
            "qr": qr,
            "expires_at": state.get("expires_at"),
        }

    async def status(self, *, acct_id, platform):
        if acct_id is None:
            acct_id = ""
        else:
            acct_id = ext_str('acct_id', acct_id, strip=False)
        platform = ext_str('platform', platform, strip=False)
        key = self._key(acct_id, platform)
        async with self._lock:
            state = self._sessions.get(key) or {}
        return {
            "acct_id": acct_id.strip(),
            "platform": strip_qr_prefix(platform),
            "status": state.get("status") or "failed",
            "qr": state.get("qr") or "",
            "expires_at": state.get("expires_at"),
            "user_id": state.get("user_id") or "",
            "error": state.get("error") or "",
        }

    async def cancel(self, *, acct_id, platform):
        key = self._key(acct_id, platform)
        async with self._lock:
            await self._cancel_locked(key)
        return {"ok": True}

    async def _cancel_locked(self, key):
        state = self._sessions.pop(key, None)
        if not state:
            return
        handle = state.get("handle")
        if handle is not None:
            try:
                await handle.stop()
            except Exception as e:
                logger.error("unexpected where=pair_cancel key=%s error=%s", key, e, exc_info=e)

    async def _notify_consul(self, *, acct_id, status, user_id="", error=""):
        payload = {
            "acct_id": acct_id,
            "conductor_address": self.lc.conductor_address(),
            "status": status,
            "user_id": user_id,
            "ts_ms": int(time.time() * 1000),
            "last_error_kind": ("pair_" + status) if error else "",
            "last_error_message": (error[:1024] if error else ""),
        }
        body = await self.lc._consul_post_full(
            path="/api/local_conductor/account_link/complete",
            payload=payload,
        )
        if not body or body.get("result") != 0:
            logger.error(
                "account_link/complete failed acct_id=%s status=%s",
                acct_id,
                status,
            )

    def session_dir(self, *, platform, acct_id):
        base = os.environ.get("LOCAL_CONDUCTOR_WHATSAPP_SESSION_DIR") or ""
        if not base:
            db_path = self.lc.config.database.path or "."
            parent = db_path if os.path.isdir(db_path) else os.path.dirname(os.path.abspath(db_path)) or "."
            base = os.path.join(parent, "whatsapp_sessions")
        path = os.path.join(base, strip_qr_prefix(platform), (acct_id or "").strip())
        os.makedirs(path, exist_ok=True)
        return path
