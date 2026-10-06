import contextlib
import mimetypes
import os

from station.errors import ExternalError
from station.prototypes.attachments import attachment_type_for_path

STATION_OUTBOUND_DIR = ".station_outbound"
STATION_OUTBOUND_HINT = (
    "To send files to the user, write them under %s/ in the workspace. "
    "Station delivers those files with send_message methods."
    % STATION_OUTBOUND_DIR
)


def is_file_id(value):
    text = (value or "").strip()
    return len(text) == 32 and all(c in "0123456789abcdef" for c in text)


class PrototypeOutboundFiles:
    def _station_outbound_dir(self):
        return os.path.join(self._workspace_root(), STATION_OUTBOUND_DIR)

    def _attachment_from_path(self, path):
        if not path or not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            return None
        name = os.path.basename(path)
        mime, _ = mimetypes.guess_type(path)
        mime = mime or "application/octet-stream"
        meta = self.save_temp(data=data, original_name=name, mime_type=mime)
        return {
            "type": attachment_type_for_path(path, mime=mime),
            "file_id": meta["file_id"],
            "file_name": name,
        }

    def _attachment_from_file_id(self, file_id):
        try:
            path = self.resolve_file_id(file_id)
        except ExternalError:
            return None
        row = self.file_row(file_id)
        mime = ""
        if row is not None:
            mime = (row.get("mime_type") or "").strip()
        return {
            "type": attachment_type_for_path(path, mime=mime),
            "file_id": file_id,
        }

    def _attachments_from_refs(self, refs):
        out = []
        for item in refs:
            ref = item.strip()
            if not ref:
                continue
            if is_file_id(ref):
                attachment = self._attachment_from_file_id(ref)
            elif os.path.isfile(ref):
                attachment = self._attachment_from_path(ref)
            else:
                attachment = None
            if attachment:
                out.append(attachment)
        return out

    def _collect_station_outbound_attachments(self):
        folder = self._station_outbound_dir()
        if not os.path.isdir(folder):
            return [], []
        attachments = []
        paths = []
        for name in sorted(os.listdir(folder)):
            if name.startswith("."):
                continue
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            attachment = self._attachment_from_path(path)
            if not attachment:
                continue
            attachments.append(attachment)
            paths.append(path)
        return attachments, paths

    async def _drain_station_outbound(self, *, chat_id, acct_id, platform="", chat_type=""):
        attachments, paths = self._collect_station_outbound_attachments()
        if not attachments:
            return False
        ok = await self.send_outbound(
            attachments=attachments,
            chat_id=chat_id,
            acct_id=acct_id,
            platform=platform,
            chat_type=chat_type)
        for path in paths:
            with contextlib.suppress(OSError):
                os.remove(path)
        return ok
