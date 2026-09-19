import os
import re
import time
import uuid

from station.errors import ExternalError, InternalError
from station.prototypes.boundary import ext_dict, ext_list, ext_str, validate_attachment
from station.prototypes.fs_paths import model_dir

_VALID_KINDS = ("chat", "temp")
_VALID_FOLDERS = ("download", "temp")
_VALID_STATUSES = ("registered", "ready")
_KIND_FOLDER = {"chat": "download", "temp": "temp"}
_PUBLIC_META_KEYS = (
    "file_id",
    "folder",
    "kind",
    "ext",
    "status",
    "original_name",
    "mime_type",
    "size_bytes",
    "created_at",
    "info",
    "url",
    "chat_id",
    "acct_id",
    "server",
    "remote_path",
)

def new_file_id():
    return uuid.uuid4().hex

def sanitize_ext(value):

    raw = ext_str("ext", value).lower()
    if not raw:
        return ""
    if "/" in raw or "\\" in raw:
        raw = os.path.basename(raw.replace("\\", "/"))
    if "." in raw and not raw.startswith("."):
        raw = raw.rsplit(".", 1)[-1]
        raw = "." + raw if raw else ""
    if not raw.startswith("."):
        raw = "." + raw
    cleaned = "." + re.sub(r"[^a-z0-9]", "", raw[1:])
    if cleaned == "." or len(cleaned) > 16:
        return ""
    return cleaned

def ext_from_name(name):
    name = ext_str("name", name)
    base = os.path.basename(name.replace("\\", "/"))
    _, ext = os.path.splitext(base)
    return sanitize_ext(ext)

def disk_name(*, kind, file_id, ext=""):
    e = sanitize_ext(ext)
    return f"{kind}_{file_id}{e}"

