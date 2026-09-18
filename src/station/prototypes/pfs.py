import os

from .filesystem import BoundFileSystem
from .fs_paths import model_dir, prototype_dir, prototype_storage_dir

class PrototypeFS:
    @property
    def fs(self):
        if self._fs is None:
            self._fs = BoundFileSystem(
                self.app,
                prototype_id=self.prototype_id,
                model_id=self.model_id,
            )
        return self._fs

    @property
    def fs_root(self):
        return self.app.get("fs_root") or ""

    @property
    def prototype_dir(self):
        p = prototype_dir(self.fs_root, self.prototype_id)
        os.makedirs(p, exist_ok=True)
        return p

    @property
    def prototype_storage_dir(self):
        p = prototype_storage_dir(self.fs_root, self.prototype_id)
        os.makedirs(p, exist_ok=True)
        return p

    @property
    def storage_dir(self):
        p = model_dir(self.fs_root, self.prototype_id, self.model_id)
        os.makedirs(p, exist_ok=True)
        return p

    def resolve_file_id(self, file_id):
        return self.fs.resolve_file_id(file_id)

    def register_file(self, **kwargs):
        return self.fs.register_file(**kwargs)

    def list_downloads(self, **kwargs):
        return self.fs.list_downloads(**kwargs)

    def check_file(self, file_id):
        return self.fs.check_file(file_id)

    def save_temp(self, **kwargs):
        return self.fs.save_temp(**kwargs)

    def place_server_file(self, **kwargs):
        return self.fs.place_server_file(**kwargs)

    def rewrite_inbound_attachments(self, body, *, chat_id=None, acct_id=None):
        return self.fs.rewrite_inbound_attachments(body, chat_id=chat_id, acct_id=acct_id)

    def remote_file_id_for(self, file_id):
        return self.fs.remote_file_id_for(file_id)

    def file_row(self, file_id, *, include_remote=False):
        return self.fs.file_row(file_id, include_remote=include_remote)

    async def download_url(self, *, url, timeout_seconds=60, info=None):
        return await self.fs.download_url(url=url, timeout_seconds=timeout_seconds, info=info)

    async def download_chat_file(self, *, file_id, info=None, max_attempts=3):
        return await self.fs.download_chat_file(
            file_id=file_id,
            info=info,
            client_context=self.client_context,
            token=self.token,
            max_attempts=max_attempts,
        )
