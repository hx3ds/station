import re

LOCAL_PLATFORM_TYPE_MAX_LENGTH = 15
LOCAL_PLATFORM_TYPE_REGEX = re.compile(r"^[a-z][a-z0-9_-]{0,14}$")
QR_ACCOUNT_TYPE_PREFIX = "qr:"

def normalize_local_platform_type(value):
    return (value or "").strip().lower()

def is_qr_account_type(value):
    return normalize_local_platform_type(value).startswith(QR_ACCOUNT_TYPE_PREFIX)

def strip_qr_prefix(value):
    normalized = normalize_local_platform_type(value)
    if normalized.startswith(QR_ACCOUNT_TYPE_PREFIX):
        return normalized[len(QR_ACCOUNT_TYPE_PREFIX) :]
    return normalized

def validate_bare_platform_type(value, *, field_name):
    normalized = strip_qr_prefix(value)
    if not normalized:
        raise ValueError(f"{field_name} is required")
    if len(normalized) > LOCAL_PLATFORM_TYPE_MAX_LENGTH:
        raise ValueError(f"{field_name} must be shorter than 16 characters")
    try:
        normalized.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be lowercase ASCII") from exc
    if not LOCAL_PLATFORM_TYPE_REGEX.fullmatch(normalized):
        raise ValueError(
            f"{field_name} must match {LOCAL_PLATFORM_TYPE_REGEX.pattern}"
        )
    return normalized

def validate_local_platform_type(value, *, field_name):
    normalized = normalize_local_platform_type(value)
    if normalized.startswith(QR_ACCOUNT_TYPE_PREFIX):
        bare = validate_bare_platform_type(
            normalized[len(QR_ACCOUNT_TYPE_PREFIX) :],
            field_name=field_name,
        )
        return QR_ACCOUNT_TYPE_PREFIX + bare
    return validate_bare_platform_type(normalized, field_name=field_name)
