"""Probe the LIVE posture-api (:8000) with real dataset windows via /api/infer.

No TB writes (unlike /api/demo/inject). Confirms the running service serves the
retrained model and classifies in-distribution dataset windows correctly.
"""

from __future__ import annotations

import json
import random
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "posture-api"))

from app.fall_dataset import sample_demo_window  # noqa: E402

BASE = "http://127.0.0.1:8000"


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def main() -> None:
    rng = random.Random(0)
    cases = []
    for sc in ("normal", "sit", "lie", "fall", "collapse"):
        imu, hr = sample_demo_window(sc, rng)
        cases.append((sc, imu, hr))

    must = {"fall": "FALL", "collapse": "FALL"}
    print(f"live /api/infer @ {BASE}")
    for sc, imu, hr in cases:
        res = post("/api/infer", {"worker_id": "verify", "samples": imu, "hr_samples": hr})
        sit = res["situation"]
        amax = res["features"]["acc_mag_max"]
        hrx = res["features"]["hr_max"]
        flag = ("OK " if sit == must[sc] else "XX ") if sc in must else ".. "
        print(f"  {flag}{sc:9s} -> {sit:12s} conf={res['situation_confidence']:.2f} "
              f"acc_mag_max={amax:5.2f} hr_max={hrx:5.1f} src={res['model_source']}")


if __name__ == "__main__":
    main()
