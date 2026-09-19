import sqlite3
import json
import time
import os
import shutil
from station.client.consul import fetch_model
from station.conductor.db_mixin import LocalConductorDatabaseMixin
from station.database import AsyncDatabase
from station.database.request_dedupe import run_in_db_executor, shared_dedupe_cache
from station.database.file_registry import (
    FILE_COLUMNS,
    FILE_UPDATE_ALLOWED,
    FILES_INDEXES,
    FILES_TABLE_SQL_SQLITE,
    row_to_file_dict,
)
from station.database.postgres_database import PostgresDatabase
from station.errors import ExternalError, InternalError
from station import logger

def _remove_path(path: str) -> None:
    path = (path or "").strip()
    if not path:
        return
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)

def _clear_directory(path: str) -> None:
    path = (path or "").strip()
    if not path:
        return
    if not os.path.isdir(path):
        return
    for name in os.listdir(path):
        _remove_path(os.path.join(path, name))

def wipe_station_runtime_data(*, db_path: str, fs_root: str) -> None:
    logger.info(
        "wipe_on_restart db=%s fs=%s",
        db_path,
        fs_root,
    )
    db_path = (db_path or "").strip()
    if db_path:
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = f"{db_path}{suffix}" if suffix else db_path
            if os.path.lexists(candidate):
                os.unlink(candidate)
    _clear_directory(fs_root)

