import importlib

KIND_ENTRYPOINTS = {
    "station": "station.prototypes.prototype:Prototype",
    "hermes": "station.prototypes.hermes.prototype:HermesPrototype",
    "hermes-d": "hermes_d.prototype:HermesDPrototype",
    "grok": "station.prototypes.grok.prototype:GrokPrototype",
    "opencode": "station.prototypes.opencode.prototype:OpenCodePrototype",
    "codex": "codex_bridge_prototype.prototype:CodexBridgePrototype",
    "pi": "station.prototypes.pi.prototype:PiPrototype",
}

_class_cache = {}

def register_kind(kind, cls):
    kind = kind.strip()
    _class_cache[kind] = cls
    return cls

def resolve_prototype_class(kind):
    cached = _class_cache.get(kind)
    if cached is not None:
        return cached
    entry = KIND_ENTRYPOINTS.get(kind)
    if not entry:
        raise ValueError("Unknown prototype kind: %s" % kind)
    module_name, _, attr = entry.partition(":")
    if not module_name or not attr:
        raise ValueError("Invalid kind entrypoint for %s: %s" % (kind, entry))
    module = importlib.import_module(module_name)
    cls = module.__dict__[attr]
    _class_cache[kind] = cls
    return cls
