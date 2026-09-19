import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

from station import logger
from station.errors import ExternalError
from station.prototypes.boundary import ext_dict, ext_float, ext_str

XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = "%s/.well-known/openid-configuration" % XAI_OAUTH_ISSUER
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REFRESH_SKEW_MS = 2 * 60 * 1000

DEFAULT_TOKEN_ENDPOINT = "https://auth.x.ai/oauth2/token"
DEFAULT_DEVICE_AUTHORIZATION_ENDPOINT = "https://auth.x.ai/oauth2/device/code"
DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

DEVICE_CODE_DEFAULT_INTERVAL_MS = 5_000
DEVICE_CODE_MIN_INTERVAL_MS = 1_000
DEVICE_CODE_SLOW_DOWN_INCREMENT_MS = 5_000
DEVICE_CODE_DEFAULT_EXPIRES_MS = 5 * 60 * 1000
OAUTH_POLLING_SAFETY_MARGIN_MS = 3_000

@dataclass(slots=True)
class OAuthCredentials:
    access: str
    refresh: str
    expires: int
    token_endpoint: str = ""
    id_token: str = ""
    token_type: str = "Bearer"

    def is_fresh(self, *, now_ms=None):
        current = int(time.time() * 1000) if now_ms is None else now_ms
        return self.access and (not self.expires or self.expires > current)

@dataclass(slots=True)
class PendingDeviceLogin:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int
    token_endpoint: str
    device_authorization_endpoint: str

def _validate_xai_endpoint(url):
    url = ext_str('oauth endpoint', url, strip=False)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "x.ai" and not host.endswith(".x.ai")):
        raise ExternalError("xAI OAuth discovery returned an unexpected endpoint: %s" % url)
    return url

def _http_json(method, url, *, headers=None, body=None, timeout=30.0):
    status, data = _http_json_status(method, url, headers=headers, body=body, timeout=timeout)
    if status < 200 or status >= 300:
        detail = ""
        if type(data) is dict:
            error_description = data.get("error_description")
            error = data.get("error")
            if error_description is not None:
                error_description = ext_str('error_description', error_description, strip=False)
            if error is not None:
                error = ext_str('error', error, strip=False)
            if error_description:
                detail = error_description
            elif error:
                detail = error
        elif type(data) is str:
            detail = data
        elif data is not None:
            raise ExternalError("oauth error body must be dict or str")
        raise RuntimeError("xAI OAuth HTTP %s%s" % (status, (": " + detail) if detail else ""))
    return data

def _decode_body(headers, raw_bytes):
    raw = raw_bytes.decode("utf-8", errors="replace")
    if not raw:
        return {}
    content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if "json" not in content_type:
        return raw
    return json.loads(raw)

def _http_json_status(method, url, *, headers=None, body=None, timeout=30.0):
    req = Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode_body(resp.headers, resp.read())
    except HTTPError as exc:
        return int(exc.code), _decode_body(exc.headers, exc.read())

def _default_oauth_endpoints():
    return {
        "token_endpoint": DEFAULT_TOKEN_ENDPOINT,
        "device_authorization_endpoint": DEFAULT_DEVICE_AUTHORIZATION_ENDPOINT,
    }

def discover_xai_oauth():
    try:
        data = _http_json("GET", XAI_OAUTH_DISCOVERY_URL, headers={"Accept": "application/json"})
    except (OSError, RuntimeError) as exc:
        logger.info("xAI OAuth discovery failed (%s); using defaults", exc)
        return _default_oauth_endpoints()
    data = ext_dict('oauth discovery response', data)
    token_endpoint = data.get("token_endpoint")
    device_authorization_endpoint = data.get("device_authorization_endpoint")
    if not token_endpoint or not device_authorization_endpoint:
        logger.info("xAI OAuth discovery missing device endpoints; using defaults")
        return _default_oauth_endpoints()
    return {
        "token_endpoint": _validate_xai_endpoint(token_endpoint),
        "device_authorization_endpoint": _validate_xai_endpoint(device_authorization_endpoint),
    }

def _credentials_from_token_payload(data, token_endpoint, fallback_refresh=""):
    data = ext_dict('xAI token response', data)
    access = data.get("access_token")
    access = ext_str('access_token', access, strip=False)
    if not access:
        raise RuntimeError("xAI token response did not include an access token")
    refresh = data.get("refresh_token", fallback_refresh)
    refresh = ext_str('refresh_token', refresh, strip=False)
    if not refresh:
        raise RuntimeError("xAI token response did not include a refresh token")
    expires_in = data.get("expires_in", 3600)
    expires_in = ext_float('expires_in', expires_in)
    expires = int(time.time() * 1000) + int(expires_in) * 1000 - XAI_OAUTH_REFRESH_SKEW_MS
    id_token = data.get("id_token", "")
    token_type = data.get("token_type", "Bearer")
    id_token = ext_str('id_token', id_token, strip=False)
    token_type = ext_str('token_type', token_type, strip=False)
    return OAuthCredentials(
        access=access,
        refresh=refresh,
        expires=expires,
        token_endpoint=token_endpoint,
        id_token=id_token,
        token_type=token_type,
    )

def exchange_xai_token(token_endpoint, body):
    token_endpoint = _validate_xai_endpoint(token_endpoint)
    payload = urlencode(body).encode("utf-8")
    data = _http_json(
        "POST",
        token_endpoint,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=payload,
    )
    return _credentials_from_token_payload(data, token_endpoint, fallback_refresh=body.get("refresh_token", ""))

