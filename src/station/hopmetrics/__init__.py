from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

_LOCK = threading.Lock()
_COUNTS: dict[tuple[str, str], int] = defaultdict(int)
_SUM_MS: dict[tuple[str, str], float] = defaultdict(float)
_SAMPLES: dict[tuple[str, str], list[float]] = defaultdict(list)
_SAMPLE_POS: dict[tuple[str, str], int] = defaultdict(int)
_SAMPLE_CAP = 4096

def observe(flow: str, outcome: str, duration_ms: float) -> None:
    flow = (flow or "").strip()
    if not flow:
        return
    outcome = (outcome or "unknown").strip() or "unknown"
    if duration_ms < 0:
        duration_ms = 0.0
    key = (flow, outcome)
    with _LOCK:
        _COUNTS[key] += 1
        _SUM_MS[key] += duration_ms
        buf = _SAMPLES[key]
        if len(buf) < _SAMPLE_CAP:
            buf.append(duration_ms)
        else:
            pos = _SAMPLE_POS[key] % _SAMPLE_CAP
            buf[pos] = duration_ms
            _SAMPLE_POS[key] = pos + 1

def _percentile(sorted_vals: list[float], p: int) -> float:
    if not sorted_vals:
        return 0.0
    if p <= 0:
        return sorted_vals[0]
    if p >= 100:
        return sorted_vals[-1]
    idx = (p * len(sorted_vals)) // 100
    if idx >= len(sorted_vals):
        idx = len(sorted_vals) - 1
    return sorted_vals[idx]

def snapshot() -> list[dict[str, Any]]:
    with _LOCK:
        items = []
        for (flow, outcome), count in _COUNTS.items():
            samples = sorted(_SAMPLES.get((flow, outcome), []))
            items.append(
                {
                    "flow": flow,
                    "outcome": outcome,
                    "count": count,
                    "sum_ms": _SUM_MS.get((flow, outcome), 0.0),
                    "p50_ms": _percentile(samples, 50),
                    "p90_ms": _percentile(samples, 90),
                    "p99_ms": _percentile(samples, 99),
                }
            )
        return items

def render_prometheus(prefix: str = "station") -> str:
    prefix = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in (prefix or "station"))
    lines = [f"# TYPE {prefix}_hop_completions_total counter"]
    with _LOCK:
        for (flow, outcome), count in sorted(_COUNTS.items()):
            lines.append(
                f'{prefix}_hop_completions_total{{flow="{flow}",outcome="{outcome}"}} {count}'
            )
        lines.append(f"# TYPE {prefix}_hop_duration_ms_sum counter")
        for (flow, outcome), total in sorted(_SUM_MS.items()):
            lines.append(
                f'{prefix}_hop_duration_ms_sum{{flow="{flow}",outcome="{outcome}"}} {total}'
            )
    return "\n".join(lines) + "\n"
