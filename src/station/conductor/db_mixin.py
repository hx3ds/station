import json
import sqlite3
import time

from station.conductor.platform_types import strip_qr_prefix, validate_local_platform_type
from station.database.request_dedupe import run_in_db_executor
from station.prototypes.boundary import ext_dict, ext_str

class LocalConductorDatabaseMixin:
    def _init_local_conductor_db(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_accounts (
                acct_id TEXT PRIMARY KEY,
                model_id TEXT,
                prototype_id INTEGER,
                acct_type TEXT,
                username TEXT,
                server TEXT,
                encrypted_token TEXT,
                is_local INTEGER,
                created_at INTEGER
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_accounts_model_id ON local_accounts(model_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_accounts_type ON local_accounts(acct_type)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_chats (
                acct_id TEXT,
                chat_id TEXT,
                model_id TEXT,
                prototype_id INTEGER,
                chat_type TEXT,
                carrier_user_id TEXT,
                created_at INTEGER,
                PRIMARY KEY (acct_id, chat_id)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_local_chats_model_id ON local_chats(model_id)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS local_account_runtime_state (
                acct_id TEXT,
                acct_type TEXT,
                state_key TEXT,
                state_json TEXT,
                updated_at INTEGER,
                PRIMARY KEY (acct_id, acct_type, state_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_local_account_runtime_state_acct_id ON local_account_runtime_state(acct_id)"
        )

    def _validate_runtime_state_identifiers(
        self,
        *,
        acct_id: str,
        acct_type: str | None = None,
        state_key: str | None = None,
    ) -> tuple[str, str | None, str | None]:
        if not acct_id:
            raise ValueError("acct_id is required")
        normalized_type = None
        if acct_type is not None:
            normalized_type = validate_local_platform_type(acct_type, field_name="acct_type")
        normalized_key = None
        if state_key is not None:
            if not state_key:
                raise ValueError("state_key is required")
            normalized_key = validate_local_platform_type(state_key, field_name="state_key")
        return acct_id, normalized_type, normalized_key

    def _decode_runtime_state_row(self, row_value: str | None) -> dict | None:
        if row_value is None:
            return None
        raw = ext_str("runtime state row", row_value, default="").strip()
        if not raw:
            return None
        return ext_dict("runtime state", json.loads(raw))

    async def delete_local_conductor_state_for_model(self, model_id: str) -> None:
        if not model_id:
            return

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    """
                    DELETE FROM local_account_runtime_state
                    WHERE acct_id IN (
                        SELECT acct_id
                        FROM local_accounts
                        WHERE model_id = ?
                    )
                    """,
                    (model_id,),
                )
                conn.execute("DELETE FROM local_chats WHERE model_id = ?", (model_id,))
                conn.execute("DELETE FROM local_accounts WHERE model_id = ?", (model_id,))

        await run_in_db_executor(_sync)

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
            raise ValueError("acct_id is required")
        acct_type = validate_local_platform_type(acct_type, field_name="acct_type")

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO local_accounts
                    (acct_id, model_id, prototype_id, acct_type, username, server, encrypted_token, is_local, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        acct_id,
                        model_id,
                        prototype_id,
                        acct_type,
                        username,
                        server,
                        encrypted_token,
                        1 if is_local else 0,
                        int(time.time()),
                    ),
                )

        await run_in_db_executor(_sync)

    async def get_local_account(self, acct_id: str) -> dict | None:
        if not acct_id:
            return None

        def _sync():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT acct_id, model_id, prototype_id, acct_type, username, server, encrypted_token, is_local FROM local_accounts WHERE acct_id = ?",
                    (acct_id,),
                ).fetchone()
            if not row:
                return None
            return {
                "acct_id": row[0],
                "model_id": row[1],
                "prototype_id": row[2],
                "acct_type": row[3],
                "username": row[4],
                "server": row[5],
                "encrypted_token": row[6],
                "is_local": row[7] != 0,
            }

        return await run_in_db_executor(_sync)

    async def list_local_accounts(self) -> list[dict]:
        def _sync():
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT acct_id, model_id, prototype_id, acct_type, username, server, encrypted_token
                    FROM local_accounts
                    ORDER BY acct_type ASC, acct_id ASC
                    """
                ).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "acct_id": r[0],
                        "model_id": r[1],
                        "prototype_id": r[2],
                        "acct_type": r[3],
                        "username": r[4],
                        "server": r[5],
                        "encrypted_token": r[6],
                    }
                )
            return out

        return await run_in_db_executor(_sync)

    async def delete_local_account(self, acct_id: str) -> None:
        if not acct_id:
            return

        def _sync():
            with self._connect() as conn:
                conn.execute("DELETE FROM local_account_runtime_state WHERE acct_id = ?", (acct_id,))
                conn.execute("DELETE FROM local_chats WHERE acct_id = ?", (acct_id,))
                conn.execute("DELETE FROM local_accounts WHERE acct_id = ?", (acct_id,))

        await run_in_db_executor(_sync)

    async def replace_model_chats(self, *, model_id: str, prototype_id: int | None, chats: list[dict]) -> None:
        if not model_id:
            raise ValueError("model_id is required")
        prepared = []
        for c in chats:
            acct_id = c.get("acct_id") or ""
            chat_id = c.get("chat_id") or ""
            if not acct_id or not chat_id:
                continue
            prepared.append(
                (
                    acct_id,
                    chat_id,
                    model_id,
                    prototype_id,
                    validate_local_platform_type(
                        strip_qr_prefix(c.get("chat_type")),
                        field_name="chat_type",
                    ),
                    c.get("carrier_user_id") or None,
                )
            )

        def _sync():
            with self._connect() as conn:
                conn.execute("DELETE FROM local_chats WHERE model_id = ?", (model_id,))
                now = int(time.time())
                for acct_id, chat_id, mid, pid, chat_type, carrier in prepared:
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO local_chats
                        (acct_id, chat_id, model_id, prototype_id, chat_type, carrier_user_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (acct_id, chat_id, mid, pid, chat_type, carrier, now),
                    )

        await run_in_db_executor(_sync)

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

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO local_chats
                    (acct_id, chat_id, model_id, prototype_id, chat_type, carrier_user_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        acct_id,
                        chat_id,
                        model_id,
                        prototype_id,
                        chat_type,
                        carrier_user_id,
                        int(time.time()),
                    ),
                )

        await run_in_db_executor(_sync)

    async def list_model_chats(self, *, model_id: str, acct_id: str | None = None) -> list[dict]:
        if not model_id:
            return []

        def _sync():
            with self._connect() as conn:
                if acct_id:
                    rows = conn.execute(
                        "SELECT chat_id, chat_type, carrier_user_id, acct_id FROM local_chats WHERE model_id = ? AND acct_id = ? ORDER BY chat_id ASC",
                        (model_id, acct_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT chat_id, chat_type, carrier_user_id, acct_id FROM local_chats WHERE model_id = ? ORDER BY chat_id ASC",
                        (model_id,),
                    ).fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "chat_id": r[0],
                        "chat_type": r[1],
                        "carrier_user_id": r[2],
                        "acct_id": r[3],
                    }
                )
            return out

        return await run_in_db_executor(_sync)

    async def lookup_model_for_chat(self, *, acct_id: str, chat_id: str) -> str | None:
        if not acct_id or not chat_id:
            return None

        def _sync():
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT model_id FROM local_chats WHERE acct_id = ? AND chat_id = ?",
                    (acct_id, chat_id),
                ).fetchone()
            return row[0] if row else None

        return await run_in_db_executor(_sync)

    async def remove_chat(self, *, model_id: str, chat_id: str, acct_id: str) -> bool:
        if not model_id or not chat_id or not acct_id:
            return False

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM local_chats WHERE model_id = ? AND chat_id = ? AND acct_id = ?",
                    (model_id, chat_id, acct_id),
                )
            return True

        return await run_in_db_executor(_sync)

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

        def _sync():
            with self._connect() as conn:
                row = conn.execute(
                    """
                    SELECT state_json
                    FROM local_account_runtime_state
                    WHERE acct_id = ? AND acct_type = ? AND state_key = ?
                    """,
                    (acct_id, acct_type, state_key),
                ).fetchone()
            return self._decode_runtime_state_row(row[0] if row else None)

        return await run_in_db_executor(_sync)

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
        payload = json.dumps(state)

        def _sync():
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO local_account_runtime_state
                    (acct_id, acct_type, state_key, state_json, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (acct_id, acct_type, state_key, payload, int(time.time())),
                )

        await run_in_db_executor(_sync)
