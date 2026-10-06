import asyncio
import base64
import json
import uuid

import aiohttp

from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_float, ext_list, ext_str
from station.prototypes.chat_history import prefix_content
from station.prototypes.grok.tools import CLIENT_TOOL_NAMES, parse_tool_calls

def _extract_message_content_text(content):
    if type(content) is str:
        return content.strip()
    content = ext_list("message content", content)
    parts = []
    for item in content:
        item = ext_dict("message content item", item)
        if item.get("type") in ("text", "output_text"):
            text = item.get("text")
            if text is None:
                continue
            text = ext_str("message content text", text)
            if text.strip():
                parts.append(text.strip())
    return "\n".join(parts).strip()

def _format_request_error(exc):
    parts = [type(exc).__name__]
    msg = str(exc).strip()
    if msg:
        parts.append(msg)
    cause = exc.__cause__ or exc.__context__
    if cause is not None and cause is not exc:
        parts.append("cause=%s: %s" % (type(cause).__name__, cause))
    return " | ".join(parts)

def _is_retryable_transport_error(exc):
    return isinstance(
        exc,
        (
            aiohttp.ClientConnectorError,
            aiohttp.ClientConnectorSSLError,
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientOSError,
            asyncio.TimeoutError,
            ConnectionResetError,
            ConnectionError,
            TimeoutError,
        ),
    )

def _auth_headers(token, *, content_type="application/json"):
    headers = {
        "Authorization": "Bearer %s" % token,
        "Accept": "application/json",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers

async def _open_session(session, timeout):
    if session is not None and not session.closed:
        return session, False
    active = aiohttp.ClientSession(
        timeout=timeout,
        trust_env=True,
        connector=aiohttp.TCPConnector(ssl=True, force_close=True, limit=8),
    )
    return active, True

async def _read_json_body(resp):
    raw = await resp.text()
    content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not raw or "json" not in content_type:
        return None, raw
    return json.loads(raw), raw

def _error_message(data, raw, resp):
    def _fallback():
        if raw is not None:
            text = ext_str("error raw body", raw, strip=False).strip()
            if text:
                return text
        reason = resp.reason
        if reason is None:
            return "Request failed"
        text = ext_str("response reason", reason, strip=False).strip()
        return text if text else "Request failed"

    if data is None:
        return _fallback()
    data = ext_dict("error response body", data)
    err = data.get("error")
    if err is None:
        return _fallback()
    if type(err) is dict:
        msg = err.get("message")
        if msg is None:
            msg = err.get("code")
        if msg is None:
            msg = err.get("error")
        if msg is None:
            return _fallback()
        return ext_str("error message", msg, strip=False).strip()
    if type(err) is str:
        return err.strip()
    raise ExternalError("error must be dict or str")

async def complete_chat(
    *,
    session,
    settings,
    access_token,
    user_text="",
    user_content=None,
    history=None,
    sender=None,
    tools=None,
    run_tool=None,
    max_attempts=4,
):
    token = access_token or settings.api_key
    if not token:
        return "Grok not configured (missing OAuth access token or api_key)."

    content = user_text if user_content is None else user_content
    if type(content) is str:
        content = content.strip()
        if not content:
            return "Grok request failed: empty prompt."
    elif type(content) is list:
        if not content:
            return "Grok request failed: empty prompt."
    else:
        raise ExternalError("user_content must be str or list")

    messages = []
    if settings.system_prompt:
        messages.append({"role": "system", "content": settings.system_prompt})
    if history:
        history = ext_list("chat history", history)
        for index, item in enumerate(history):
            item = ext_dict("chat history[%d]" % index, item)
            role = ext_str("chat history[%d].role" % index, item.get("role"))
            if role not in {"user", "assistant"}:
                raise ExternalError("chat history[%d].role must be user or assistant" % index)
            text = item.get("content")
            if type(text) is str:
                text = text.strip()
                if not text:
                    raise ExternalError("chat history[%d].content must be non-empty" % index)
            elif type(text) is list:
                if not text:
                    raise ExternalError("chat history[%d].content must be non-empty" % index)
            else:
                raise ExternalError("chat history[%d].content must be str or list" % index)
            item_sender = item.get("sender")
            if item_sender is not None:
                item_sender = ext_dict("chat history[%d].sender" % index, item_sender)
            messages.append({"role": role, "content": prefix_content(text, item_sender)})
    messages.append({"role": "user", "content": prefix_content(content, sender)})

    url = "%s/chat/completions" % settings.base_url.rstrip("/")
    headers = _auth_headers(token)
    timeout = aiohttp.ClientTimeout(total=300, connect=20, sock_connect=20, sock_read=240)
    rounds = 0
    while True:
        payload = {
            "model": settings.model,
            "messages": messages,
        }
        if settings.temperature is not None:
            payload["temperature"] = settings.temperature
        if settings.max_tokens is not None:
            payload["max_tokens"] = settings.max_tokens
        if tools is not None:
            payload["tools"] = tools
        message, failed = await _post_chat_message(
            session=session,
            url=url,
            headers=headers,
            payload=payload,
            timeout=timeout,
            max_attempts=max_attempts,
        )
        if failed:
            return failed
        if tools is None or run_tool is None:
            text = _message_text(message)
            if text:
                return text
            return "Grok returned no content."
        try:
            calls = parse_tool_calls(message)
        except ExternalError as exc:
            return "Grok request failed: %s" % exc
        text = _message_text(message)
        if not calls:
            if text:
                return text
            return "Grok returned no content."
        client_calls = [call for call in calls if call["name"] in CLIENT_TOOL_NAMES]
        if len(client_calls) != len(calls):
            if text:
                return text
            if not client_calls:
                return "I could not complete that."
            calls = client_calls
        rounds += 1
        if rounds > 6:
            return await _finish_without_tools(
                session=session,
                url=url,
                headers=headers,
                messages=messages,
                settings=settings,
                tools=tools,
                timeout=timeout,
                max_attempts=max_attempts,
            )
        messages.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": [call["api"] for call in calls],
        })
        for call in calls:
            logger.info("grok tool name=%s", call["name"])
            output = await run_tool(call["name"], call["arguments"])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})


