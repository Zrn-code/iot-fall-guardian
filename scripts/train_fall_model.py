"""Retrain the situation classifier on the real(istic) Elderly-Fall IoT dataset.

Source: data/dataset/fall_detection.csv  (Kaggle: ziya07/elderly-fall-detection-
iot-dataset — a multimodal simulation built on the Montreal "Multiple Cameras
Fall" video set). 500 sequences x 50 timesteps, one label each.

What we use and why
-------------------
* Binary FALL vs NORMAL, trained from this dataset's real labelled events: the
  5 fall subtypes -> FALL; lie/sit/stand/walk/bend all fold into NORMAL.
* The dataset reconstruction (gravity-present accel), the label mapping, and the
  resting-HR synthesis all live in app.fall_dataset so this trainer and the live
  demo injector (app.main) can never drift apart. See that module's docstring
  for the ambient-sensor / HR-leak / gravity rationale.

Why binary: only FALL drives any downstream action (ThingsBoard escalation, the
watch alarm). Folding sit/lie into NORMAL teaches the model that sitting/lying
down are NOT falls, which lowers false alarms, instead of wasting capacity on a
sit-vs-stand distinction that is unobservable on watch-available channels.

Caveats:
* The dataset is a simulation; resting noise is larger than a real watch's.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "posture-api"))

import random  # noqa: E402

from app.classifier import SituationClassifier  # noqa: E402
from app.fall_dataset import load_sequences, resting_hr_window, SAMPLE_DT  # noqa: E402
from app.training_store import load_records  # noqa: E402

TRAINING_DIR = ROOT / "data" / "posture_training"
MODEL_PATH = ROOT / "data" / "models" / "posture_classifier.joblib"


def build_fall_records() -> list[dict]:
    rng = random.Random(42)
    records = []
    for seq in load_sequences():
        samples = seq["samples"]
        duration = (len(samples) - 1) * SAMPLE_DT
        records.append({
            "situation": seq["situation"],
            "recorded_at": "2026-06-02T00:00:00+00:00",
            "source": "elderly-fall-iot-dataset",
            "samples": samples,
            "hr_samples": resting_hr_window(duration, rng),
        })
    return records


def write_training_file(path: Path, records: list[dict]) -> None:
    path.write_text(json.dumps({"records": records}), encoding="utf-8")


def main() -> None:
    fall = build_fall_records()

    counts: dict[str, int] = defaultdict(int)
    for r in fall:
        counts[r["situation"]] += 1
    print("Built records per situation:", dict(counts))

    write_training_file(TRAINING_DIR / "posture_fall_dataset.json", fall)
    print(f"Wrote {len(fall)} real fall/posture records to {TRAINING_DIR}")

    records = load_records(TRAINING_DIR)
    clf = SituationClassifier(MODEL_PATH)
    status, accuracy, confusion, n_used, message = clf.train(records)

    print("\n=== TRAIN RESULT ===")
    print("status   :", status)
    print("accuracy :", accuracy)
    print("samples  :", n_used)
    if message:
        print("message  :", message)
    if confusion:
        labels = sorted(confusion)
        print("\nconfusion (rows=true, cols=pred):")
        print("           " + "".join(f"{l[:9]:>11s}" for l in labels))
        for t in labels:
            print(f"{t:>11s}" + "".join(f"{confusion[t].get(p, 0):>11d}" for p in labels))
    print("\nmodel saved to:", MODEL_PATH)


if __name__ == "__main__":
    main()
