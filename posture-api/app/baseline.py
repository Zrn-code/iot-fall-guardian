"""Per-worker heart-rate baseline (the personalization core).

What's "normal" resting HR for one person isn't for another. We seed a default
and refine it with EWMA over windows the classifier labels NORMAL, so the
baseline feature (hr_above_baseline) is per-person.
Thread-safe + optional JSON persistence, mirroring PositionStore.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict
from pathlib import Path

# EWMA weight for each new NORMAL observation (small = slow, stable baseline).
EWMA_ALPHA = float(os.environ.get("BASELINE_EWMA_ALPHA", "0.05"))
DEFAULT_HR = float(os.environ.get("BASELINE_DEFAULT_HR", "72"))


@dataclass
class Baseline:
    worker_id: str
    hr: float
    updated_at: float
    n_obs: int = 0


class BaselineStore:
    def __init__(self, persist_path: Path | None = None) -> None:
        self._d: dict[str, Baseline] = {}
        self._lock = threading.RLock()
        self._persist = persist_path
        self._load()

    def get(self, worker_id: str | None) -> Baseline | None:
        if not worker_id:
            return None
        with self._lock:
            b = self._d.get(worker_id)
            if b is None:
                b = Baseline(worker_id, DEFAULT_HR, time.time())
                self._d[worker_id] = b
            return b

    def seed(self, worker_id: str, hr: float) -> Baseline:
        with self._lock:
            b = Baseline(worker_id, hr, time.time(), n_obs=1)
            self._d[worker_id] = b
        self._save()
        return b

    def observe_normal(self, worker_id: str | None, feats: dict[str, float]) -> None:
        """EWMA-update HR baseline from a window classified NORMAL."""
        if not worker_id:
            return
        hr = feats.get("hr_max") or feats.get("hr_mean") or 0.0
        with self._lock:
            b = self.get(worker_id)
            assert b is not None
            if hr > 0:
                b.hr = _ewma(b.hr, hr)
            b.updated_at = time.time()
            b.n_obs += 1

    # ---------- persistence (best-effort) ----------

    def _load(self) -> None:
        if not self._persist or not self._persist.exists():
            return
        try:
            raw = json.loads(self._persist.read_text(encoding="utf-8"))
            with self._lock:
                self._d = {k: Baseline(**v) for k, v in raw.items()}
        except Exception:  # noqa: BLE001
            pass

    def _save(self) -> None:
        if not self._persist:
            return
        try:
            with self._lock:
                data = {k: asdict(v) for k, v in self._d.items()}
            self._persist.parent.mkdir(parents=True, exist_ok=True)
            self._persist.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def _ewma(prev: float, new: float) -> float:
    return (1 - EWMA_ALPHA) * prev + EWMA_ALPHA * new