def _chat_payload(settings, messages, *, tools, tool_choice):
    payload = {
        "model": settings.model,
        "messages": messages,
    }
    if settings.temperature is not None:
        payload["temperature"] = settings.temperature
    if settings.max_tokens is not None:
        payload["max_tokens"] = settings.max_tokens
    if tools is not None:
        payload["tools"] = tools
    if tool_choice:
        payload["tool_choice"] = tool_choice
    return payload


def _plain_messages(messages):
    plain = []
    for item in messages:
        role = item.get("role")
        if role == "tool":
            continue
        if item.get("tool_calls"):
            text = _message_text(item)
            if text:
                plain.append({"role": "assistant", "content": text})
            continue
        plain.append(item)
    return plain


async def _finish_without_tools(*, session, url, headers, messages, settings, tools, timeout, max_attempts):
    payload = _chat_payload(settings, messages, tools=tools, tool_choice=None)
    message, failed = await _post_chat_message(
        session=session,
        url=url,
        headers=headers,
        payload=payload,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    text = "" if failed or message is None else _message_text(message)
    if text:
        return text
    payload = _chat_payload(settings, _plain_messages(messages), tools=None, tool_choice=None)
    message, failed = await _post_chat_message(
        session=session,
        url=url,
        headers=headers,
        payload=payload,
        timeout=timeout,
        max_attempts=max_attempts,
    )
    if not failed and message is not None:
        text = _message_text(message)
        if text:
            return text
    return "I could not complete that."


def _message_text(message):
    content = message.get("content")
    if content is None:
        return ""
    return _extract_message_content_text(content)


async def _post_chat_message(*, session, url, headers, payload, timeout, max_attempts):
    last_error = ""
    attempts = max(1, max_attempts)
    for attempt in range(1, attempts + 1):
        active, owns_session = await _open_session(session, timeout)
        try:
            async with active.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                data, raw = await _read_json_body(resp)
                if resp.status >= 400:
                    msg = _error_message(data, raw, resp)
                    if resp.status == 429 and attempt < attempts:
                        last_error = "%s %s" % (resp.status, msg)
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    return None, "Grok request failed: %s %s" % (resp.status, msg)
                data = ext_dict("chat completion response", data)
                choices = data.get("choices")
                choices = ext_list("choices", choices)
                if not choices:
                    return None, "Grok returned no content."
                first = choices[0]
                first = ext_dict("choice", first)
                message = first.get("message")
                message = ext_dict("choice message", message)
                return message, ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _format_request_error(exc)
            retryable = _is_retryable_transport_error(exc)
            logger.warning(
                "Grok completion attempt %d/%d failed retryable=%s error=%s",
                attempt,
                attempts,
                retryable,
                last_error,
            )
            if not retryable or attempt >= attempts:
                logger.error("Grok completion failed error=%s", last_error)
                return None, "Grok request failed: %s" % last_error
            await asyncio.sleep(0.35 * attempt)
        finally:
            if owns_session:
                await active.close()
    return None, "Grok request failed: %s" % (last_error or "error")

async def transcribe_audio(
    *,
    session,
    settings,
    access_token,
    audio_bytes,
    filename="audio.wav",
    max_attempts=3,
):
    token = access_token or settings.api_key
    if not token or not audio_bytes:
        return ""

    url = "%s/stt" % settings.base_url.rstrip("/")
    headers = _auth_headers(token, content_type=None)
    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_connect=20, sock_read=150)
    last_error = ""
    attempts = max(1, max_attempts)
    name = filename or "audio.wav"

    for attempt in range(1, attempts + 1):
        active, owns_session = await _open_session(session, timeout)
        try:
            form = aiohttp.FormData()
            form.add_field("file", audio_bytes, filename=name, content_type="application/octet-stream")
            async with active.post(url, headers=headers, data=form, timeout=timeout) as resp:
                raw = await resp.read()
                if resp.status >= 400:
                    last_error = "%s %r" % (resp.status, raw[:400])
                    if resp.status == 429 and attempt < attempts:
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    logger.warning("Grok STT failed: %s", last_error)
                    return ""
                data = json.loads(raw.decode("utf-8"))
                data = ext_dict('stt response', data)
                text = data.get("text", "")
                text = ext_str('stt text', text, strip=False)
                return text.strip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _format_request_error(exc)
            retryable = _is_retryable_transport_error(exc)
            logger.warning(
                "Grok STT attempt %d/%d failed retryable=%s error=%s",
                attempt,
                attempts,
                retryable,
                last_error,
            )
            if not retryable or attempt >= attempts:
                logger.error("Grok STT failed error=%s", last_error)
                return ""
            await asyncio.sleep(0.35 * attempt)
        finally:
            if owns_session:
                await active.close()

    return ""