class Database(LocalConductorDatabaseMixin, AsyncDatabase):
    def __init__(self, db_path="prototype.db", *, enable_local_conductor: bool = False):
        self.db_path = db_path
        self.enable_local_conductor = enable_local_conductor
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self):
        try:
            with self._connect() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS models (
                        model_id TEXT PRIMARY KEY,
                        prototype_id INTEGER,
                        conductor_addr TEXT,
                        settings TEXT,
                        version INTEGER,
                        account_id TEXT,
                        accts TEXT,
                        created_at INTEGER
                    )
                """)
                conn.execute("DROP TABLE IF EXISTS requests")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS inbound_pending (
                        job_id TEXT PRIMARY KEY,
                        model_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS prototypes (
                        prototype_id INTEGER PRIMARY KEY,
                        name TEXT,
                        token TEXT,
                        access_point TEXT,
                        type TEXT,
                        ava BOOLEAN,
                        version INTEGER,
                        raw_data TEXT,
                        created_at INTEGER
                    )
                """)
                conn.execute(FILES_TABLE_SQL_SQLITE)
                for stmt in FILES_INDEXES:
                    conn.execute(stmt)
                if self.enable_local_conductor:
                    self._init_local_conductor_db(conn)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_pending_model_id ON inbound_pending(model_id)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_inbound_pending_created_at ON inbound_pending(created_at)")
        except Exception as e:
            logger.error("database start failed error=%s", e, exc_info=e)
            raise

    def insert_file(self, *, model_id, file_id, folder, kind, ext="", status="ready", original_name=None, mime_type=None, size_bytes=None, created_at=None, info=None, remote_file_id=None, url=None, chat_id=None, acct_id=None, server=None, remote_path=None):
        if not model_id:
            raise InternalError("missing_model_id")
        if created_at is None:
            created_at = time.time()
        try:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO files ({", ".join(FILE_COLUMNS)})
                    VALUES ({", ".join("?" for _ in FILE_COLUMNS)})
                    """,
                    (
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
                    ),
                )
        except sqlite3.IntegrityError:
            raise ExternalError("file_id_exists")

    def get_file(self, file_id, *, model_id, include_remote=True):
        if not model_id or not file_id:
            return None
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM files WHERE file_id = ? AND model_id = ?",
                (file_id, model_id),
            )
            row = cur.fetchone()
        return row_to_file_dict(row, include_remote=include_remote)

    def update_file(self, file_id, *, model_id, **fields):
        updates = {k: v for k, v in fields.items() if k in FILE_UPDATE_ALLOWED}
        if not model_id or not file_id or not updates:
            return self.get_file(file_id, model_id=model_id, include_remote=False)
        cols = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [file_id, model_id]
        with self._connect() as conn:
            conn.execute(
                f"UPDATE files SET {cols} WHERE file_id = ? AND model_id = ?",
                vals,
            )
        return self.get_file(file_id, model_id=model_id, include_remote=False)

    async def put_model(self, model_id, data):
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
        payload = (
            model_id,
            prototype_id,
            conductor_addr,
            json.dumps(settings_obj),
            version,
            account_id,
            json.dumps(accts_obj),
            time.time(),
        )

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO models
                    (model_id, prototype_id, conductor_addr, settings, version, account_id, accts, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )

        await run_in_db_executor(_sync)

    async def get_model(self, model_id):
        def _sync():
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT model_id, prototype_id, conductor_addr, settings, version, account_id, accts, created_at FROM models WHERE model_id = ?",
                    (model_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                settings = json.loads(row[3]) if row[3] else {}
                accts = json.loads(row[6]) if row[6] else []
                return {
                    "model_id": row[0],
                    "prototype_id": row[1],
                    "conductor_address": row[2],
                    "settings": settings,
                    "version": row[4],
                    "account_id": row[5],
                    "accts": accts,
                    "created_at": row[7],
                }

        return await run_in_db_executor(_sync)

    async def update_model_conductor_addr(self, model_id, conductor_addr):
        if not model_id:
            return False

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    "UPDATE models SET conductor_addr = ? WHERE model_id = ?",
                    (conductor_addr or "", model_id),
                )
            return True

        return await run_in_db_executor(_sync)

    async def delete_model(self, model_id):
        def _sync():
            with self._connect() as conn:
                conn.execute("DELETE FROM files WHERE model_id = ?", (model_id,))
                conn.execute("DELETE FROM models WHERE model_id = ?", (model_id,))

        await run_in_db_executor(_sync)

    async def count_models(self):
        def _sync():
            with self._connect() as conn:
                row = conn.execute("SELECT COUNT(1) FROM models").fetchone()
                return row[0] if row else 0

        return await run_in_db_executor(_sync)

    async def put_prototype(self, data):
        prototype_id = data.get("prototype_id")
        name = data.get("name", "")
        token = data.get("token", "")
        access_point = data.get("access_point", "")
        proto_type = data.get("type", "token")
        ava = data.get("ava", False)
        version = data.get("version")
        raw_data = json.dumps(data)
        payload = (
            prototype_id,
            name,
            token,
            access_point,
            proto_type,
            ava,
            version,
            raw_data,
            time.time(),
        )

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO prototypes
                    (prototype_id, name, token, access_point, type, ava, version, raw_data, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )

        await run_in_db_executor(_sync)

    async def get_prototype_info(self, prototype_id):
        def _sync():
            with self._connect() as conn:
                cursor = conn.execute("SELECT raw_data FROM prototypes WHERE prototype_id = ?", (prototype_id,))
                row = cursor.fetchone()
                if not row or not row[0]:
                    return None
                return json.loads(row[0])

        return await run_in_db_executor(_sync)

    async def is_duplicate_request(self, key):
        if not key:
            return False

        return shared_dedupe_cache().contains(key)

    async def remember_request(self, key, ttl=300):
        if not key:
            return True

        return shared_dedupe_cache().remember(key, ttl=ttl)

    async def cleanup_requests(self, ttl=3600):
        shared_dedupe_cache().cleanup(ttl=ttl)

    async def put_inbound_pending(self, *, job_id: str, model_id: str, kind: str, payload: dict) -> None:
        if not job_id or not model_id or not kind:
            raise InternalError("inbound_pending requires job_id, model_id, and kind")
        now = time.time()
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

        def _put():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO inbound_pending (job_id, model_id, kind, payload, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                        model_id=excluded.model_id,
                        kind=excluded.kind,
                        payload=excluded.payload,
                        created_at=excluded.created_at
                    """,
                    (job_id, model_id, kind, body, now),
                )

        await run_in_db_executor(_put)

    async def delete_inbound_pending(self, job_id: str) -> None:
        if not job_id:
            return

        def _delete():
            with self._connect() as conn:
                conn.execute("DELETE FROM inbound_pending WHERE job_id = ?", (job_id,))

        await run_in_db_executor(_delete)

    async def list_inbound_pending(self, model_id: str | None = None) -> list[dict]:
        def _list():
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                if model_id:
                    cur = conn.execute(
                        """
                        SELECT job_id, model_id, kind, payload, created_at
                        FROM inbound_pending
                        WHERE model_id = ?
                        ORDER BY created_at ASC
                        """,
                        (model_id,),
                    )
                else:
                    cur = conn.execute(
                        """
                        SELECT job_id, model_id, kind, payload, created_at
                        FROM inbound_pending
                        ORDER BY created_at ASC
                        """
                    )
                return [dict(row) for row in cur.fetchall()]

        rows = await run_in_db_executor(_list)
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

    async def get_or_fetch_model(self, model_id, session, consul_url, token):
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

    async def ensure_model_lock(self, app, model_id: str) -> bool:
        return True

    async def try_model_lock_once(self, app, model_id: str) -> bool:
        return True

    async def release_model_lock_from_app(self, app, model_id: str) -> None:
        return None

    async def release_all_model_locks(self, app) -> None:
        return None

    async def close(self) -> None:
        return None


def _normalize_backend(value: str | None) -> str:
    v = (value or "").strip().lower()
    if not v:
        return "sqlite"
    if v == "postgres":
        return "postgres"
    if v == "sqlite":
        return "sqlite"
    return v

async def create_database(
    *,
    backend: str | None = None,
    db_path: str,
    dsn: str | None = None,
    enable_local_conductor: bool = False,
    wipe_on_restart: bool = False,
) -> AsyncDatabase:
    backend = _normalize_backend(backend)
    dsn = (dsn or "").strip() or None

    if backend == "postgres":
        if not dsn:
            raise ExternalError("DB_DSN is required when DB_BACKEND=postgres")
        db = await PostgresDatabase.create(dsn=dsn, enable_local_conductor=enable_local_conductor)
        if wipe_on_restart:
            await db.wipe_all()
            await db._init_db()
        logger.info("database connected backend=postgres")
        return db

    if backend == "sqlite":
        db = Database(db_path=db_path, enable_local_conductor=enable_local_conductor)
        logger.info("database connected backend=sqlite")
        return db

    raise ExternalError("Unsupported DB_BACKEND: %s" % backend)
