import json
import sys

from station.prototypes.boundary import ext_dict, ext_mapping_get, ext_str

def _read_payload():

    raw = sys.stdin.read()
    if not raw.strip():
        raise RuntimeError("speech runtime stdin payload is required")
    return ext_dict("speech payload", json.loads(raw))

def _write_payload(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")

def _transcribe(payload):
    from tools.transcription_tools import transcribe_audio

    file_path = ext_mapping_get(payload, "file_path", (str,), "").strip()
    if not file_path:
        return {"success": False, "error": "file_path is required", "transcript": ""}

    return ext_dict("transcription result", transcribe_audio(file_path))

def _synthesize(payload):
    from tools.tts_tool import text_to_speech_tool

    text = ext_mapping_get(payload, "text", (str,), "").strip()
    output_path = ext_mapping_get(payload, "output_path", (str,), "").strip()
    if not text:
        return {"success": False, "error": "text is required"}
    if not output_path:
        return {"success": False, "error": "output_path is required"}

    raw = text_to_speech_tool(text=text, output_path=output_path)
    if type(raw) is dict:
        return raw
    raw = ext_str("tts result", raw).strip()
    if not raw:
        return {"success": False, "error": "tts returned empty result"}
    return ext_dict("tts result", json.loads(raw))

def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m station.prototypes.hermes.speech_runtime <transcribe|synthesize>")

    command = sys.argv[1].strip().lower()
    payload = _read_payload()
    if command == "transcribe":
        _write_payload(_transcribe(payload))
        return
    if command == "synthesize":
        _write_payload(_synthesize(payload))
        return
    raise SystemExit("unknown command: %s" % command)

if __name__ == "__main__":
    main()