async def generate_image(
    *,
    session,
    settings,
    access_token,
    prompt,
    max_attempts=3,
):
    token = access_token or settings.api_key
    text = prompt.strip()
    if not token:
        return b"", "", "Grok not configured (missing OAuth access token or api_key)."
    if not text:
        return b"", "", "Usage: /image <prompt>"

    url = "%s/images/generations" % settings.base_url.rstrip("/")
    headers = _auth_headers(token)
    payload = {
        "model": settings.image_model,
        "prompt": text,
        "n": 1,
        "response_format": "b64_json",
    }
    if settings.image_aspect_ratio:
        payload["aspect_ratio"] = settings.image_aspect_ratio
    if settings.image_resolution:
        payload["resolution"] = settings.image_resolution

    timeout = aiohttp.ClientTimeout(total=180, connect=20, sock_connect=20, sock_read=150)
    last_error = ""
    attempts = max(1, max_attempts)

    for attempt in range(1, attempts + 1):
        active, owns_session = await _open_session(session, timeout)
        try:
            async with active.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                data, raw = await _read_json_body(resp)
                if resp.status >= 400:
                    msg = _error_message(data, raw, resp)
                    if resp.status == 429 and attempt < attempts:
                        last_error = "%s %s" % (resp.status, msg)
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    return b"", "", "Grok image generation failed: %s %s" % (resp.status, msg)

                data = ext_dict('image generation response', data)
                items = data.get("data")
                try:
                    items = ext_list("image generation data", items)
                except ExternalError:
                    return b"", "", "Grok image generation returned no images."
                if not items:
                    return b"", "", "Grok image generation returned no images."
                first = ext_dict("image generation item", items[0])
                b64 = first.get("b64_json", "")
                mime = first.get("mime_type", "image/png")
                b64 = ext_str('b64_json', b64, strip=False)
                mime = ext_str('mime_type', mime, strip=False)
                mime = mime.strip() or "image/png"
                if b64:
                    return base64.b64decode(b64), mime, ""
                image_url = first.get("url", "")
                image_url = ext_str('image url', image_url, strip=False)
                image_url = image_url.strip()
                if not image_url:
                    return b"", "", "Grok image generation returned no image data."

            async with active.get(image_url, timeout=timeout) as img_resp:
                img_raw = await img_resp.read()
                if img_resp.status >= 400 or not img_raw:
                    return b"", "", "Grok image download failed: %s" % img_resp.status
                content_type = (img_resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                return img_raw, content_type or mime or "image/png", ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _format_request_error(exc)
            retryable = _is_retryable_transport_error(exc)
            logger.warning(
                "Grok image generation attempt %d/%d failed retryable=%s error=%s",
                attempt,
                attempts,
                retryable,
                last_error,
            )
            if not retryable or attempt >= attempts:
                logger.error("Grok image generation failed error=%s", last_error)
                return b"", "", "Grok image generation failed: %s" % last_error
            await asyncio.sleep(0.35 * attempt)
        finally:
            if owns_session:
                await active.close()

    return b"", "", "Grok image generation failed: %s" % (last_error or "error")

VIDEO_POLL_INTERVAL_SECONDS = 5
VIDEO_POLL_TIMEOUT_SECONDS = 240
VIDEO_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"}
VIDEO_RESOLUTIONS = {"480p", "720p"}

def _clamp_video_duration(value):
    if value is None:
        return 8
    if value < 1:
        return 1
    if value > 15:
        return 15
    return value

def _video_output_url(video):
    video = ext_dict("video generation video", video)
    file_output = video.get("file_output")
    stored = ""
    if file_output is not None:
        file_output = ext_dict("video file_output", file_output)
        public_url = file_output.get("public_url", "")
        public_url = ext_str("video public_url", public_url, strip=False)
        stored = public_url.strip()
    temporary = video.get("url", "")
    temporary = ext_str("video url", temporary, strip=False)
    temporary = temporary.strip()
    return stored or temporary

async def generate_video(
    *,
    session,
    settings,
    access_token,
    prompt,
    max_attempts=3,
):
    token = access_token or settings.api_key
    text = prompt.strip()
    if not token:
        return b"", "", "Grok not configured (missing OAuth access token or api_key)."
    if not text:
        return b"", "", "Usage: /video <prompt>"

    aspect_ratio = (settings.video_aspect_ratio or "").strip() or "16:9"
    if aspect_ratio not in VIDEO_ASPECT_RATIOS:
        aspect_ratio = "16:9"
    resolution = (settings.video_resolution or "").strip().lower() or "720p"
    if resolution not in VIDEO_RESOLUTIONS:
        resolution = "720p"
    duration = _clamp_video_duration(settings.video_duration)

    url = "%s/videos/generations" % settings.base_url.rstrip("/")
    headers = _auth_headers(token)
    headers["x-idempotency-key"] = str(uuid.uuid4())
    payload = {
        "model": settings.video_model,
        "prompt": text,
        "duration": duration,
        "aspect_ratio": aspect_ratio,
        "resolution": resolution,
    }
    timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=120)
    last_error = ""
    attempts = max(1, max_attempts)
    request_id = ""

    for attempt in range(1, attempts + 1):
        active, owns_session = await _open_session(session, timeout)
        try:
            async with active.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                data, raw = await _read_json_body(resp)
                if resp.status >= 400:
                    msg = _error_message(data, raw, resp)
                    if resp.status == 429 and attempt < attempts:
                        last_error = "%s %s" % (resp.status, msg)
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    return b"", "", "Grok video generation failed: %s %s" % (resp.status, msg)
                data = ext_dict("video generation response", data)
                request_id = data.get("request_id", "")
                request_id = ext_str("request_id", request_id, strip=False).strip()
                if not request_id:
                    return b"", "", "Grok video generation returned no request_id."
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _format_request_error(exc)
            retryable = _is_retryable_transport_error(exc)
            logger.warning(
                "Grok video generation attempt %d/%d failed retryable=%s error=%s",
                attempt,
                attempts,
                retryable,
                last_error,
            )
            if not retryable or attempt >= attempts:
                logger.error("Grok video generation failed error=%s", last_error)
                return b"", "", "Grok video generation failed: %s" % last_error
            await asyncio.sleep(0.35 * attempt)
        finally:
            if owns_session:
                await active.close()
    else:
        return b"", "", "Grok video generation failed: %s" % (last_error or "error")

    poll_url = "%s/videos/%s" % (settings.base_url.rstrip("/"), request_id)
    poll_headers = _auth_headers(token)
    elapsed = 0.0
    last_status = "queued"
    while elapsed < VIDEO_POLL_TIMEOUT_SECONDS:
        active, owns_session = await _open_session(session, timeout)
        try:
            async with active.get(poll_url, headers=poll_headers, timeout=timeout) as resp:
                data, raw = await _read_json_body(resp)
                if resp.status >= 400:
                    msg = _error_message(data, raw, resp)
                    if resp.status == 429:
                        last_error = "%s %s" % (resp.status, msg)
                        await asyncio.sleep(VIDEO_POLL_INTERVAL_SECONDS)
                        elapsed += VIDEO_POLL_INTERVAL_SECONDS
                        continue
                    return b"", "", "Grok video generation failed: %s %s" % (resp.status, msg)
                data = ext_dict("video poll response", data)
                last_status = data.get("status", "")
                last_status = ext_str("video status", last_status, strip=False).strip().lower()
                if last_status == "done":
                    video = data.get("video")
                    if video is None:
                        return b"", "", "Grok video generation returned no video."
                    video_url = _video_output_url(video)
                    if not video_url:
                        return b"", "", "Grok video generation returned no video data."
                    async with active.get(video_url, timeout=timeout) as vid_resp:
                        vid_raw = await vid_resp.read()
                        if vid_resp.status >= 400 or not vid_raw:
                            return b"", "", "Grok video download failed: %s" % vid_resp.status
                        content_type = (vid_resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                        return vid_raw, content_type or "video/mp4", ""
                if last_status in {"failed", "error", "expired", "cancelled"}:
                    err = data.get("error")
                    msg = ""
                    if type(err) is dict:
                        msg = err.get("message") or err.get("code") or ""
                        if msg is not None:
                            msg = ext_str("video error message", msg, strip=False).strip()
                    elif type(err) is str:
                        msg = err.strip()
                    if not msg:
                        fallback = data.get("message", "")
                        if fallback is not None:
                            msg = ext_str("video message", fallback, strip=False).strip()
                    return b"", "", "Grok video generation failed: %s" % (msg or last_status)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _format_request_error(exc)
            retryable = _is_retryable_transport_error(exc)
            logger.warning(
                "Grok video poll failed retryable=%s error=%s",
                retryable,
                last_error,
            )
            if not retryable:
                logger.error("Grok video generation failed error=%s", last_error)
                return b"", "", "Grok video generation failed: %s" % last_error
        finally:
            if owns_session:
                await active.close()
        await asyncio.sleep(VIDEO_POLL_INTERVAL_SECONDS)
        elapsed += VIDEO_POLL_INTERVAL_SECONDS

    return b"", "", "Grok video generation failed: timed out after %ss (status=%s)" % (
        VIDEO_POLL_TIMEOUT_SECONDS,
        last_status,
    )

GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_SETTINGS_URL = "https://cli-chat-proxy.grok.com/v1/settings"

def _money_val(obj):
    if obj is None:
        return None
    if type(obj) is dict:
        val = ext_dict("billing money value", obj).get("val")
        if val is None:
            return None
        return ext_float("billing money val", val)
    return ext_float("billing money value", obj)

def _format_usage_reply(data, *, plan_name=""):
    data = ext_dict('billing response', data)
    config = data.get("config")
    config = ext_dict('billing config', config)

    used = config.get("creditUsagePercent")
    if used is None:
        on_demand_used = _money_val(config.get("onDemandUsed"))
        on_demand_cap = _money_val(config.get("onDemandCap"))
        if on_demand_cap and on_demand_cap > 0 and on_demand_used is not None:
            used = (on_demand_used / on_demand_cap) * 100.0
        else:
            used = 0.0
    used = float(ext_float("creditUsagePercent", used))
    remaining = max(0.0, 100.0 - used)

    period = config.get("currentPeriod")
    resets = ""
    if period is not None:
        period = ext_dict('billing currentPeriod', period)
        end = period.get("end")
        if end is not None:
            end = ext_str('billing currentPeriod.end', end, strip=False)
            resets = end.strip()
    if not resets:
        end = config.get("billingPeriodEnd")
        if end is not None:
            end = ext_str('billing billingPeriodEnd', end, strip=False)
            resets = end.strip()

    lines = []
    if plan_name:
        lines.append("Plan: %s" % plan_name)
    lines.append("Grok weekly usage: %.0f%% used (%.0f%% left)" % (used, remaining))
    if resets:
        lines.append("Resets: %s" % resets)

    products = config.get("productUsage")
    if products is not None:
        products = ext_list('billing productUsage', products)
        parts = []
        for item in products:
            item = ext_dict('billing productUsage item', item)
            name = item.get("product")
            name = ext_str('billing productUsage product', name, strip=False)
            name = name.strip()
            if not name:
                continue
            pct = item.get("usagePercent")
            if pct is None:
                parts.append(name)
                continue
            pct = float(ext_float("billing usagePercent", pct))
            parts.append("%s %.0f%%" % (name, pct))
        if parts:
            lines.append("By product: %s" % ", ".join(parts))

    on_demand_used = _money_val(config.get("onDemandUsed"))
    on_demand_cap = _money_val(config.get("onDemandCap"))
    prepaid = _money_val(config.get("prepaidBalance"))
    if on_demand_used is not None or on_demand_cap is not None:
        used_s = "%.0f" % on_demand_used if on_demand_used is not None else "?"
        cap_s = "%.0f" % on_demand_cap if on_demand_cap is not None else "?"
        lines.append("Extra credits: %s / %s" % (used_s, cap_s))
    if prepaid is not None and prepaid > 0:
        lines.append("Prepaid balance: %.0f" % prepaid)
    return "\n".join(lines)

async def fetch_usage(*, session, access_token, max_attempts=3):
    token = access_token.strip() if access_token else ""
    if not token:
        return "Grok usage requires OAuth sign-in (not an API key). Use /login."

    headers = _auth_headers(token)
    headers["X-XAI-Token-Auth"] = "xai-grok-cli"
    timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_connect=10, sock_read=20)
    last_error = ""
    attempts = max(1, max_attempts)
    plan_name = ""

    for attempt in range(1, attempts + 1):
        active, owns_session = await _open_session(session, timeout)
        try:
            async with active.get(GROK_SETTINGS_URL, headers=headers, timeout=timeout) as settings_resp:
                settings_data, _ = await _read_json_body(settings_resp)
                if settings_resp.status < 400 and settings_data is not None:
                    settings_data = ext_dict('settings response', settings_data)
                    tier = settings_data.get("subscription_tier_display")
                    if tier is not None:
                        tier = ext_str('subscription_tier_display', tier, strip=False)
                    plan_name = (tier or "").strip()

            async with active.get(GROK_BILLING_URL, headers=headers, timeout=timeout) as resp:
                data, raw = await _read_json_body(resp)
                if resp.status >= 400:
                    msg = _error_message(data, raw, resp)
                    if resp.status in {401, 403}:
                        return "Grok usage failed: sign in again with /login (%s %s)" % (resp.status, msg)
                    if resp.status == 429 and attempt < attempts:
                        last_error = "%s %s" % (resp.status, msg)
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    return "Grok usage failed: %s %s" % (resp.status, msg)
                try:
                    return _format_usage_reply(data, plan_name=plan_name)
                except (TypeError, ValueError) as e:
                    return "Grok usage failed: unexpected billing payload (%s)" % e
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _format_request_error(exc)
            retryable = _is_retryable_transport_error(exc)
            logger.warning(
                "Grok usage attempt %d/%d failed retryable=%s error=%s",
                attempt,
                attempts,
                retryable,
                last_error,
            )
            if not retryable or attempt >= attempts:
                logger.error("Grok usage failed error=%s", last_error)
                return "Grok usage failed: %s" % last_error
            await asyncio.sleep(0.35 * attempt)
        finally:
            if owns_session:
                await active.close()

    return "Grok usage failed: %s" % (last_error or "error")