class BoundFileSystem:
    def __init__(self, app, *, prototype_id, model_id):
        self.app = app
        self.prototype_id = prototype_id
        self.model_id = model_id
        self._ensure_layout()

    @property
    def db(self):
        db = self.app.get("db")
        if db is None:
            raise InternalError("missing_db")
        return db

    @property
    def fs_root(self):
        return self.app.get("fs_root")

    @property
    def root(self):
        p = model_dir(self.fs_root, self.prototype_id, self.model_id)
        os.makedirs(p, exist_ok=True)
        return p

    @property
    def download_dir(self):
        p = os.path.join(self.root, "download")
        os.makedirs(p, exist_ok=True)
        return p

    @property
    def temp_dir(self):
        p = os.path.join(self.root, "temp")
        os.makedirs(p, exist_ok=True)
        return p

    def _ensure_layout(self):
        os.makedirs(self.download_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

    def relative_path_for_row(self, row):
        name = disk_name(kind=row["kind"], file_id=row["file_id"], ext=row["ext"] if row["ext"] is not None else "")
        return f"{row['folder']}/{name}"

    def absolute_path_for_row(self, row):
        return os.path.join(self.root, self.relative_path_for_row(row))

    def resolve_file_id(self, file_id):
        row = self._get_row(file_id)
        if row is None:
            raise ExternalError("file_not_found")
        return self.absolute_path_for_row(row)

    def _public_meta(self, row):
        if row is None:
            return None
        return {k: row.get(k) for k in _PUBLIC_META_KEYS if k in row}

    def _get_row(self, file_id, *, include_remote=True):
        return self.db.get_file(file_id, model_id=self.model_id, include_remote=include_remote)

    def register_file(
        self,
        *,
        file_id,
        folder,
        kind,
        ext="",
        info=None,
        status="ready",
        original_name=None,
        mime_type=None,
        size_bytes=None,
        remote_file_id=None,
        url=None,
        chat_id=None,
        acct_id=None,
        server=None,
        remote_path=None,
    ):
        fid = file_id.strip() if file_id else ""
        if not fid or len(fid) != 32 or any(c not in "0123456789abcdef" for c in fid):
            raise ExternalError("invalid_file_id")
        if folder not in _VALID_FOLDERS:
            raise ExternalError("invalid_folder")
        if kind not in _VALID_KINDS:
            raise ExternalError("invalid_kind")
        if _KIND_FOLDER[kind] != folder:
            raise ExternalError("folder_kind_mismatch")
        if status not in _VALID_STATUSES:
            raise ExternalError("invalid_status")
        e = sanitize_ext(ext)
        self.db.insert_file(
            model_id=self.model_id,
            file_id=fid,
            folder=folder,
            kind=kind,
            ext=e,
            status=status,
            original_name=original_name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            created_at=time.time(),
            info=info,
            remote_file_id=remote_file_id,
            url=url,
            chat_id=chat_id,
            acct_id=acct_id,
            server=server,
            remote_path=remote_path,
        )
        return self.check_file(fid)

    def _update_file(self, file_id, **fields):
        self.db.update_file(file_id, model_id=self.model_id, **fields)
        return self.check_file(file_id)

    def check_file(self, file_id):
        row = self._get_row(file_id, include_remote=False)
        if row is None:
            raise ExternalError("file_not_found")
        meta = self._public_meta(row)
        full = self.absolute_path_for_row(row)
        meta["path"] = full
        exists = os.path.isfile(full)
        meta["exists"] = exists
        if exists:
            st = os.stat(full)
            meta["disk_size_bytes"] = st.st_size
            meta["mtime"] = st.st_mtime
        else:
            meta["disk_size_bytes"] = None
            meta["mtime"] = None
        return meta

    def _write_bytes(self, *, folder, kind, file_id, ext, data):
        name = disk_name(kind=kind, file_id=file_id, ext=ext)
        dest_dir = self.download_dir if folder == "download" else self.temp_dir
        full = os.path.join(dest_dir, name)
        parent = os.path.dirname(full)
        os.makedirs(parent, exist_ok=True)
        tmp = os.path.join(parent, f".{name}.{uuid.uuid4().hex}.tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, full)
        return full, os.path.getsize(full)

    async def download_chat_file(self, *, file_id, info=None, client_context=None, token=None, max_attempts=3):
        from station.client.conductor import download_file as gateway_download_file

        row = self._get_row(file_id, include_remote=True)
        if row is None:
            raise ExternalError("file_not_found")
        if row["kind"] != "chat":
            raise ExternalError("not_chat_file")
        if row["status"] == "ready":
            full = self.absolute_path_for_row(row)
            if os.path.isfile(full):
                meta = self.check_file(file_id)
                if info is not None:
                    meta = self._update_file(file_id, info=info)
                return meta
        remote_file_id = row.get("remote_file_id")
        stored_url = ext_str("url", row.get("url"))
        chat_id = row.get("chat_id")
        acct_id = row.get("acct_id")
        if stored_url.startswith("http://") or stored_url.startswith("https://"):
            remote = ext_str("remote_file_id", remote_file_id)
            if not (remote.startswith("http://") or remote.startswith("https://")):
                remote_file_id = stored_url
        if not remote_file_id or not chat_id or not acct_id:
            raise ExternalError("missing_remote_fields")
        if client_context is None:
            registry = self.app.get("tenants")
            if registry is not None:
                tenant = registry.get(self.prototype_id)
                if tenant is not None:
                    client_context = tenant.client_context
        if client_context is None:
            raise InternalError("missing_client_context")
        data = await gateway_download_file(
            client_context,
            model_id=self.model_id,
            token=token,
            chat_id=chat_id,
            file_id=remote_file_id,
            max_attempts=max_attempts,
            acct_id=acct_id,
        )
        if not data:
            raise ExternalError("download_failed")
        full, size = self._write_bytes(
            folder=row["folder"],
            kind=row["kind"],
            file_id=row["file_id"],
            ext=row.get("ext") or "",
            data=data,
        )
        fields = {"status": "ready", "size_bytes": size}
        if info is not None:
            fields["info"] = info
        return self._update_file(file_id, **fields)

    def _place_ready(
        self,
        *,
        data,
        kind,
        original_name=None,
        mime_type=None,
        info=None,
        ext=None,
        file_id=None,
        remote_file_id=None,
        url=None,
        chat_id=None,
        acct_id=None,
        server=None,
        remote_path=None,
    ):
        if kind not in _VALID_KINDS:
            raise ExternalError("invalid_kind")
        folder = _KIND_FOLDER[kind]
        fid = file_id or new_file_id()
        e = sanitize_ext(ext) if ext is not None else ext_from_name(original_name or remote_path or url)
        full, size = self._write_bytes(folder=folder, kind=kind, file_id=fid, ext=e, data=data)
        return self.register_file(
            file_id=fid,
            folder=folder,
            kind=kind,
            ext=e,
            info=info,
            status="ready",
            original_name=original_name,
            mime_type=mime_type,
            size_bytes=size,
            remote_file_id=remote_file_id,
            url=url,
            chat_id=chat_id,
            acct_id=acct_id,
            server=server,
            remote_path=remote_path,
        )

    def save_temp(self, *, data=None, content=None, original_name=None, mime_type=None, info=None, ext=None, file_id=None):
        if data is None and content is None:
            raise ExternalError("missing_data")
        if data is None:
            data = content.encode("utf-8")
        return self._place_ready(
            data=data,
            kind="temp",
            original_name=original_name,
            mime_type=mime_type,
            info=info,
            ext=ext,
            file_id=file_id,
        )

    def register_inbound_attachment(self, attachment, *, chat_id=None, acct_id=None):

        attachment = validate_attachment(attachment)
        remote = ext_str("file_id", attachment.get("file_id"))
        local_path = attachment.get("local_path")
        if local_path is not None:
            local_path = ext_str("local_path", local_path)
        if not remote and not local_path:
            return attachment
        if remote and len(remote) == 32 and all(c in "0123456789abcdef" for c in remote):
            existing = self._get_row(remote, include_remote=False)
            if existing is not None:
                out = dict(attachment)
                out["file_id"] = remote
                out.pop("local_path", None)
                return out
        fid = new_file_id()
        file_name = attachment.get("file_name")
        if file_name is None:
            file_name = attachment.get("filename")
        file_name = ext_str("file_name", file_name)
        mime_type = attachment.get("mime_type")
        if mime_type is None:
            mime_type = attachment.get("content_type")
        if mime_type is None:
            mime_type = attachment.get("contentType")
        if mime_type is not None:
            mime_type = ext_str("mime_type", mime_type)
        e = ext_from_name(file_name)
        if not e and mime_type:
            mime_map = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
                "audio/ogg": ".ogg",
                "audio/mpeg": ".mp3",
                "audio/wav": ".wav",
                "video/mp4": ".mp4",
                "application/pdf": ".pdf",
            }
            e = sanitize_ext(mime_map.get(mime_type.split(";")[0].strip(), ""))
        stored_url = attachment.get("url")
        if stored_url is not None:
            stored_url = ext_str("url", stored_url) or None
        if stored_url and not (stored_url.startswith("http://") or stored_url.startswith("https://")):
            stored_url = None
        if local_path and os.path.isfile(local_path):
            with open(local_path, "rb") as f:
                data = f.read()
            self._place_ready(
                data=data,
                kind="chat",
                file_id=fid,
                ext=e,
                original_name=file_name or None,
                mime_type=mime_type,
                remote_file_id=remote or None,
                url=stored_url,
                chat_id=chat_id,
                acct_id=acct_id,
            )
        else:
            self.register_file(
                file_id=fid,
                folder="download",
                kind="chat",
                ext=e,
                status="registered",
                original_name=file_name or None,
                mime_type=mime_type,
                remote_file_id=remote,
                url=stored_url,
                chat_id=chat_id,
                acct_id=acct_id,
            )
        out = dict(attachment)
        out["file_id"] = fid
        out.pop("local_path", None)
        return out

    def rewrite_inbound_attachments(self, body, *, chat_id=None, acct_id=None):

        body = ext_dict("body", body)
        attachments = body.get("attachments")
        if attachments is None:
            return body
        attachments = ext_list("attachments", attachments)
        if not attachments:
            return body
        rewritten = [self.register_inbound_attachment(att, chat_id=chat_id, acct_id=acct_id) for att in attachments]
        out = dict(body)
        out["attachments"] = rewritten
        return out

    def remote_file_id_for(self, file_id):
        row = self._get_row(file_id, include_remote=True)
        if row is None:
            return None
        return row.get("remote_file_id")

    def file_row(self, file_id, *, include_remote=False):
        return self._get_row(file_id, include_remote=include_remote)
