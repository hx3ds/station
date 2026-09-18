import abc

class AsyncDatabase(abc.ABC):
    @abc.abstractmethod
    async def put_model(self, model_id: str, data: dict) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_model(self, model_id: str) -> dict | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def update_model_conductor_addr(self, model_id: str, conductor_addr: str) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_model(self, model_id: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def count_models(self) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_or_fetch_model(self, model_id: str, session, consul_url: str, token: str) -> dict | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def put_prototype(self, data: dict) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_prototype_info(self, prototype_id: int) -> dict | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def is_duplicate_request(self, key: str) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def remember_request(self, key: str, ttl: int = 300) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def cleanup_requests(self, ttl: int = 3600) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def put_inbound_pending(self, *, job_id: str, model_id: str, kind: str, payload: dict) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_inbound_pending(self, job_id: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_inbound_pending(self, model_id: str | None = None) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_local_conductor_state_for_model(self, model_id: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
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
        raise NotImplementedError

    @abc.abstractmethod
    async def get_local_account(self, acct_id: str) -> dict | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_local_accounts(self) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_local_account(self, acct_id: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def replace_model_chats(self, *, model_id: str, prototype_id: int | None, chats: list[dict]) -> None:
        raise NotImplementedError

    @abc.abstractmethod
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
        raise NotImplementedError

    @abc.abstractmethod
    async def list_model_chats(self, *, model_id: str, acct_id: str | None = None) -> list[dict]:
        raise NotImplementedError

    @abc.abstractmethod
    async def lookup_model_for_chat(self, *, acct_id: str, chat_id: str) -> str | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def remove_chat(self, *, model_id: str, chat_id: str, acct_id: str) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_local_account_runtime_state(
        self,
        *,
        acct_id: str,
        acct_type: str,
        state_key: str,
    ) -> dict | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def set_local_account_runtime_state(
        self,
        *,
        acct_id: str,
        acct_type: str,
        state_key: str,
        state: dict,
    ) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def close(self) -> None:
        raise NotImplementedError