async def synthesize_speech(
    *,
    session,
    settings,
    access_token,
    text,
    max_attempts=3,
):
    token = access_token or settings.api_key
    prompt = text.strip()
    if not token or not prompt:
        return b"", ""

    url = "%s/tts" % settings.base_url.rstrip("/")
    headers = _auth_headers(token)
    payload = {
        "text": prompt[:15000],
        "voice_id": settings.tts_voice_id,
        "language": settings.tts_language,
        "output_format": {"codec": "mp3", "sample_rate": 24000},
    }
    timeout = aiohttp.ClientTimeout(total=120, connect=20, sock_connect=20, sock_read=90)
    last_error = ""
    attempts = max(1, max_attempts)

    for attempt in range(1, attempts + 1):
        active, owns_session = await _open_session(session, timeout)
        try:
            async with active.post(url, headers=headers, json=payload, timeout=timeout) as resp:
                raw = await resp.read()
                content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if resp.status >= 400:
                    last_error = "%s %r" % (resp.status, raw[:400])
                    if resp.status == 429 and attempt < attempts:
                        await asyncio.sleep(0.4 * attempt)
                        continue
                    logger.warning("Grok TTS failed: %s", last_error)
                    return b"", ""
                if "application/json" in content_type:
                    data = json.loads(raw.decode("utf-8"))
                    data = ext_dict('tts response', data)
                    audio_b64 = data.get("audio", "")
                    audio_b64 = ext_str('tts audio', audio_b64, strip=False)
                    if not audio_b64:
                        return b"", ""
                    audio = base64.b64decode(audio_b64)
                    mime = data.get("content_type", "audio/mpeg")
                    mime = ext_str('tts content_type', mime, strip=False)
                    return audio, mime.strip() or "audio/mpeg"
                if raw:
                    return raw, content_type or "audio/mpeg"
                return b"", ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = _format_request_error(exc)
            retryable = _is_retryable_transport_error(exc)
            logger.warning(
                "Grok TTS attempt %d/%d failed retryable=%s error=%s",
                attempt,
                attempts,
                retryable,
                last_error,
            )
            if not retryable or attempt >= attempts:
                logger.error("Grok TTS failed error=%s", last_error)
                return b"", ""
            await asyncio.sleep(0.35 * attempt)
        finally:
            if owns_session:
                await active.close()

    return b"", ""
