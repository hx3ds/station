from __future__ import annotations

from dataclasses import dataclass, field

from station.client.context import ClientContext
from station.errors import ExternalError, InternalError
from station.prototypes.registry import resolve_prototype_class
from station.tenants.token_state import TokenState

@dataclass
class Tenant:
    id: int
    kind: str
    token_state: TokenState
    client_context: ClientContext | None = None
    config_file: str | None = None
    secret_file: str | None = None
    ava: bool = False
    reply_to: bool = False
    is_local: bool = False
    _prototype_class: type | None = field(default=None, repr=False)

    @property
    def token(self) -> str:
        return self.token_state.token

    @property
    def prototype_class(self) -> type:
        if self._prototype_class is not None:
            return self._prototype_class
        self._prototype_class = resolve_prototype_class(self.kind)
        return self._prototype_class

class TenantRegistry:
    def __init__(self):
        self._by_id: dict[int, Tenant] = {}
        self._by_token: dict[str, Tenant] = {}
        self._primary_id: int | None = None
        self._order: list[int] = []

    @property
    def primary(self) -> Tenant:
        if self._primary_id is None:
            raise InternalError("TenantRegistry has no primary tenant")
        tenant = self._by_id.get(self._primary_id)
        if tenant is None:
            raise InternalError("TenantRegistry primary tenant missing")
        return tenant

    @property
    def prototype_ids(self) -> list[int]:
        return list(self._order)

    def get(self, prototype_id: int) -> Tenant | None:
        return self._by_id.get(prototype_id)

    def hosts(self, prototype_id: int) -> bool:
        return self.get(prototype_id) is not None

    def authorize(self, header_token: str) -> Tenant | None:
        header_token = (header_token or "").strip()
        if not header_token:
            return None
        tenant = self._by_token.get(header_token)
        if tenant is not None and tenant.token_state.is_authorized(header_token):
            return tenant
        for candidate in self._by_id.values():
            if candidate.token_state.is_authorized(header_token):
                self._by_token[header_token] = candidate
                return candidate
        return None

    def _index_token(self, tenant: Tenant, token: str | None = None) -> None:
        token = (token if token is not None else tenant.token or "").strip()
        if token:
            self._by_token[token] = tenant

    def _drop_token(self, token: str | None) -> None:
        token = (token or "").strip()
        if token:
            self._by_token.pop(token, None)

    def add(self, tenant: Tenant, *, primary: bool = False) -> Tenant:
        if tenant.id is None:
            raise ExternalError("tenant.id is required")
        pid = tenant.id
        if pid in self._by_id:
            raise ExternalError("Duplicate prototype_id: %s" % pid, status=409)
        token = (tenant.token or "").strip()
        if not token:
            raise ExternalError("prototype_id=%s missing token" % pid)
        existing = self._by_token.get(token)
        if existing is not None and existing.id != pid:
            raise ExternalError("Duplicate prototype token for prototype_id=%s" % pid, status=409)
        self._by_id[pid] = tenant
        self._index_token(tenant, token)
        if pid not in self._order:
            self._order.append(pid)
        if primary or self._primary_id is None:
            self._primary_id = pid
        return tenant

    async def rotate_token(
        self,
        prototype_id: int | None,
        new_token: str,
        *,
        grace_seconds: int = 300,
    ) -> Tenant:
        if prototype_id is None:
            tenant = self.primary
        else:
            tenant = self.get(prototype_id)
            if tenant is None:
                raise ExternalError(f"prototype_id not hosted: {prototype_id}", status=404)
        old_token = tenant.token
        old_previous = tenant.token_state.previous_token
        await tenant.token_state.rotate_token(new_token, grace_seconds=grace_seconds)
        self._drop_token(old_token)
        if old_previous and old_previous != old_token:
            self._drop_token(old_previous)
        self._index_token(tenant, tenant.token)
        if tenant.token_state.previous_token:
            self._index_token(tenant, tenant.token_state.previous_token)
        if tenant.client_context is not None:
            tenant.client_context.token = tenant.token
        return tenant
