"""Shared loader for the Elderly-Fall IoT dataset (data/dataset/fall_detection.csv).

Single source of truth so training (scripts/train_fall_model.py) and the live
demo injector (app.main) never diverge in how they read this dataset:

* label -> 2-class situation mapping (LABEL_MAP): FALL vs NORMAL (sit/lie/
  stand/walk/bend all fold into NORMAL)
* gravity-present accelerometer reconstruction (the CSV stores gravity-removed
  linear accel + a pitch/roll orientation channel; a real watch and our feature
  pipeline expect gravity-present g, so we add a unit-gravity vector back)
* resting-HR synthesis shared across both IMU classes (HR must NOT encode
  the class — see scripts/train_fall_model.py)

The 3 ambient channels (floor_vibration / room_occupancy / pressure_mat) are
deliberately ignored: they are label-leakage and a wrist watch does not have
them. HR is still synthesised per window so the collapse demo and the
fall->SOS_COLLAPSE escalation (which read hr_above_baseline / hr_max) keep
working.
"""

from __future__ import annotations

import csv
import math
import random
from collections import defaultdict
from functools import lru_cache
from pathlib import Path

SAMPLE_DT = 0.05  # 20 Hz, the dataset's cadence

# dataset label -> our 2-class taxonomy (FALL vs NORMAL).
# sit/lie fold into NORMAL: the model only needs to separate "fell" from
# "didn't fall", and folding teaches it that sitting/lying down are NOT falls.
LABEL_MAP: dict[str, str] = {
    "fall_forward": "FALL",
    "fall_backward": "FALL",
    "fall_side_left": "FALL",
    "fall_side_right": "FALL",
    "fall_slump": "FALL",
    "lie_down": "NORMAL",
    "sit": "NORMAL",
    "stand": "NORMAL",
    "walk": "NORMAL",
    "bend": "NORMAL",
}

# scenario name -> dataset-backed situation. sit/lie replay NORMAL windows now
# (their dataset sequences fold into the NORMAL group via LABEL_MAP).
SCENARIO_SITUATION: dict[str, str] = {
    "normal": "NORMAL",
    "sit": "NORMAL",
    "lie": "NORMAL",
    "fall": "FALL",
    "collapse": "FALL",  # fall IMU; the caller adds an elevated-HR channel
}

RESTING_HR_RANGE = (66.0, 84.0)  # identical distribution for every IMU class
RESTING_HR_JITTER = 4.0


def default_csv_path() -> Path:
    # app/ -> posture-api/ -> repo root -> data/dataset/fall_detection.csv
    return Path(__file__).resolve().parent.parent.parent / "data" / "dataset" / "fall_detection.csv"


def gravity_vector(pitch_deg: float, roll_deg: float) -> tuple[float, float, float]:
    """Unit gravity (g) in the sensor frame for a pitch/roll (|g| == 1)."""
    th = math.radians(pitch_deg)
    ph = math.radians(roll_deg)
    return (-math.sin(th), math.cos(th) * math.sin(ph), math.cos(th) * math.cos(ph))


def resting_hr_window(duration_s: float, rng: random.Random) -> list[dict]:
    """Resting HR window shared by all IMU classes (carries no class signal)."""
    base = rng.uniform(*RESTING_HR_RANGE)
    n = max(2, int(round(duration_s / 0.5)))
    return [
        {"timestamp": i * 0.5,
         "bpm": max(40.0, base + rng.uniform(-RESTING_HR_JITTER, RESTING_HR_JITTER)),
         "accuracy": 3}
        for i in range(n)
    ]


def elevated_hr_window(duration_s: float, rng: random.Random) -> list[dict]:
    """High, climbing HR for the collapse demo so TB escalates to SOS_COLLAPSE."""
    n = max(2, int(round(duration_s / 0.5)))
    return [
        {"timestamp": i * 0.5,
         "bpm": 138.0 + (24.0 * i / max(n - 1, 1)) + rng.uniform(-5.0, 5.0),
         "accuracy": 3}
        for i in range(n)
    ]


def _reconstruct_window(rows: list[dict]) -> list[dict]:
    rows.sort(key=lambda r: int(r["timestep"]))
    out = []
    for i, r in enumerate(rows):
        gx, gy, gz = gravity_vector(float(r["pitch"]), float(r["roll"]))
        out.append({
            "timestamp": i * SAMPLE_DT,
            "acc_x": float(r["accel_x"]) + gx,
            "acc_y": float(r["accel_y"]) + gy,
            "acc_z": float(r["accel_z"]) + gz,
            "gyro_x": float(r["gyro_x"]),
            "gyro_y": float(r["gyro_y"]),
            "gyro_z": float(r["gyro_z"]),
        })
    return out


def load_sequences(csv_path: Path | None = None) -> list[dict]:
    """All sequences as {situation, label, samples}. samples are JSON-ready dicts
    of gravity-present IMU (no HR — callers attach HR appropriate to their use)."""
    path = csv_path or default_csv_path()
    by_seq: dict[str, list[dict]] = defaultdict(list)
    with path.open() as f:
        for row in csv.DictReader(f):
            by_seq[row["sequence_id"]].append(row)
    out = []
    for rows in by_seq.values():
        label = rows[0]["label"]
        out.append({
            "situation": LABEL_MAP[label],
            "label": label,
            "samples": _reconstruct_window(rows),
        })
    return out


@lru_cache(maxsize=1)
def _windows_by_situation(csv_str: str) -> dict[str, list[list[dict]]]:
    grouped: dict[str, list[list[dict]]] = defaultdict(list)
    for seq in load_sequences(Path(csv_str)):
        grouped[seq["situation"]].append(seq["samples"])
    return dict(grouped)


def sample_demo_window(
    scenario: str, rng: random.Random | None = None, csv_path: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    """Replay a real dataset window for `scenario` + an appropriate HR channel.

    Used by /api/demo/inject so the demo feeds the model in-distribution data.
    """
    rng = rng or random.Random()
    path = csv_path or default_csv_path()
    situation = SCENARIO_SITUATION[scenario]
    windows = _windows_by_situation(str(path)).get(situation, [])
    if not windows:
        raise ValueError(f"no dataset windows for situation {situation!r}")
    samples = list(rng.choice(windows))
    duration = (len(samples) - 1) * SAMPLE_DT
    if scenario == "collapse":
        hr = elevated_hr_window(duration, rng)
    else:
        hr = resting_hr_window(duration, rng)
    return samples, hr
