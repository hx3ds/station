from dataclasses import dataclass

from station.config.config import mapping_get
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
    voice_delivery_method: str
    tts_voice_id: str
    tts_language: str
    realtime_model: str
    realtime_voice: str
    realtime_sample_rate: int
    image_model: str
    image_aspect_ratio: str
    image_resolution: str

    @classmethod
    def from_model_settings(cls, model_settings, *, config_file=None):
        raw = merge_launch_settings(model_settings, config_file=config_file)

        sections = launch_sections(raw, "model", "oauth", "sampling", "voice", "realtime", "image")
        model = sections["model"]
        oauth = sections["oauth"]
        sampling = sections["sampling"]
        voice = sections["voice"]
        realtime = sections["realtime"]
        image = sections["image"]

        voice_reply_mode, voice_delivery_method = voice_settings_from_mapping(
            voice,
            reply_mode_default="voice_only",
            delivery_method_default="audio",
        )

        sample_rate = mapping_get(realtime, "sample_rate", (int,), 24000)
        if sample_rate not in {8000, 16000, 22050, 24000, 32000, 44100, 48000}:
            raise ValueError("realtime.sample_rate must be a supported PCM rate")

        tts_voice_id = mapping_get(voice, "tts_voice_id", (str,), "eve")
        realtime_voice = mapping_get(realtime, "voice", (str,), tts_voice_id)

        return cls(
            model=mapping_get(model, "model", (str,), "grok-4.5"),
            base_url=mapping_get(model, "base_url", (str,), "https://api.x.ai/v1").rstrip("/"),
            api_key=mapping_get(model, "api_key", (str,), ""),
            system_prompt=mapping_get(model, "system_prompt", (str,), ""),
            reuse_grok_cli_auth=mapping_get(oauth, "reuse_grok_cli_auth", (bool,), True),
            temperature=mapping_get(sampling, "temperature", (int, float), None, allow_none=True),
            max_tokens=mapping_get(sampling, "max_tokens", (int,), None, allow_none=True),
            voice_reply_mode=voice_reply_mode,
            voice_delivery_method=voice_delivery_method,
            tts_voice_id=tts_voice_id,
            tts_language=mapping_get(voice, "tts_language", (str,), "en"),
            realtime_model=mapping_get(realtime, "model", (str,), "grok-voice-latest"),
            realtime_voice=realtime_voice,
            realtime_sample_rate=sample_rate,
            image_model=mapping_get(image, "model", (str,), "grok-imagine-image"),
            image_aspect_ratio=mapping_get(image, "aspect_ratio", (str,), ""),
            image_resolution=mapping_get(image, "resolution", (str,), ""),
        )
