import os
import shlex
from urllib.parse import urlparse

from station.config.config import load_optional_toml, merge_nested
from station.prototypes.boundary import ext_list, ext_mapping_get, ext_require, parse_env_int
from station.prototypes.voice_policy import parse_voice_delivery_method, parse_voice_reply_mode

LOCAL_LLM_PROVIDERS = {
    "custom",
    "local",
    "local_llm",
    "local-llm",
    "ollama",
    "vllm",
    "llamacpp",
    "lmstudio",
    "mlx",
}

XAI_PROVIDERS = {"xai", "grok", "xai-oauth", "grok-oauth"}

PROVIDER_ENV_KEYS = {
    "openai_api_key": "OPENAI_API_KEY",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "groq_api_key": "GROQ_API_KEY",
    "xai_api_key": "XAI_API_KEY",
    "xai_base_url": "XAI_BASE_URL",
    "mistral_api_key": "MISTRAL_API_KEY",
    "google_api_key": "GOOGLE_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
    "local_llm_api_key": "LOCAL_LLM_API_KEY",
    "local_llm_base_url": "LOCAL_LLM_BASE_URL",
}

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def apply_local_provider_env(env, *, base_url=""):
    cleaned = dict(env)
    for key in _PROXY_ENV_KEYS:
        cleaned.pop(key, None)
    hosts = ["127.0.0.1", "localhost", "::1"]
    text = (base_url or "").strip()
    if text:
        parsed = urlparse(text if "://" in text else "http://%s" % text)
        if parsed.hostname:
            hosts.append(parsed.hostname)
            if parsed.port:
                hosts.append("%s:%s" % (parsed.hostname, parsed.port))
    existing = cleaned.get("NO_PROXY") or cleaned.get("no_proxy") or ""
    parts = [item.strip() for item in existing.split(",") if item.strip()]
    for host in hosts:
        if host not in parts:
            parts.append(host)
    no_proxy = ",".join(parts)
    cleaned["NO_PROXY"] = no_proxy
    cleaned["no_proxy"] = no_proxy
    return cleaned


def normalize_openai_base_url(url):
    text = (url or "").strip().rstrip("/")
    if not text:
        return ""
    if text.endswith("/v1"):
        return text
    return text + "/v1"

def merge_launch_settings(model_settings, *, config_file=None):
    ext_require("model_settings", model_settings, (dict,))
    return merge_nested(load_optional_toml(config_file, "prototype config file"), model_settings)

def collect_env_section(env_section):
    extra_env = {}
    for env_key, env_value in env_section.items():
        ext_require("env.%s" % env_key, env_value, (str,))
        if env_key.strip() and env_value.strip():
            extra_env[env_key.strip()] = env_value
    return extra_env

def voice_settings_from_mapping(voice, *, reply_mode_default="", delivery_method_default="voice"):
    return (
        parse_voice_reply_mode(ext_mapping_get(voice, "reply_mode", (str,), reply_mode_default)),
        parse_voice_delivery_method(
            ext_mapping_get(voice, "delivery_method", (str,), delivery_method_default),
            default=delivery_method_default or "voice",
        ),
    )

def env_str(name):
    return os.getenv(name, "").strip()

def env_int(name, default=0):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return parse_env_int(name, raw)

def parse_command_args(value, label):

    if value is None:
        return []
    if type(value) is str:
        if not value.strip():
            return []
        return shlex.split(value)
    items = ext_list(label, value)
    args = []
    for item in items:
        ext_require("%s item" % label, item, (str,))
        if item.strip():
            args.append(item.strip())
    return args

def collect_provider_env(keys, env_section, *, env_keys=None):
    extra_env = collect_env_section(env_section)
    mapping = env_keys if env_keys is not None else PROVIDER_ENV_KEYS
    for source_key, target_key in mapping.items():
        value = ext_mapping_get(keys, source_key, (str,), "")
        if value.strip():
            extra_env[target_key] = value.strip()
    return extra_env

def split_provider_model(model_id, provider=""):
    model_id = (model_id or "").strip()
    provider = (provider or "").strip()
    if not provider and "/" in model_id:
        provider, model_id = model_id.split("/", 1)
    return provider, model_id

def launch_sections(raw, *names):
    return {name: ext_mapping_get(raw, name, (dict,), {}) for name in names}

def uses_xai(provider, model="", *, local_llm=False):
    if local_llm:
        return False
    if (provider or "").strip().lower() in XAI_PROVIDERS:
        return True
    return (model or "").strip().lower().startswith("grok")

def resolve_local_llm(*, model, keys, local_llm, extra_env, provider, empty_provider="", max_output_default=0):
    base_url = normalize_openai_base_url(
        ext_mapping_get(local_llm, "base_url", (str,), "")
        or ext_mapping_get(model, "base_url", (str,), "")
        or env_str("LOCAL_LLM_BASE_URL")
    )
    api_key = (
        ext_mapping_get(keys, "local_llm_api_key", (str,), "")
        or ext_mapping_get(local_llm, "api_key", (str,), "")
        or ext_mapping_get(model, "api_key", (str,), "")
        or env_str("LOCAL_LLM_API_KEY")
    )
    if not (provider or "").strip() and base_url and empty_provider:
        provider = empty_provider
    uses = bool(base_url) or (provider or "").strip().lower() in LOCAL_LLM_PROVIDERS
    if uses:
        api_key = api_key or "local"
        extra_env.setdefault("LOCAL_LLM_API_KEY", api_key)
        extra_env.setdefault("OPENAI_API_KEY", api_key)
        if base_url:
            extra_env.setdefault("LOCAL_LLM_BASE_URL", base_url)
    return (
        provider,
        base_url,
        api_key,
        uses,
        ext_mapping_get(local_llm, "context_window", (int,), 0),
        ext_mapping_get(local_llm, "max_output", (int,), max_output_default),
    )

def first_existing_command(candidates):
    for item in candidates:
        if not item:
            continue
        command = [item] if type(item) is str else list(item)
        if not command:
            continue
        exe = command[0]
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            return command
    return None
