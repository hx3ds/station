from abc import ABC

from aiohttp import web

class LocalPlatformAdapter(ABC):
    acct_type: str
    capabilities: frozenset[str] = frozenset()

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def ensure_account_ready(self, acct_id: str) -> bool:
        return True

    async def poller_loop(self, acct_id: str) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    def register_routes(self, app: web.Application) -> None:
        return None
