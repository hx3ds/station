import json
import hashlib
import time
import asyncio
import concurrent.futures

from station.client.consul import fetch_model
from station.conductor.platform_types import strip_qr_prefix, validate_local_platform_type
from station.database import AsyncDatabase
from station.database.request_dedupe import shared_dedupe_cache
from station.database.file_registry import (
    FILE_COLUMNS,
    FILE_UPDATE_ALLOWED,
    FILES_INDEXES,
    FILES_TABLE_SQL_POSTGRES,
    row_to_file_dict,
)
from station.errors import ExternalError, InternalError
from station import logger

class PostgresDatabase(AsyncDatabase):
    def __init__(self, *, dsn: str, pool, lock_conn, enable_local_conductor: bool = False):
        self.dsn = dsn
        self.pool = pool

        self._lock_conn = lock_conn
        self._lock_mu = asyncio.Lock()
        self.enable_local_conductor = enable_local_conductor

    @classmethod
    async def create(cls, *, dsn: str, enable_local_conductor: bool = False) -> "PostgresDatabase":
        try:
            import asyncpg
        except ImportError as e:
            raise RuntimeError(
                "PostgreSQL backend requires asyncpg. Install it (e.g. pip install asyncpg)."
            ) from e

        pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=20)
        lock_conn = await asyncpg.connect(dsn=dsn)
        self = cls(
            dsn=dsn,
            pool=pool,
            lock_conn=lock_conn,
            enable_local_conductor=enable_local_conductor,
        )
        await self._init_db()
        return self

    def _lock_session_ok(self) -> bool:
        conn = self._lock_conn
        if conn is None:
            return False
        return not conn.is_closed()

    def _model_lock_key(self, model_id: str) -> int:
        if not model_id:
            raise InternalError("model_id is required for model lock")
        digest = hashlib.blake2b(model_id.encode("utf-8"), digest_size=8, person=b"station_model").digest()
        return int.from_bytes(digest, byteorder="big", signed=True)

    def _validate_runtime_state_identifiers(
        self,
        *,
        acct_id: str,
        acct_type: str | None = None,
        state_key: str | None = None,
    ) -> tuple[str, str | None, str | None]:
        if not acct_id:
            raise ExternalError("acct_id is required")
        normalized_type = None
        if acct_type is not None:
            normalized_type = validate_local_platform_type(acct_type, field_name="acct_type")
        normalized_key = None
        if state_key is not None:
            if not state_key:
                raise ExternalError("state_key is required")
            normalized_key = validate_local_platform_type(state_key, field_name="state_key")
        return acct_id, normalized_type, normalized_key

    async def acquire_model_lock(self, model_id: str):

        if not self._lock_session_ok():
            return None
        key = self._model_lock_key(model_id)
        async with self._lock_mu:
            locked = await self._lock_conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
            if not locked:
                return None
            return True

    async def release_model_lock(self, model_id: str, _token=None) -> None:
        if not self._lock_session_ok():
            return
        key = self._model_lock_key(model_id)
        async with self._lock_mu:
            await self._lock_conn.execute("SELECT pg_advisory_unlock($1)", key)

    async def ensure_model_lock(self, app, model_id: str) -> bool:
        if not model_id:
            return False

        locks = app.setdefault("model_locks", {})
        if model_id in locks:
            if self._lock_session_ok():
                return True
            locks.pop(model_id, None)

        token = await self.acquire_model_lock(model_id)
        if token is None:
            return False
        locks[model_id] = True
        return True

    async def try_model_lock_once(self, app, model_id: str) -> bool:
        if not model_id:
            return False
        token = await self.acquire_model_lock(model_id)
        if token is None:
            return False
        try:
            await self.release_model_lock(model_id, token)
        except Exception as e:
            logger.error("unexpected where=model lock release model_id=%s error=%s", model_id, e, exc_info=e)
        return True

    async def release_model_lock_from_app(self, app, model_id: str) -> None:
        if not model_id:
            return
        locks = app.get("model_locks") or {}
        if locks.pop(model_id, None) is None:
            return
        try:
            await self.release_model_lock(model_id)
        except Exception as e:
            logger.error("unexpected where=model lock release model_id=%s error=%s", model_id, e, exc_info=e)

    async def release_all_model_locks(self, app) -> None:
        locks = app.get("model_locks") or {}
        for model_id in list(locks.keys()):
            try:
                await self.release_model_lock(model_id)
            except Exception as e:
                logger.error("unexpected where=model lock release model_id=%s error=%s", model_id, e, exc_info=e)
        locks.clear()

    async def _init_db(self) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS models (
                        model_id TEXT PRIMARY KEY,
                        prototype_id INTEGER,
                        conductor_addr TEXT,
                        settings TEXT,
                        version INTEGER,
                        account_id TEXT,
                        accts TEXT,
                        created_at BIGINT
                    )
                    """
                )
                await conn.execute("DROP TABLE IF EXISTS requests CASCADE")
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inbound_pending (
                        job_id TEXT PRIMARY KEY,
                        model_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at DOUBLE PRECISION NOT NULL
                    )
                    """
                )
                await conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS prototypes (
                        prototype_id INTEGER PRIMARY KEY,
                        name TEXT,
                        token TEXT,
                        access_point TEXT,
                        type TEXT,
                        ava BOOLEAN,
                        version INTEGER,
                        raw_data TEXT,
                        created_at BIGINT
                    )
                    """
                )
                await conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_pending_model_id ON inbound_pending(model_id)")
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_inbound_pending_created_at ON inbound_pending(created_at)"
                )

                await conn.execute(FILES_TABLE_SQL_POSTGRES)
                for stmt in FILES_INDEXES:
                    await conn.execute(stmt)

                if self.enable_local_conductor:
                    await conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS local_accounts (
                            acct_id TEXT PRIMARY KEY,
                            model_id TEXT,
                            prototype_id INTEGER,
                            acct_type TEXT,
                            username TEXT,
                            server TEXT,
                            encrypted_token TEXT,
                            is_local BOOLEAN,
                            created_at BIGINT
                        )
                        """
                    )
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_local_accounts_model_id ON local_accounts(model_id)")
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_local_accounts_type ON local_accounts(acct_type)")
                    await conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS local_chats (
                            acct_id TEXT,
                            chat_id TEXT,
                            model_id TEXT,
                            prototype_id INTEGER,
                            chat_type TEXT,
                            carrier_user_id TEXT,
                            created_at BIGINT,
                            PRIMARY KEY (acct_id, chat_id)
                        )
                        """
                    )
                    await conn.execute("CREATE INDEX IF NOT EXISTS idx_local_chats_model_id ON local_chats(model_id)")
                    await conn.execute(
                        """
                        CREATE TABLE IF NOT EXISTS local_account_runtime_state (
                            acct_id TEXT,
                            acct_type TEXT,
                            state_key TEXT,
                            state_json TEXT,
                            updated_at BIGINT,
                            PRIMARY KEY (acct_id, acct_type, state_key)
                        )
                        """
                    )
                    await conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_local_account_runtime_state_acct_id ON local_account_runtime_state(acct_id)"
                    )

    async def wipe_all(self) -> None:
        tables = (
            "local_account_runtime_state",
            "local_chats",
            "local_accounts",
            "files",
            "prototypes",
            "inbound_pending",
            "requests",
            "models",
        )
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for table in tables:
                    await conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
        logger.info("database wiped backend=postgres")

    async def put_model(self, model_id: str, data: dict) -> None:
        conductor_addr = data.get("conductor_address") or ""
        prototype_id = data.get("prototype_id", 0)
        settings_obj = data.get("settings")
        if settings_obj is None:
            settings_obj = {}
        version = data.get("version", 1)
        account_id = data.get("account_id")
        if account_id is None:
            account_id = ""
        accts_obj = data.get("accts")
        if accts_obj is None:
            accts_obj = []

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO models
                (model_id, prototype_id, conductor_addr, settings, version, account_id, accts, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (model_id) DO UPDATE SET
                    prototype_id = EXCLUDED.prototype_id,
                    conductor_addr = EXCLUDED.conductor_addr,
                    settings = EXCLUDED.settings,
                    version = EXCLUDED.version,
                    account_id = EXCLUDED.account_id,
                    accts = EXCLUDED.accts,
                    created_at = EXCLUDED.created_at
                """,
                model_id,
                prototype_id,
                conductor_addr,
                json.dumps(settings_obj),
                version,
                account_id,
                json.dumps(accts_obj),
                int(time.time()),
            )

    async def get_model(self, model_id: str) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT model_id, prototype_id, conductor_addr, settings, version, account_id, accts, created_at
                FROM models
                WHERE model_id = $1
                """,
                model_id,
            )
        if not row:
            return None
        settings = json.loads(row["settings"]) if row["settings"] else {}
        accts = json.loads(row["accts"]) if row["accts"] else []
        return {
            "model_id": row["model_id"],
            "prototype_id": row["prototype_id"],
            "conductor_address": row["conductor_addr"],
            "settings": settings,
            "version": row["version"],
            "account_id": row["account_id"],
            "accts": accts,
            "created_at": row["created_at"],
        }

    async def update_model_conductor_addr(self, model_id: str, conductor_addr: str) -> bool:
        if not model_id:
            return False
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE models SET conductor_addr = $1 WHERE model_id = $2",
                (conductor_addr or ""),
                model_id,
            )
        return True

    async def delete_model(self, model_id: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM files WHERE model_id = $1", model_id)
            await conn.execute("DELETE FROM models WHERE model_id = $1", model_id)

    async def count_models(self) -> int:
        async with self.pool.acquire() as conn:
            n = await conn.fetchval("SELECT COUNT(1) FROM models")
            return n or 0

    def _run_sync(self, coro):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        def runner():
            return asyncio.run(coro)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(runner).result()

    async def _with_fresh_conn(self, fn):
        import asyncpg

        conn = await asyncpg.connect(self.dsn)
        try:
            return await fn(conn)
        finally:
            await conn.close()

    def insert_file(self, *, model_id, file_id, folder, kind, ext="", status="ready", original_name=None, mime_type=None, size_bytes=None, created_at=None, info=None, remote_file_id=None, url=None, chat_id=None, acct_id=None, server=None, remote_path=None):
        if not model_id:
            raise InternalError("missing_model_id")
        if created_at is None:
            created_at = time.time()
        cols = ", ".join(FILE_COLUMNS)
        placeholders = ", ".join(f"${i}" for i in range(1, len(FILE_COLUMNS) + 1))
        values = (
            file_id,
            model_id,
            folder,
            kind,
            ext or "",
            status,
            original_name,
            mime_type,
            size_bytes,
            created_at,
            info,
            remote_file_id,
            url,
            chat_id,
            acct_id,
            server,
            remote_path,
        )

        async def _do(conn):
            import asyncpg

            try:
                await conn.execute(
                    f"INSERT INTO files ({cols}) VALUES ({placeholders})",
                    *values,
                )
            except asyncpg.UniqueViolationError as e:
                raise ExternalError("file_id_exists") from e

        self._run_sync(self._with_fresh_conn(_do))

    def get_file(self, file_id, *, model_id, include_remote=True):
        if not model_id or not file_id:
            return None

        async def _do(conn):
            row = await conn.fetchrow(
                "SELECT * FROM files WHERE file_id = $1 AND model_id = $2",
                file_id,
                model_id,
            )
            return row_to_file_dict(row, include_remote=include_remote) if row else None

        return self._run_sync(self._with_fresh_conn(_do))

    def update_file(self, file_id, *, model_id, **fields):
        updates = {k: v for k, v in fields.items() if k in FILE_UPDATE_ALLOWED}
        if not model_id or not file_id or not updates:
            return self.get_file(file_id, model_id=model_id, include_remote=False)
        keys = list(updates.keys())
        sets = ", ".join(f"{k} = ${i}" for i, k in enumerate(keys, start=1))
        vals = list(updates.values())
        fid_i = len(vals) + 1
        model_i = len(vals) + 2
        vals.extend([file_id, model_id])

        async def _do(conn):
            await conn.execute(
                f"UPDATE files SET {sets} WHERE file_id = ${fid_i} AND model_id = ${model_i}",
                *vals,
            )

        self._run_sync(self._with_fresh_conn(_do))
        return self.get_file(file_id, model_id=model_id, include_remote=False)

    async def get_or_fetch_model(self, model_id: str, session, consul_url: str, token: str) -> dict | None:
        model = await self.get_model(model_id)
        if not model:
            remote = await fetch_model(session=session, consul_url=consul_url, token=token, model_id=model_id)
            if remote:
                await self.put_model(model_id, remote)
                remote_model_id = remote.get("model_id")
                if remote_model_id and remote_model_id != model_id:
                    await self.put_model(remote_model_id, remote)
                model = (await self.get_model(model_id)) or (await self.get_model(remote.get("model_id")))
        return model

    async def put_prototype(self, data: dict) -> None:
        prototype_id = data.get("prototype_id")
        name = data.get("name", "")
        token = data.get("token", "")
        access_point = data.get("access_point", "")
        proto_type = data.get("type", "token")
        ava = data.get("ava", False)
        version = data.get("version")
        raw_data = json.dumps(data)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO prototypes
                (prototype_id, name, token, access_point, type, ava, version, raw_data, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (prototype_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    token = EXCLUDED.token,
                    access_point = EXCLUDED.access_point,
                    type = EXCLUDED.type,
                    ava = EXCLUDED.ava,
                    version = EXCLUDED.version,
                    raw_data = EXCLUDED.raw_data,
                    created_at = EXCLUDED.created_at
                """,
                prototype_id,
                name,
                token,
                access_point,
                proto_type,
                ava,
                version,
                raw_data,
                int(time.time()),
            )

    async def get_prototype_info(self, prototype_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT raw_data FROM prototypes WHERE prototype_id = $1", prototype_id)
        if not row or not row["raw_data"]:
            return None
        return json.loads(row["raw_data"])

    async def is_duplicate_request(self, key: str) -> bool:
        if not key:
            return False
        return shared_dedupe_cache().contains(key)

    async def remember_request(self, key: str, ttl: int = 300) -> bool:
        if not key:
            return True

        return shared_dedupe_cache().remember(key, ttl=ttl)

    async def cleanup_requests(self, ttl: int = 3600) -> None:
        shared_dedupe_cache().cleanup(ttl=ttl)

    async def put_inbound_pending(self, *, job_id: str, model_id: str, kind: str, payload: dict) -> None:
        if not job_id or not model_id or not kind:
            raise InternalError("inbound_pending requires job_id, model_id, and kind")
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO inbound_pending (job_id, model_id, kind, payload, created_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (job_id) DO UPDATE SET
                    model_id = EXCLUDED.model_id,
                    kind = EXCLUDED.kind,
                    payload = EXCLUDED.payload,
                    created_at = EXCLUDED.created_at
                """,
                job_id,
                model_id,
                kind,
                body,
                time.time(),
            )

    async def delete_inbound_pending(self, job_id: str) -> None:
        if not job_id:
            return
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM inbound_pending WHERE job_id = $1", job_id)

    async def list_inbound_pending(self, model_id: str | None = None) -> list[dict]:
        async with self.pool.acquire() as conn:
            if model_id:
                rows = await conn.fetch(
                    """
                    SELECT job_id, model_id, kind, payload, created_at
                    FROM inbound_pending
                    WHERE model_id = $1
                    ORDER BY created_at ASC
                    """,
                    model_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT job_id, model_id, kind, payload, created_at
                    FROM inbound_pending
                    ORDER BY created_at ASC
                    """
                )
        out = []
        for row in rows:
            out.append(
                {
                    "job_id": row["job_id"],
                    "model_id": row["model_id"],
                    "kind": row["kind"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                }
            )
        return out

    async def delete_local_conductor_state_for_model(self, model_id: str) -> None:
        if not model_id:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    DELETE FROM local_account_runtime_state
                    WHERE acct_id IN (
                        SELECT acct_id
                        FROM local_accounts
                        WHERE model_id = $1
                    )
                    """,
                    model_id,
                )
                await conn.execute("DELETE FROM local_chats WHERE model_id = $1", model_id)
                await conn.execute("DELETE FROM local_accounts WHERE model_id = $1", model_id)

    async def upsert_local_account(
        self,
        *,
        acct_id: str,
        model_id: str | None,
        prototype_id: int | None,
        acct_type: str | None,
        username: str | None,
        server: str | None,
        encrypted_token: str | None,
        is_local: bool = True,
    ) -> None:
        if not acct_id:
            raise ExternalError("acct_id is required")
        acct_type = validate_local_platform_type(acct_type, field_name="acct_type")
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO local_accounts
                (acct_id, model_id, prototype_id, acct_type, username, server, encrypted_token, is_local, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (acct_id) DO UPDATE SET
                    model_id = EXCLUDED.model_id,
                    prototype_id = EXCLUDED.prototype_id,
                    acct_type = EXCLUDED.acct_type,
                    username = EXCLUDED.username,
                    server = EXCLUDED.server,
                    encrypted_token = EXCLUDED.encrypted_token,
                    is_local = EXCLUDED.is_local,
                    created_at = EXCLUDED.created_at
                """,
                acct_id,
                model_id or None,
                prototype_id,
                acct_type,
                username or None,
                server or None,
                encrypted_token or None,
                is_local,
                int(time.time()),
            )

    async def get_local_account(self, acct_id: str) -> dict | None:
        if not acct_id:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT acct_id, model_id, prototype_id, acct_type, username, server, encrypted_token, is_local
                FROM local_accounts
                WHERE acct_id = $1
                """,
                acct_id,
            )
        if not row:
            return None
        return {
            "acct_id": row["acct_id"],
            "model_id": row["model_id"],
            "prototype_id": row["prototype_id"],
            "acct_type": row["acct_type"],
            "username": row["username"],
            "server": row["server"],
            "encrypted_token": row["encrypted_token"],
            "is_local": row["is_local"],
        }

    async def list_local_accounts(self) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT acct_id, model_id, prototype_id, acct_type, username, server, encrypted_token
                FROM local_accounts
                ORDER BY acct_type ASC, acct_id ASC
                """,
            )
        out = []
        for r in rows:
            out.append(
                {
                    "acct_id": r["acct_id"],
                    "model_id": r["model_id"],
                    "prototype_id": r["prototype_id"],
                    "acct_type": r["acct_type"],
                    "username": r["username"],
                    "server": r["server"],
                    "encrypted_token": r["encrypted_token"],
                }
            )
        return out

    async def delete_local_account(self, acct_id: str) -> None:
        if not acct_id:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM local_account_runtime_state WHERE acct_id = $1", acct_id)
                await conn.execute("DELETE FROM local_chats WHERE acct_id = $1", acct_id)
                await conn.execute("DELETE FROM local_accounts WHERE acct_id = $1", acct_id)

    async def replace_model_chats(self, *, model_id: str, prototype_id: int | None, chats: list[dict]) -> None:
        if not model_id:
            raise InternalError("model_id is required")
        now = int(time.time())
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM local_chats WHERE model_id = $1", model_id)
                for c in chats or []:
                    acct_id = c.get("acct_id") or ""
                    chat_id = c.get("chat_id") or ""
                    if not acct_id or not chat_id:
                        continue
                    await conn.execute(
                        """
                        INSERT INTO local_chats
                        (acct_id, chat_id, model_id, prototype_id, chat_type, carrier_user_id, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT (acct_id, chat_id) DO UPDATE SET
                            model_id = EXCLUDED.model_id,
                            prototype_id = EXCLUDED.prototype_id,
                            chat_type = EXCLUDED.chat_type,
                            carrier_user_id = EXCLUDED.carrier_user_id,
                            created_at = EXCLUDED.created_at
                        """,
                        acct_id,
                        chat_id,
                        model_id,
                        prototype_id,
                        validate_local_platform_type(
                            strip_qr_prefix(c.get("chat_type")),
                            field_name="chat_type",
                        ),
                        c.get("carrier_user_id") or None,
                        now,
                    )

    async def upsert_local_chat(
        self,
        *,
        acct_id: str,
        chat_id: str,
        model_id: str,
        prototype_id: int | None,
        chat_type: str | None,
        carrier_user_id: str | None,
    ) -> None:
        if not (acct_id and chat_id and model_id):
            return
        chat_type = validate_local_platform_type(
            strip_qr_prefix(chat_type),
            field_name="chat_type",
        )
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO local_chats
                (acct_id, chat_id, model_id, prototype_id, chat_type, carrier_user_id, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (acct_id, chat_id) DO UPDATE SET
                    model_id = EXCLUDED.model_id,
                    prototype_id = EXCLUDED.prototype_id,
                    chat_type = EXCLUDED.chat_type,
                    carrier_user_id = EXCLUDED.carrier_user_id,
                    created_at = EXCLUDED.created_at
                """,
                acct_id,
                chat_id,
                model_id,
                prototype_id,
                chat_type,
                carrier_user_id or None,
                int(time.time()),
            )

    async def list_model_chats(self, *, model_id: str, acct_id: str | None = None) -> list[dict]:
        if not model_id:
            return []
        async with self.pool.acquire() as conn:
            if acct_id:
                rows = await conn.fetch(
                    """
                    SELECT chat_id, chat_type, carrier_user_id, acct_id
                    FROM local_chats
                    WHERE model_id = $1 AND acct_id = $2
                    ORDER BY chat_id ASC
                    """,
                    model_id,
                    acct_id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT chat_id, chat_type, carrier_user_id, acct_id
                    FROM local_chats
                    WHERE model_id = $1
                    ORDER BY chat_id ASC
                    """,
                    model_id,
                )
        out = []
        for r in rows:
            out.append(
                {
                    "chat_id": r["chat_id"],
                    "chat_type": r["chat_type"],
                    "carrier_user_id": r["carrier_user_id"],
                    "acct_id": r["acct_id"],
                }
            )
        return out

    async def lookup_model_for_chat(self, *, acct_id: str, chat_id: str) -> str | None:
        if not acct_id or not chat_id:
            return None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT model_id FROM local_chats WHERE acct_id = $1 AND chat_id = $2",
                acct_id,
                chat_id,
            )
        return row["model_id"] if row else None

    async def remove_chat(self, *, model_id: str, chat_id: str, acct_id: str) -> bool:
        if not model_id or not chat_id or not acct_id:
            return False
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM local_chats WHERE model_id = $1 AND chat_id = $2 AND acct_id = $3",
                model_id,
                chat_id,
                acct_id,
            )
        return True

    async def get_local_account_runtime_state(
        self,
        *,
        acct_id: str,
        acct_type: str,
        state_key: str,
    ) -> dict | None:
        acct_id, acct_type, state_key = self._validate_runtime_state_identifiers(
            acct_id=acct_id,
            acct_type=acct_type,
            state_key=state_key,
        )
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT state_json
                FROM local_account_runtime_state
                WHERE acct_id = $1 AND acct_type = $2 AND state_key = $3
                """,
                acct_id,
                acct_type,
                state_key,
            )
        if not row or not row["state_json"]:
            return None
        return json.loads(row["state_json"])

    async def set_local_account_runtime_state(
        self,
        *,
        acct_id: str,
        acct_type: str,
        state_key: str,
        state: dict,
    ) -> None:
        acct_id, acct_type, state_key = self._validate_runtime_state_identifiers(
            acct_id=acct_id,
            acct_type=acct_type,
            state_key=state_key,
        )
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO local_account_runtime_state (acct_id, acct_type, state_key, state_json, updated_at)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (acct_id, acct_type, state_key) DO UPDATE SET
                    state_json = EXCLUDED.state_json,
                    updated_at = EXCLUDED.updated_at
                """,
                acct_id,
                acct_type,
                state_key,
                json.dumps(state),
                int(time.time()),
            )

    async def close(self) -> None:
        await self.pool.close()
        if self._lock_conn is not None:
            try:
                await self._lock_conn.close()
            except Exception as e:
                logger.error("unexpected where=lock session close error=%s", e, exc_info=e)
            self._lock_conn = None
