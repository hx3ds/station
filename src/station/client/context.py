from dataclasses import dataclass, field

@dataclass
class ClientContext:
    session: object
    consul_url: str = ""
    env: str = ""
    prototype_id: int | None = None
    token: str | None = None
    prototype_version: int | None = None
    prototype_type: str | None = None
    db: object | None = None
    conductor_addresses: dict = field(default_factory=dict)
    app: object | None = None

def update_client_context_prototype(ctx, prototype_data):
    ctx.prototype_type = prototype_data.get("type")
    ctx.prototype_version = prototype_data.get("version")
