"""Synthetic scenario generator (shared by /api/demo/inject and replay).

Produces variable-length IMU windows + ~1 Hz HR samples for the situation
taxonomy (HR + IMU only). The sit/lie scenarios are kept as benign cross-
distribution test cases — they fold into NORMAL now (the model is binary
FALL/NORMAL). collapse is a fall with an HR spike so escalation resolves to
SOS_COLLAPSE.

build_scenario(...) -> (imu, hr) as JSON-ready dicts.
"""

from __future__ import annotations

import math
import random
from time import time

SCENARIOS = ("normal", "sit", "lie", "fall", "collapse")

# scenario -> situation label (binary). sit/lie are benign -> NORMAL.
SITUATION_OF = {
    "normal": "NORMAL",
    "sit": "NORMAL",
    "lie": "NORMAL",
    "fall": "FALL",
    "collapse": "FALL",  # collapse = fall + HR spike; situation is FALL, escalation -> SOS
}


def build_scenario(
    scenario: str = "collapse",
    *,
    core_samples: int = 200,
    rate_hz: float = 50.0,
) -> tuple[list[dict], list[dict]]:
    scenario = scenario.lower()
    if scenario not in SCENARIOS:
        raise ValueError(f"unknown scenario '{scenario}', expected one of {SCENARIOS}")

    dt = 1.0 / rate_hz
    start = time()
    n = core_samples
    imu = [
        _sample(scenario, idx / max(n - 1, 1), start + idx * dt)
        for idx in range(n)
    ]
    duration_s = n * dt

    if scenario == "collapse":
        hr = _vital_ramp(start, duration_s, 120.0, 168.0, jitter=6.0)
    elif scenario == "fall":
        hr = _vital_ramp(start, duration_s, 80.0, 100.0, jitter=5.0)
    else:  # normal / sit / lie
        base_hr = {"normal": 72.0, "sit": 74.0, "lie": 70.0}[scenario]
        hr = _vital_flat(start, duration_s, base_hr, 3.0)

    return imu, hr


# ---------- per-sample IMU shaping ----------


def _sample(scenario: str, phase: float, ts: float) -> dict:
    if scenario == "normal":
        return _payload(ts,
            0.05 * math.sin(2 * math.pi * phase) + _n(0.03),
            0.08 * math.sin(math.pi * phase) + _n(0.03),
            0.97 + 0.05 * math.sin(math.pi * phase) + _n(0.03),
            0.3 * math.sin(math.pi * phase) + _n(0.08),
            0.2 * math.sin(2 * math.pi * phase) + _n(0.08),
            0.1 * math.sin(math.pi * phase) + _n(0.08))

    if scenario == "sit":
        tilt = 1.0 - 0.4 * phase
        bump = 0.4 * math.exp(-((phase - 0.8) ** 2) / 0.004)
        return _payload(ts,
            _ramp(0.05, 0.45, phase) + _n(0.05),
            0.2 + _n(0.05),
            tilt + bump * 0.3 + _n(0.05),
            0.6 * bump + _n(0.1),
            0.8 * bump + _n(0.1),
            0.3 * bump + _n(0.1))

    if scenario == "lie":
        return _payload(ts,
            _ramp(0.05, 0.95, phase) + _n(0.04),
            0.1 + _n(0.04),
            _ramp(1.0, 0.12, phase) + _n(0.04),
            0.4 * math.sin(math.pi * phase) + _n(0.08),
            0.5 * math.sin(math.pi * phase) + _n(0.08),
            0.2 * math.sin(math.pi * phase) + _n(0.08))

    # fall / collapse share the fall IMU signature
    return _fall_sample(phase, ts)


def _fall_sample(phase: float, ts: float) -> dict:
    if phase < 0.55:  # upright, mild motion
        return _payload(ts, _n(0.05), _n(0.05), 0.97 + _n(0.05),
                        _n(0.2), _n(0.2), _n(0.2))
    if phase < 0.70:  # free fall -> acc magnitude collapses toward ~0 g
        return _payload(ts, _n(0.08), _n(0.08), 0.1 + _n(0.06),
                        _n(0.5), _n(0.5), _n(0.5))
    if phase < 0.78:  # impact spike
        return _payload(ts, 1.6 + _n(0.3), 1.4 + _n(0.3), 2.4 + _n(0.3),
                        3.0 + _n(0.6), 3.2 + _n(0.6), 2.8 + _n(0.6))
    # post-impact stillness (lying motionless): low motion, tilted
    return _payload(ts, 0.9 + _n(0.02), 0.1 + _n(0.02), 0.15 + _n(0.02),
                    _n(0.04), _n(0.04), _n(0.04))


# ---------- HR ----------


def _vital_flat(start_ts: float, duration_s: float, value: float, jitter: float) -> list[dict]:
    n = max(1, int(round(duration_s)))
    return [_hr(start_ts + i, max(0.0, value + _n(jitter))) for i in range(n)]


def _vital_ramp(start_ts: float, duration_s: float, a: float, b: float, jitter: float) -> list[dict]:
    n = max(1, int(round(duration_s)))
    out = []
    for i in range(n):
        p = i / max(n - 1, 1)
        out.append(_hr(start_ts + i, max(0.0, _ramp(a, b, p) + _n(jitter))))
    return out


def _hr(ts: float, bpm: float) -> dict:
    return {"timestamp": ts, "bpm": float(bpm), "accuracy": 3}


# ---------- helpers ----------


def _ramp(a: float, b: float, phase: float) -> float:
    return a + (b - a) * phase


def _n(scale: float) -> float:
    return random.uniform(-scale, scale)


def _payload(ts, acc_x, acc_y, acc_z, gyro_x, gyro_y, gyro_z) -> dict:
    return {
        "timestamp": ts,
        "acc_x": acc_x, "acc_y": acc_y, "acc_z": acc_z,
        "gyro_x": gyro_x, "gyro_y": gyro_y, "gyro_z": gyro_z,
    }
