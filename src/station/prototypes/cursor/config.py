import os
from dataclasses import dataclass

from station.prototypes.boundary import ext_mapping_get
from station.prototypes.launch_settings import (
    collect_provider_env,
    launch_sections,
    merge_launch_settings,
    split_provider_model,
    voice_settings_from_mapping,
)

DEFAULT_MODEL = "composer-2.5"


@dataclass(slots=True)
class CursorLaunchSettings:
    workspace_dir: str
    model: str
    api_key: str
    state_root: str
    extra_env: dict
    voice: dict
    voice_reply_mode: str
    provider: str
    local_llm_base_url: str
    local_llm_api_key: str
    base_url: str

    @classmethod
    def from_model_settings(
        cls,
        model_settings,
        *,
        default_workspace,
        default_state_root,
        config_file=None,
        secret_file=None,
        workspace_override=None,
        overlay=None,
    ):
        raw = merge_launch_settings(
            model_settings,
            config_file=config_file,
            secret_file=secret_file,
            overlay=overlay,
        )
        sections = launch_sections(raw, "model", "workspace", "keys", "cursor", "voice")
        model = sections["model"]
        workspace = sections["workspace"]
        keys = sections["keys"]
        cursor = sections["cursor"]
        voice = sections["voice"]
        voice_reply_mode = voice_settings_from_mapping(voice)

        workspace_dir = (
            (workspace_override.strip() if workspace_override else "")
            or ext_mapping_get(workspace, "dir", (str,), "")
            or default_workspace
        )
        workspace_path = os.path.expanduser(workspace_dir)

        _, model_id = split_provider_model(
            ext_mapping_get(model, "model", (str,), "") or DEFAULT_MODEL,
            ext_mapping_get(model, "provider", (str,), ""),
        )
        if not model_id:
            model_id = DEFAULT_MODEL

        api_key = (
            ext_mapping_get(keys, "cursor_api_key", (str,), "")
            or ext_mapping_get(model, "api_key", (str,), "")
        )

        state_root = ext_mapping_get(cursor, "state_root", (str,), "") or default_state_root

        extra_env = collect_provider_env(keys)
        if api_key:
            extra_env.setdefault("CURSOR_API_KEY", api_key)
        return cls(
            workspace_dir=workspace_path,
            model=model_id,
            api_key=api_key,
            state_root=os.path.expanduser(state_root),
            extra_env=extra_env,
            voice=dict(voice),
            voice_reply_mode=voice_reply_mode,
            provider="",
            local_llm_base_url="",
            local_llm_api_key="",
            base_url="",
        )
