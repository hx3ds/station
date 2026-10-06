import shutil
from dataclasses import dataclass
from pathlib import Path

from station.prototypes.boundary import ext_mapping_get
from station.prototypes.launch_settings import (
    collect_provider_env,
    first_existing_command,
    launch_sections,
    merge_launch_settings,
    parse_command_args,
    split_provider_model,
    voice_settings_from_mapping,
)

PI_ROOT = Path(__file__).resolve().parents[5] / "pi"
PI_CODING_AGENT = PI_ROOT / "packages" / "coding-agent"

def _resolve_pi_command(pi_section):
    explicit = ext_mapping_get(pi_section, "command", (list,), None, allow_none=True)
    if explicit:
        parsed = parse_command_args(explicit, "pi.command")
        if parsed:
            return parsed

    tsx = PI_ROOT / "node_modules" / ".bin" / "tsx"
    cli_ts = PI_CODING_AGENT / "src" / "cli.ts"
    node = shutil.which("node")
    bash = shutil.which("bash")
    pi_bin = shutil.which("pi")
    found = first_existing_command(
        [
            [str(tsx), "--tsconfig", str(PI_ROOT / "tsconfig.json"), str(cli_ts)] if tsx.exists() and cli_ts.exists() else None,
            [node, str(PI_CODING_AGENT / "dist/bundle/cli.js")] if node and (PI_CODING_AGENT / "dist/bundle/cli.js").exists() else None,
            [node, str(PI_CODING_AGENT / "dist/cli.js")] if node and (PI_CODING_AGENT / "dist/cli.js").exists() else None,
            [bash, str(PI_ROOT / "pi-test.sh")] if bash and (PI_ROOT / "pi-test.sh").exists() else None,
            [pi_bin] if pi_bin else None,
        ]
    )
    if found:
        return found

    raise RuntimeError(
        "Pi binary not found. Build the pi monorepo, install the pi CLI, "
        "or set pi.command."
    )

@dataclass(slots=True)
class PiLaunchSettings:
    command: list
    command_cwd: Path
    workspace_dir: str
    session_root: Path
    agent_home: Path
    model: str
    provider: str
    thinking_level: str
    extra_env: dict
    rpc_args: list
    no_session: bool
    voice: dict
    voice_reply_mode: str
    api_key: str
    local_llm_base_url: str
    local_llm_api_key: str
    base_url: str

    @classmethod
    def from_model_settings(
        cls,
        model_settings,
        *,
        default_workspace,
        default_session_root,
        default_agent_home,
        config_file=None,
        secret_file=None,
        overlay=None,
    ):
        raw = merge_launch_settings(
            model_settings or {},
            config_file=config_file,
            secret_file=secret_file,
            overlay=overlay,
        )

        sections = launch_sections(raw, "pi", "model", "workspace", "keys", "voice")
        pi_section = sections["pi"]
        model = sections["model"]
        workspace = sections["workspace"]
        keys = sections["keys"]
        voice = sections["voice"]
        voice_reply_mode = voice_settings_from_mapping(voice)

        workspace_dir = (
            ext_mapping_get(workspace, "dir", (str,), "").strip() or default_workspace
        )
        workspace_path = Path(workspace_dir).expanduser()

        session_root = Path(
            ext_mapping_get(pi_section, "session_root", (str,), "").strip() or default_session_root
        ).expanduser()
        agent_home = Path(
            ext_mapping_get(pi_section, "home", (str,), "").strip() or default_agent_home
        ).expanduser()

        provider, model_id = split_provider_model(
            ext_mapping_get(model, "model", (str,), "").strip(),
            ext_mapping_get(model, "provider", (str,), "").strip(),
        )

        rpc_args = parse_command_args(
            ext_mapping_get(pi_section, "rpc_args", (list,), None, allow_none=True),
            "pi.rpc_args",
        )
        no_session = ext_mapping_get(pi_section, "no_session", (bool,), False)

        if workspace_path.is_dir():
            command_cwd = workspace_path
        elif PI_ROOT.exists():
            command_cwd = PI_ROOT
        else:
            command_cwd = Path.cwd()

        return cls(
            command=_resolve_pi_command(pi_section),
            command_cwd=command_cwd,
            workspace_dir=str(workspace_path),
            session_root=session_root,
            agent_home=agent_home,
            model=model_id,
            provider=provider,
            thinking_level=ext_mapping_get(model, "thinking_level", (str,), "").strip(),
            extra_env=collect_provider_env(keys),
            rpc_args=rpc_args,
            no_session=no_session,
            voice=dict(voice),
            voice_reply_mode=voice_reply_mode,
            api_key="",
            local_llm_base_url="",
            local_llm_api_key="",
            base_url="",
        )
