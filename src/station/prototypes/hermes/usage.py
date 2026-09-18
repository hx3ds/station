USAGE_EXHAUSTED_MARKERS = (
    "personal-team-blocked:spending-limit",
    "run out of credits",
    "out of credits",
    "billing, credits, or account entitlement is exhausted",
    "billing or credits exhausted",
    "insufficient credits",
    "insufficient_quota",
    "no_usable_credits",
    "balance_depleted",
    "member_spend_cap_exceeded",
    "payment_required",
    "need a grok subscription",
    "usage not enough",
)

USAGE_EXHAUSTED_REPLY = (
    "Hermes usage is not enough. Credits or subscription for this model are exhausted.\n"
    "Add credits at https://grok.com/?_s=usage or upgrade at https://grok.com/supergrok."
)


def _blob_parts(*parts):
    chunks = []
    for part in parts:
        if part is None:
            continue
        if isinstance(part, dict):
            chunks.append(str(part.get("message") or ""))
            chunks.append(str(part.get("error") or ""))
            chunks.append(str(part.get("text") or ""))
            chunks.append(str(part.get("failure_reason") or ""))
            chunks.append(str(part.get("reason") or ""))
            billing = part.get("billing")
            if isinstance(billing, dict):
                chunks.append(str(billing.get("message") or ""))
                chunks.append("billing")
            elif billing:
                chunks.append("billing")
            continue
        if isinstance(part, (list, tuple)):
            chunks.extend(_blob_parts(*part))
            continue
        chunks.append(str(part))
    return chunks


def is_usage_exhausted(*parts):
    for part in parts:
        if not isinstance(part, dict):
            continue
        reason = str(part.get("failure_reason") or part.get("reason") or "").strip().lower()
        if reason in {"billing", "usage_exhausted"}:
            return True
        if part.get("billing"):
            return True
    blob = "\n".join(_blob_parts(*parts)).lower()
    return any(marker in blob for marker in USAGE_EXHAUSTED_MARKERS)


def stderr_usage_exhausted_abort(line, recent):
    text = (line or "").lower()
    if "aborting" not in text:
        return False
    if "non-retryable" not in text and not any("non-retryable" in (item or "").lower() for item in recent):
        return False
    return is_usage_exhausted(*(recent or []), line)
