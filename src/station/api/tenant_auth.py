from __future__ import annotations

from station.tenants import Tenant

def authorize_request(request) -> Tenant | None:
    header_token = request.headers.get("X-Prototype-Token", "")
    return request.app["tenants"].authorize(header_token)
