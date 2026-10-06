from dataclasses import dataclass

from station.errors import ExternalError
from station.prototypes.boundary import ext_mapping_get
from station.prototypes.launch_settings import launch_sections, merge_launch_settings, voice_settings_from_mapping

@dataclass(slots=True)
class GrokLaunchSettings:
    model: str
    base_url: str
    api_key: str
    system_prompt: str
    reuse_grok_cli_auth: bool
    temperature: float | None
    max_tokens: int | None
    voice_reply_mode: str
    tts_voice_id: str
    tts_language: str
    realtime_model: str
    realtime_voice: str
    realtime_sample_rate: int
    image_model: str
    image_aspect_ratio: str
    image_resolution: str
    video_model: str
    video_duration: int
    video_aspect_ratio: str
    video_resolution: str
    provider: str
    extra_env: dict
    voice: dict
    local_llm_base_url: str
    local_llm_api_key: str

    @classmethod
    def from_model_settings(cls, model_settings, *, config_file=None, secret_file=None, overlay=None):
        raw = merge_launch_settings(
            model_settings,
            config_file=config_file,
            secret_file=secret_file,
            overlay=overlay,
        )

        sections = launch_sections(raw, "model", "oauth", "sampling", "voice", "realtime", "image", "video")
        model = sections["model"]
        oauth = sections["oauth"]
        sampling = sections["sampling"]
        voice = sections["voice"]
        realtime = sections["realtime"]
        image = sections["image"]
        video = sections["video"]

        voice_reply_mode = voice_settings_from_mapping(voice, reply_mode_default="voice_only")

        sample_rate = ext_mapping_get(realtime, "sample_rate", (int,), 24000)
        if sample_rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
            raise ExternalError("realtime.sample_rate must be a supported PCM rate")

        tts_voice_id = ext_mapping_get(voice, "tts_voice_id", (str,), "eve")
        realtime_voice = ext_mapping_get(realtime, "voice", (str,), tts_voice_id)

        return cls(
            model=ext_mapping_get(model, "model", (str,), "grok-4.5"),
            base_url=ext_mapping_get(model, "base_url", (str,), "https://api.x.ai/v1").rstrip("/"),
            api_key=ext_mapping_get(model, "api_key", (str,), ""),
            system_prompt=ext_mapping_get(model, "system_prompt", (str,), ""),
            reuse_grok_cli_auth=ext_mapping_get(oauth, "reuse_grok_cli_auth", (bool,), False),
            temperature=ext_mapping_get(sampling, "temperature", (int, float), None, allow_none=True),
            max_tokens=ext_mapping_get(sampling, "max_tokens", (int,), None, allow_none=True),
            voice_reply_mode=voice_reply_mode,
            tts_voice_id=tts_voice_id,
            tts_language=ext_mapping_get(voice, "tts_language", (str,), "en"),
            realtime_model=ext_mapping_get(realtime, "model", (str,), "grok-voice-latest"),
            realtime_voice=realtime_voice,
            realtime_sample_rate=sample_rate,
            image_model=ext_mapping_get(image, "model", (str,), "grok-imagine-image"),
            image_aspect_ratio=ext_mapping_get(image, "aspect_ratio", (str,), ""),
            image_resolution=ext_mapping_get(image, "resolution", (str,), ""),
            video_model=ext_mapping_get(video, "model", (str,), "grok-imagine-video"),
            video_duration=ext_mapping_get(video, "duration", (int,), 8),
            video_aspect_ratio=ext_mapping_get(video, "aspect_ratio", (str,), "16:9"),
            video_resolution=ext_mapping_get(video, "resolution", (str,), "720p"),
            provider="",
            extra_env={},
            voice=dict(voice),
            local_llm_base_url="",
            local_llm_api_key="",
        )
