"""Load situation training records from disk (v3.0).

Reads records that carry a v3 'situation' label. Legacy v2 records (bend/twist
only, no 'situation') are loaded but left unlabelled; the classifier ignores
unlabelled records, so old data on disk does no harm.
"""

from __future__ import annotations

import json
from pathlib import Path

from .schemas import HeartRateSample, ImuSample, LabelStats, SITUATIONS

MIN_SAMPLES_PER_RECORD = 16


def iter_training_files(data_dir: Path) -> list[Path]:
    return sorted(p for p in data_dir.glob("posture_*.json") if p.is_file())


def load_records(data_dir: Path) -> list[dict]:
    out: list[dict] = []
    for path in iter_training_files(data_dir):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for rec in payload.get("records", []):
            samples_raw = rec.get("samples") or []
            if len(samples_raw) < MIN_SAMPLES_PER_RECORD:
                continue
            try:
                samples = [ImuSample(**s) for s in samples_raw]
            except (TypeError, ValueError):
                continue
            hr_samples = _coerce(rec.get("hr_samples"), HeartRateSample)
            situation = rec.get("situation")
            if situation not in SITUATIONS:
                situation = None  # legacy / unlabelled
            out.append(
                {
                    "situation": situation,
                    "recorded_at": rec.get("recorded_at"),
                    "samples": samples,
                    "hr_samples": hr_samples,
                    "_file": path.name,
                }
            )
    return out


def _coerce(raw, model):
    try:
        return [model(**x) for x in (raw or [])]
    except (TypeError, ValueError):
        return []


def compute_stats(data_dir: Path) -> tuple[LabelStats, int]:
    records = load_records(data_dir)
    files = iter_training_files(data_dir)

    def n(situation: str) -> int:
        return sum(1 for r in records if r["situation"] == situation)

    labelled = [r for r in records if r["situation"] in SITUATIONS]
    last = max((r.get("recorded_at") or "" for r in labelled), default="") or None

    stats = LabelStats(
        normal=n("NORMAL"),
        fall=n("FALL"),
        total_records=len(labelled),
        last_recorded_at=last,
    )
    return stats, len(files)
