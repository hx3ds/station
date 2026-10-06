import contextlib
import os
import shutil
import subprocess
import tempfile

from station.errors import ExternalError

VOICE_REPLY_MODES = frozenset({"voice_only", "all", "off"})

def parse_voice_reply_mode(value, *, default="voice_only"):
    mode = value.strip().lower() or default
    if mode not in VOICE_REPLY_MODES:
        raise ExternalError("voice.reply_mode must be voice_only, all, or off")
    return mode

def should_send_voice_reply(*, reply_mode, incoming_had_audio):
    if reply_mode == "all":
        return True
    if reply_mode == "off":
        return False
    return bool(incoming_had_audio)

def transcode_audio_to_ogg_opus(audio_bytes):
    if not audio_bytes:
        return b""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return b""
    src = ""
    dst = ""
    try:
        fd, src = tempfile.mkstemp(prefix="voice_src_", suffix=".bin")
        os.close(fd)
        with open(src, "wb") as f:
            f.write(audio_bytes)
        fd, dst = tempfile.mkstemp(prefix="voice_transcode_", suffix=".ogg")
        os.close(fd)
        result = subprocess.run(
            [
                ffmpeg,
                "-v",
                "error",
                "-y",
                "-i",
                src,
                "-acodec",
                "libopus",
                "-ac",
                "1",
                "-b:a",
                "32k",
                "-vbr",
                "on",
                "-application",
                "voip",
                "-compression_level",
                "10",
                dst,
            ],
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
        if result.returncode == 0 and os.path.getsize(dst) > 0:
            with open(dst, "rb") as f:
                return f.read()
        return b""
    except (OSError, subprocess.SubprocessError, ValueError):
        return b""
    finally:
        for path in (src, dst):
            if path:
                with contextlib.suppress(OSError):
                    os.unlink(path)

def is_ogg_opus_audio(audio_bytes, content_type=""):
    mime = (content_type or "").split(";")[0].strip().lower()
    if "ogg" in mime or mime == "audio/opus":
        return True
    return bool(audio_bytes) and audio_bytes[:4] == b"OggS"
