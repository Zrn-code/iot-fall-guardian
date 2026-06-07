"""Honest evaluation of the fall-dataset-trained classifier.

1. Stratified 5-fold CV on the training records (realistic accuracy + confusion).
2. Cross-distribution check: feed the synth demo scenarios (a DIFFERENT signal
   distribution that the TB demo + watch path use) through the trained model.
3. sit-vs-stand probe: are they actually separable, or did lumping stand into
   NORMAL hide the collision?
"""

from __future__ import annotations

import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import StratifiedKFold

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "posture-api"))

from app.baseline import DEFAULT_HR, Baseline  # noqa: E402
from app.classifier import SituationClassifier  # noqa: E402
from app.features import extract_features, features_to_vector  # noqa: E402
from app.schemas import HeartRateSample, ImuSample, SITUATIONS  # noqa: E402
from app.synth import SCENARIOS, SITUATION_OF, build_scenario  # noqa: E402
from app.training_store import load_records  # noqa: E402

TRAIN_BL = Baseline("_train", DEFAULT_HR, 0.0)
MODEL_PATH = ROOT / "data" / "models" / "posture_classifier.joblib"
CSV_PATH = ROOT / "data" / "dataset" / "fall_detection.csv"


def vec(samples, hr):
    return features_to_vector(extract_features(samples, hr, TRAIN_BL))


def cross_validate():
    records = load_records(ROOT / "data" / "posture_training")
    X = np.vstack([vec(r["samples"], r["hr_samples"]) for r in records])
    y = np.array([r["situation"] for r in records])
    print(f"records: {len(y)}  per-class: {dict(Counter(y))}")

    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=7)
    accs, all_true, all_pred = [], [], []
    for tr, te in skf.split(X, y):
        m = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1,
                                   class_weight="balanced")
        m.fit(X[tr], y[tr])
        p = m.predict(X[te])
        accs.append(accuracy_score(y[te], p))
        all_true.extend(y[te]); all_pred.extend(p)
    print(f"\n5-fold CV accuracy: mean={np.mean(accs):.4f}  folds={[round(a,3) for a in accs]}")
    labels = sorted(set(y))
    cm = confusion_matrix(all_true, all_pred, labels=labels)
    print("pooled CV confusion (rows=true, cols=pred):")
    print("           " + "".join(f"{l[:9]:>11s}" for l in labels))
    for i, t in enumerate(labels):
        print(f"{t:>11s}" + "".join(f"{cm[i][j]:>11d}" for j in range(len(labels))))


def cross_distribution():
    clf = SituationClassifier(MODEL_PATH)
    print(f"\nmodel source: {clf.source}  (loaded={clf.model is not None})")
    print("\nsynth demo scenario -> trained-model prediction (expected | got):")
    for sc in SCENARIOS:
        imu, hr = build_scenario(sc, core_samples=200, rate_hz=50.0)
        samples = [ImuSample(**s) for s in imu]
        hrs = [HeartRateSample(**h) for h in hr]
        sit, conf, proba, _ = clf.predict(samples, hrs, TRAIN_BL)
        exp = SITUATION_OF[sc]
        flag = "OK " if sit == exp else "XX "
        print(f"  {flag}{sc:9s} expected={exp:12s} got={sit:12s} conf={conf:.2f}")


def sit_vs_stand_probe():
    """Train only on sit (SIT_DOWN) vs stand (NORMAL) windows from the raw CSV
    using the same reconstruction, and see if they are separable at all."""
    import math, random
    by_seq = defaultdict(list)
    with CSV_PATH.open() as f:
        for row in csv.DictReader(f):
            by_seq[row["sequence_id"]].append(row)

    def grav(p, r):
        th, ph = math.radians(p), math.radians(r)
        return (-math.sin(th), math.cos(th)*math.sin(ph), math.cos(th)*math.cos(ph))

    rng = random.Random(1)
    X, y = [], []
    for rows in by_seq.values():
        lab = rows[0]["label"]
        if lab not in ("sit", "stand"):
            continue
        rows.sort(key=lambda r: int(r["timestep"]))
        samples = []
        for i, r in enumerate(rows):
            gx, gy, gz = grav(float(r["pitch"]), float(r["roll"]))
            samples.append(ImuSample(
                timestamp=i*0.05,
                acc_x=float(r["accel_x"])+gx, acc_y=float(r["accel_y"])+gy,
                acc_z=float(r["accel_z"])+gz,
                gyro_x=float(r["gyro_x"]), gyro_y=float(r["gyro_y"]), gyro_z=float(r["gyro_z"])))
        hr = [HeartRateSample(timestamp=j, bpm=75.0, accuracy=3) for j in range(4)]
        X.append(vec(samples, hr)); y.append(lab)
    X = np.vstack(X); y = np.array(y)
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=3)
    accs = []
    for tr, te in skf.split(X, y):
        m = RandomForestClassifier(n_estimators=200, random_state=0, n_jobs=-1)
        m.fit(X[tr], y[tr]); accs.append(accuracy_score(y[te], m.predict(X[te])))
    print(f"\nsit-vs-stand binary CV accuracy: mean={np.mean(accs):.3f} "
          f"(0.5 = indistinguishable, 1.0 = perfectly separable)  n={len(y)}")


def demo_replay_check():
    """Exercise the real /api/demo/inject path: replay dataset windows through the
    trained model. Alarm scenarios (fall/collapse) MUST be right; normal/sit/lie
    are all non-emergency so either label is harmless."""
    import random as _r
    from app.fall_dataset import sample_demo_window
    clf = SituationClassifier(MODEL_PATH)
    must = {"fall": "FALL", "collapse": "FALL"}
    print("\ndemo replay (dataset windows) -> trained-model prediction:")
    rng = _r.Random(0)
    for sc in ("normal", "sit", "lie", "fall", "collapse"):
        imu, hr = sample_demo_window(sc, rng)
        sit, conf, _, _ = clf.predict([ImuSample(**s) for s in imu],
                                      [HeartRateSample(**h) for h in hr], TRAIN_BL)
        if sc in must:
            flag = "OK " if sit == must[sc] else "XX "
        else:
            flag = ".. "  # non-emergency, either label fine
        print(f"  {flag}{sc:9s} got={sit:12s} conf={conf:.2f}")


if __name__ == "__main__":
    cross_validate()
    cross_distribution()
    sit_vs_stand_probe()
    demo_replay_check()
