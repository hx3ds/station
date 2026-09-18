FILES_TABLE_SQL_SQLITE = """
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    folder TEXT NOT NULL,
    kind TEXT NOT NULL,
    ext TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    original_name TEXT,
    mime_type TEXT,
    size_bytes INTEGER,
    created_at REAL NOT NULL,
    info TEXT,
    remote_file_id TEXT,
    url TEXT,
    chat_id TEXT,
    acct_id TEXT,
    server TEXT,
    remote_path TEXT
)
"""

FILES_TABLE_SQL_POSTGRES = """
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    folder TEXT NOT NULL,
    kind TEXT NOT NULL,
    ext TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    original_name TEXT,
    mime_type TEXT,
    size_bytes BIGINT,
    created_at DOUBLE PRECISION NOT NULL,
    info TEXT,
    remote_file_id TEXT,
    url TEXT,
    chat_id TEXT,
    acct_id TEXT,
    server TEXT,
    remote_path TEXT
)
"""

FILES_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_files_model_id ON files(model_id)",
    "CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind)",
    "CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)",
    "CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder)",
)

FILE_COLUMNS = (
    "file_id",
    "model_id",
    "folder",
    "kind",
    "ext",
    "status",
    "original_name",
    "mime_type",
    "size_bytes",
    "created_at",
    "info",
    "remote_file_id",
    "url",
    "chat_id",
    "acct_id",
    "server",
    "remote_path",
)

FILE_UPDATE_ALLOWED = {
    "status",
    "size_bytes",
    "info",
    "mime_type",
    "original_name",
    "ext",
}

def row_to_file_dict(row, *, include_remote=False):
    if row is None:
        return None
    out = dict(row)
    if not include_remote:
        out.pop("remote_file_id", None)
    return out
