"""Learned per-model costs: GPU memory footprint, typical duration and cold-load time.

These feed placement decisions (ModelFacts). They start as estimates and converge on
measurements, and are saved to disk so a restart doesn't forget them.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)

ALPHA = 0.2  # EWMA weight of the newest sample
DEFAULT_DURATION = 5.0
LOAD_BYTES_PER_SEC = 1.5e9  # conservative NVMe-to-VRAM rate for a first estimate
KV_OVERHEAD = 1.15  # loaded size vs. file size before we have observed it
CTX_DECAY = 0.97  # per request: about the last ~30 requests decide a model's usual context size


@dataclass
class ModelStats:
    vram_bytes: int = 0  # largest size_vram seen in /api/ps; 0 = never observed
    duration: float = 0.0  # EWMA of request duration, seconds; 0 = no samples
    load_time: float = 0.0  # EWMA of cold-load time, seconds; 0 = no samples
    samples: int = 0
    # Decaying count of requests per context size (JSON keys are strings).
    ctx_weights: dict[str, float] = field(default_factory=dict)


def _ewma(old: float, new: float) -> float:
    return new if old == 0 else (1 - ALPHA) * old + ALPHA * new


class Stats:
    def __init__(self, path: str | None = None):
        self.path = path
        self.models: dict[str, ModelStats] = {}
        self.dirty = False
        if path:
            self._load()

    def get(self, model: str) -> ModelStats:
        return self.models.setdefault(model, ModelStats())

    def observe_loaded(self, model: str, vram_bytes: int) -> None:
        s = self.get(model)
        if vram_bytes > s.vram_bytes:
            s.vram_bytes = vram_bytes
            self.dirty = True

    def observe_request(self, model: str, duration: float, load_time: float = 0.0,
                        ctx: int | None = None) -> None:
        s = self.get(model)
        s.duration = _ewma(s.duration, duration)
        s.samples += 1
        if load_time > 0.5:  # anything faster was already loaded
            s.load_time = _ewma(s.load_time, load_time)
        if ctx:
            for k in s.ctx_weights:
                s.ctx_weights[k] *= CTX_DECAY
            s.ctx_weights[str(ctx)] = s.ctx_weights.get(str(ctx), 0.0) + 1.0
        self.dirty = True

    def usual_ctx(self, model: str) -> int | None:
        """The context size `model` is mostly requested at lately, or None if unknown."""
        w = self.get(model).ctx_weights
        return int(max(w, key=w.get)) if w else None

    def vram_estimate(self, model: str, file_size: int) -> int:
        s = self.get(model)
        if s.vram_bytes:
            return s.vram_bytes
        return int(file_size * KV_OVERHEAD)

    def duration_estimate(self, model: str) -> float:
        return self.get(model).duration or DEFAULT_DURATION

    def load_time_estimate(self, model: str, file_size: int) -> float:
        s = self.get(model)
        return s.load_time or (1.0 + file_size / LOAD_BYTES_PER_SEC)

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                raw = json.load(f)
            self.models = {k: ModelStats(**v) for k, v in raw.get("models", {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            log.warning("ignoring unreadable stats file %s: %s", self.path, exc)

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        data = {"models": {k: asdict(v) for k, v in self.models.items()}}
        d = os.path.dirname(self.path) or "."
        try:
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".stats-")
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError as exc:
            log.warning("could not save stats to %s: %s", self.path, exc)
