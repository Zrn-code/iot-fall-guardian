"""Feature extraction for one event window (v3.0 guardian).

27 dims = 15 IMU + 5 HR + 6 fall + 1 personal-baseline (HR).
See docs/plan-guardian-v3.md §3.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from .schemas import HeartRateSample, ImuSample

IMU_FEATURE_NAMES: tuple[str, ...] = (
    "acc_z_peak",
    "acc_z_min",
    "acc_z_range",
    "acc_mag_mean",
    "acc_mag_std",
    "acc_mag_max",
    "gyro_x_var",
    "gyro_y_var",
    "gyro_z_var",
    "gyro_mag_mean",
    "gyro_mag_max",
    "twist_index",
    "tilt_mean",
    "tilt_max",
    "duration_s",
)

HR_FEATURE_NAMES: tuple[str, ...] = (
    "hr_mean",
    "hr_max",
    "hr_range",
    "hr_delta_max",
    "hr_sd",
)

FALL_FEATURE_NAMES: tuple[str, ...] = (
    "acc_mag_min",            # free-fall low point (~0 g)
    "acc_mag_impact",         # impact spike
    "jerk_max",               # max d|acc|/dt
    "freefall_to_impact_ms",  # time from lowest dip to impact peak
    "post_impact_stillness",  # accel variance after impact (low = motionless)
    "orientation_delta",      # tilt change start -> end (standing -> down)
)

BASELINE_FEATURE_NAMES: tuple[str, ...] = (
    "hr_above_baseline",
)

FEATURE_NAMES: tuple[str, ...] = (
    IMU_FEATURE_NAMES
    + HR_FEATURE_NAMES
    + FALL_FEATURE_NAMES
    + BASELINE_FEATURE_NAMES
)


def _samples_to_array(samples: Sequence[ImuSample]) -> np.ndarray:
    return np.asarray(
        [[s.acc_x, s.acc_y, s.acc_z, s.gyro_x, s.gyro_y, s.gyro_z] for s in samples],
        dtype=np.float64,
    )


def extract_imu_features(samples: Sequence[ImuSample]) -> dict[str, float]:
    arr = _samples_to_array(samples)
    if arr.shape[0] < 2:
        return {name: 0.0 for name in IMU_FEATURE_NAMES}

    acc = arr[:, 0:3]
    gyro = arr[:, 3:6]
    acc_mag = np.linalg.norm(acc, axis=1)
    gyro_mag = np.linalg.norm(gyro, axis=1)

    az_clipped = np.clip(acc[:, 2] / np.maximum(acc_mag, 1e-6), -1.0, 1.0)
    tilt = np.degrees(np.arccos(az_clipped))

    gyro_var = gyro.var(axis=0)
    twist_index = float(gyro_var[2] / (gyro_var[0] + gyro_var[1] + 1e-6))

    duration = 0.0
    timestamps = [s.timestamp for s in samples if s.timestamp is not None]
    if len(timestamps) >= 2:
        duration = float(timestamps[-1] - timestamps[0])
    if duration <= 0.0:
        duration = float(len(samples)) / 50.0

    return {
        "acc_z_peak": float(acc[:, 2].max()),
        "acc_z_min": float(acc[:, 2].min()),
        "acc_z_range": float(acc[:, 2].max() - acc[:, 2].min()),
        "acc_mag_mean": float(acc_mag.mean()),
        "acc_mag_std": float(acc_mag.std()),
        "acc_mag_max": float(acc_mag.max()),
        "gyro_x_var": float(gyro_var[0]),
        "gyro_y_var": float(gyro_var[1]),
        "gyro_z_var": float(gyro_var[2]),
        "gyro_mag_mean": float(gyro_mag.mean()),
        "gyro_mag_max": float(gyro_mag.max()),
        "twist_index": twist_index,
        "tilt_mean": float(tilt.mean()),
        "tilt_max": float(tilt.max()),
        "duration_s": duration,
    }


def extract_hr_features(hr_samples: Sequence[HeartRateSample]) -> dict[str, float]:
    bpms = np.asarray(
        [s.bpm for s in hr_samples if s.bpm > 0 and (s.accuracy is None or s.accuracy > 0)],
        dtype=np.float64,
    )
    if bpms.size < 2:
        return {name: 0.0 for name in HR_FEATURE_NAMES}

    deltas = np.abs(np.diff(bpms))
    return {
        "hr_mean": float(bpms.mean()),
        "hr_max": float(bpms.max()),
        "hr_range": float(bpms.max() - bpms.min()),
        "hr_delta_max": float(deltas.max()) if deltas.size > 0 else 0.0,
        "hr_sd": float(bpms.std()),
    }


def extract_fall_features(samples: Sequence[ImuSample]) -> dict[str, float]:
    arr = _samples_to_array(samples)
    if arr.shape[0] < 3:
        return {name: 0.0 for name in FALL_FEATURE_NAMES}

    acc = arr[:, 0:3]
    acc_mag = np.linalg.norm(acc, axis=1)

    dt = 0.02
    ts = [s.timestamp for s in samples if s.timestamp is not None]
    if len(ts) >= 2:
        span = float(ts[-1] - ts[0])
        if span > 0:
            dt = span / (len(samples) - 1)

    min_idx = int(np.argmin(acc_mag))
    # impact = strongest spike at or after the free-fall dip
    tail = acc_mag[min_idx:]
    impact_rel = int(np.argmax(tail))
    impact_idx = min_idx + impact_rel
    jerk = np.abs(np.diff(acc_mag)) / max(dt, 1e-6)

    # stillness over the last ~1 s (or everything after impact, whichever shorter)
    n_still = min(len(acc_mag), max(8, int(round(1.0 / max(dt, 1e-6)))))
    still_tail = acc_mag[-n_still:]

    head = acc[: min(5, len(acc))]
    tailo = acc[-min(5, len(acc)):]
    tilt_start = _mean_tilt(head)
    tilt_end = _mean_tilt(tailo)

    return {
        "acc_mag_min": float(acc_mag.min()),
        "acc_mag_impact": float(acc_mag.max()),
        "jerk_max": float(jerk.max()) if jerk.size else 0.0,
        "freefall_to_impact_ms": float(max(0, impact_idx - min_idx) * dt * 1000.0),
        "post_impact_stillness": float(still_tail.var()),
        "orientation_delta": float(abs(tilt_end - tilt_start)),
    }


def _mean_tilt(acc: np.ndarray) -> float:
    if acc.shape[0] == 0:
        return 0.0
    mag = np.linalg.norm(acc, axis=1)
    az = np.clip(acc[:, 2] / np.maximum(mag, 1e-6), -1.0, 1.0)
    return float(np.degrees(np.arccos(az)).mean())


def extract_baseline_features(feats: dict[str, float], baseline) -> dict[str, float]:
    """Personalized deviations. `baseline` is a Baseline (hr) or None."""
    if baseline is None:
        return {name: 0.0 for name in BASELINE_FEATURE_NAMES}
    return {
        "hr_above_baseline": float(feats.get("hr_max", 0.0) - baseline.hr),
    }


def extract_features(
    samples: Sequence[ImuSample],
    hr_samples: Sequence[HeartRateSample],
    baseline=None,
) -> dict[str, float]:
    feats = extract_imu_features(samples)
    feats.update(extract_hr_features(hr_samples))
    feats.update(extract_fall_features(samples))
    feats.update(extract_baseline_features(feats, baseline))
    for key, value in feats.items():
        if not math.isfinite(value):
            feats[key] = 0.0
    return feats


def features_to_vector(features: dict[str, float]) -> np.ndarray:
    return np.asarray([features[name] for name in FEATURE_NAMES], dtype=np.float64)
