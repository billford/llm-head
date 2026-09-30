"""Human-readable formats used in Olla's /internal/status payloads."""

from __future__ import annotations

import time


def ago(ts: float, now: float | None = None) -> str:
    if not ts:
        return "never"
    secs = int((now or time.time()) - ts)
    if secs < 1:
        return "now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def until(ts: float, now: float | None = None) -> str:
    secs = int(ts - (now or time.time()))
    return "now" if secs <= 0 else f"in {secs}s"


def uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def size(nbytes: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    n = float(nbytes)
    for u in units:
        if n < 1024 or u == units[-1]:
            return f"{int(n)} B" if u == "B" else f"{n:.2f} {u}"
        n /= 1024
    return f"{n:.2f} TB"


def ms(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{int(value)}ms"


def pct(ok: int, total: int) -> str:
    """Olla prints 0%, 100%, or one decimal place (e.g. 97.5%)."""
    if total == 0 or ok == 0:
        return "0%"
    if ok >= total:
        return "100%"
    return f"{100 * ok / total:.1f}%"