def refresh_xai_credentials(credentials):
    if not credentials.refresh:
        raise RuntimeError("xAI credentials are expired and do not include a refresh token")
    token_endpoint = credentials.token_endpoint
    if not token_endpoint:
        token_endpoint = discover_xai_oauth()["token_endpoint"]
    else:
        token_endpoint = _validate_xai_endpoint(token_endpoint)
    return exchange_xai_token(
        token_endpoint,
        {
            "grant_type": "refresh_token",
            "refresh_token": credentials.refresh,
            "client_id": XAI_OAUTH_CLIENT_ID,
        },
    )

def ensure_fresh_credentials(credentials):
    if credentials.is_fresh():
        return credentials
    return refresh_xai_credentials(credentials)

def _positive_seconds_to_ms(value, default_ms):
    if value <= 0:
        return default_ms
    return int(value * 1000)

def begin_device_login():
    discovery = discover_xai_oauth()
    device_endpoint = discovery["device_authorization_endpoint"]
    token_endpoint = discovery["token_endpoint"]
    payload = urlencode(
        {
            "client_id": XAI_OAUTH_CLIENT_ID,
            "scope": XAI_OAUTH_SCOPE,
        }
    ).encode("utf-8")
    data = _http_json(
        "POST",
        device_endpoint,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        body=payload,
    )
    data = ext_dict('xAI device code response', data)
    device_code = data.get("device_code")
    user_code = data.get("user_code")
    verification_uri = data.get("verification_uri")
    verification_uri_complete = data.get("verification_uri_complete", "")
    device_code = ext_str("device_code", device_code, default="")
    if not device_code:
        raise RuntimeError("xAI device code response is missing device_code")
    user_code = ext_str("user_code", user_code, default="")
    if not user_code:
        raise RuntimeError("xAI device code response is missing user_code")
    verification_uri = ext_str("verification_uri", verification_uri, default="")
    if not verification_uri:
        raise RuntimeError("xAI device code response is missing verification_uri")
    verification_uri_complete = ext_str("verification_uri_complete", verification_uri_complete, strip=False)
    expires_in = data.get("expires_in", 300)
    expires_in = ext_float('expires_in', expires_in)
    interval = data.get("interval", 5)
    interval = ext_float('interval', interval)
    interval = int(interval)
    if interval <= 0:
        raise ExternalError("interval must be > 0")
    return PendingDeviceLogin(
        device_code=device_code,
        user_code=user_code,
        verification_uri=verification_uri,
        verification_uri_complete=verification_uri_complete,
        expires_in=int(expires_in),
        interval=interval,
        token_endpoint=token_endpoint,
        device_authorization_endpoint=device_endpoint,
    )

def poll_device_code_token(pending, *, sleep=None, now_ms=None):
    sleep_fn = time.sleep if sleep is None else sleep
    now_fn = (lambda: int(time.time() * 1000)) if now_ms is None else now_ms
    expires_in_ms = _positive_seconds_to_ms(pending.expires_in, DEVICE_CODE_DEFAULT_EXPIRES_MS)
    deadline = now_fn() + expires_in_ms
    interval_ms = max(
        _positive_seconds_to_ms(pending.interval, DEVICE_CODE_DEFAULT_INTERVAL_MS),
        DEVICE_CODE_MIN_INTERVAL_MS,
    )
    token_endpoint = _validate_xai_endpoint(pending.token_endpoint)
    body = urlencode(
        {
            "grant_type": DEVICE_CODE_GRANT_TYPE,
            "client_id": XAI_OAUTH_CLIENT_ID,
            "device_code": pending.device_code,
        }
    ).encode("utf-8")

    while now_fn() < deadline:
        status, data = _http_json_status(
            "POST",
            token_endpoint,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=body,
        )
        if status >= 200 and status < 300:
            return _credentials_from_token_payload(data, token_endpoint)

        error = ""
        error_description = ""
        if type(data) is dict:
            raw_error = data.get("error", "")
            raw_description = data.get("error_description", "")
            raw_error = ext_str('oauth error', raw_error, strip=False)
            raw_description = ext_str('oauth error_description', raw_description, strip=False)
            error = raw_error
            error_description = raw_description
        remaining_ms = max(0, deadline - now_fn())
        if error == "authorization_pending":
            sleep_fn(min((interval_ms + OAUTH_POLLING_SAFETY_MARGIN_MS) / 1000.0, remaining_ms / 1000.0))
            continue
        if error == "slow_down":
            interval_ms += DEVICE_CODE_SLOW_DOWN_INCREMENT_MS
            sleep_fn(min((interval_ms + OAUTH_POLLING_SAFETY_MARGIN_MS) / 1000.0, remaining_ms / 1000.0))
            continue
        if error in {"access_denied", "authorization_denied"}:
            raise RuntimeError("xAI device authorization was denied")
        if error == "expired_token":
            raise RuntimeError("xAI device code expired - please re-run /login")
        if error_description or error:
            detail = error_description or error
        elif type(data) is str:
            detail = data
        elif data is None:
            detail = ""
        else:
            raise ExternalError("oauth poll error body must be dict or str")
        raise RuntimeError("xAI device token exchange failed (%s)%s" % (status, (": " + detail) if detail else ""))

    raise RuntimeError("xAI device authorization timed out")
