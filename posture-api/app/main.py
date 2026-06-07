"""FastAPI service v3.2 — AI inference microservice.

Single responsibility: turn an IMU + HR window into a classified *situation*
(+ confidence + features). Everything else — escalation, alarms, GPS, buddy /
guardian messaging, two-way commands, statistics — is orchestrated by
ThingsBoard (the `GuardianRules` rule chain), not here.

Data flow:
    watch --POST /api/infer {imu, hr}--> this service --> {situation, confidence,
        features}     (server's only job)
    watch --/api/v1/{wearer_token}/telemetry--> ThingsBoard   (watch posts the
        inference result + GPS straight to TB; the rule chain does the rest)

The only server->TB path left is the *demo* injector, which posts a synthetic
inference result to a wearer's TB device so the whole TB pipeline can be shown
with one request. It is clearly demo-only.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time as _time_mod
from datetime import datetime
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, HTTPException

from .baseline import BaselineStore
from .classifier import SituationClassifier
from .schemas import (
    HealthResponse,
    InferRequest,
    InferResponse,
    RebuildResponse,
    TrainingStatsResponse,
    TrainingUpload,
    TrainingUploadResponse,
)
from .fall_dataset import sample_demo_window
from .synth import SCENARIOS
from .tb_client import TbConfig, WorkerTbBridge, push_telemetry_token
from .training_store import compute_stats, load_records

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("guardian-api")

DATA_DIR = Path(__file__).parent.parent.parent / "data"
TRAINING_DIR = DATA_DIR / "posture_training"
MODEL_DIR = DATA_DIR / "models"
TRAINING_DIR.mkdir(parents=True, exist_ok=True)
MODEL_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="Wearable Guardian — AI Inference API v3.2",
    description=(
        "Edge inference microservice for a wrist-worn physiological guardian: a "
        "binary situation classifier (fall / normal) over "
        "IMU + HR with a personal HR baseline. Escalation, alarms, GPS and "
        "messaging are orchestrated by ThingsBoard, not this service."
    ),
    version="3.2.0",
)

classifier = SituationClassifier(MODEL_DIR / "posture_classifier.joblib")
baseline_store = BaselineStore(DATA_DIR / "worker_baselines.json")
tb_cfg = TbConfig()

# Wearer token map (W-001 -> TB device token). Only used by the demo injector
# to post a synthetic inference result straight to TB so the rule chain runs.
WORKER_TOKENS_PATH = DATA_DIR / "worker_tokens.json"
tb_workers = WorkerTbBridge.from_file(tb_cfg.api_base, WORKER_TOKENS_PATH)


# ---------- Core: AI inference (the service's only real job) ----------


def _infer(request: InferRequest) -> InferResponse:
    started = perf_counter()
    baseline = baseline_store.get(request.worker_id)
    situation, conf, proba, feats = classifier.predict(
        request.samples,
        request.hr_samples,
        baseline,
    )
    latency_ms = round((perf_counter() - started) * 1000.0, 3)

    # Personal HR baseline keeps learning from NORMAL windows (part of "AI").
    if situation == "NORMAL":
        baseline_store.observe_normal(request.worker_id, feats)

    return InferResponse(
        situation=situation,
        situation_confidence=conf,
        proba=proba,
        features=feats,
        latency_ms=latency_ms,
        model_source=classifier.source,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        model=classifier.source,
        has_trained_model=classifier.model is not None,
        tb_workers_loaded=tb_workers.count(),
    )


@app.post("/api/infer", response_model=InferResponse)
def infer(request: InferRequest) -> InferResponse:
    """Classify one IMU + HR window. No GPS / escalation / alarms / TB push."""
    return _infer(request)


# Backwards-compatible alias so older watch builds keep working during rollout.
@app.post("/api/evaluate_lift", response_model=InferResponse)
def evaluate_lift(request: InferRequest) -> InferResponse:
    return _infer(request)


# ---------- Training (also part of the AI lifecycle) ----------


@app.post("/api/training", response_model=TrainingUploadResponse)
def upload_training_data(request: TrainingUpload) -> TrainingUploadResponse:
    if not request.records:
        raise HTTPException(status_code=400, detail="No training records provided")

    timestamp = datetime.now().isoformat(timespec="milliseconds").replace(":", "-").replace(".", "-")
    suffix = secrets.token_hex(2)
    file_path = TRAINING_DIR / f"posture_{timestamp}_{suffix}.json"
    file_path.write_text(
        json.dumps(request.model_dump(), indent=2, default=str), encoding="utf-8"
    )

    return TrainingUploadResponse(
        status="success",
        records_saved=len(request.records),
        file_path=str(file_path),
        timestamp=timestamp,
    )


@app.get("/api/training/stats", response_model=TrainingStatsResponse)
def training_stats() -> TrainingStatsResponse:
    stats, files = compute_stats(TRAINING_DIR)
    return TrainingStatsResponse(stats=stats, total_files=files)


@app.post("/api/training/rebuild", response_model=RebuildResponse)
def training_rebuild() -> RebuildResponse:
    records = load_records(TRAINING_DIR)
    status, accuracy, cm, used, msg = classifier.train(records)
    return RebuildResponse(
        status=status,
        model_source=classifier.source,
        accuracy=accuracy,
        samples_used=used,
        confusion=cm,
        message=msg,
    )


# ---------- Demo injection (demo-only server->TB shortcut) ----------


@app.post("/api/demo/inject")
def demo_inject(
    scenario: str = "collapse",
    worker_id: str = "W-001",
    lat: float = 24.78686,   # 陽明交大 光復校區 (NYCU Guangfu campus)
    lon: float = 120.99681,
) -> dict:
    """Run a synthetic scenario through inference and post the result to TB.

    Demo-only: lets the whole TB pipeline (escalation JS -> alarms -> clear ->
    relations -> nearby) be shown with a single request, without a real watch.
    The escalation / alarm logic lives in the TB rule chain, not here — this
    endpoint only posts the inference result + GPS to the wearer's TB device,
    exactly like the watch would.
    """
    scenario = scenario.lower()
    if scenario not in SCENARIOS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown scenario '{scenario}', expected one of {list(SCENARIOS)}",
        )

    # Replay a REAL window from the fall dataset so the demo feeds the trained
    # model in-distribution data.
    imu, hr = sample_demo_window(scenario)
    req = InferRequest(
        worker_id=worker_id,
        session_id=f"demo-{int(_time_mod.time())}",
        samples=imu,  # type: ignore[arg-type]  # pydantic coerces dicts
        hr_samples=hr,  # type: ignore[arg-type]
    )
    result = _infer(req)

    pushed = False
    token = tb_workers.tokens.get(worker_id)
    if token:
        _hr_mean = result.features.get("hr_mean")
        payload = {
            "situation": result.situation,
            "situation_confidence": result.situation_confidence,
            # bpm = current HR so the dashboard's live HR shows in demos (the real
            # watch posts bpm continuously; the demo has only the window's HR).
            "bpm": round(_hr_mean) if _hr_mean else None,
            "hr_mean": _hr_mean,
            "hr_max": result.features.get("hr_max"),
            "hr_delta_max": result.features.get("hr_delta_max"),
            "hr_above_baseline": result.features.get("hr_above_baseline"),
            "post_impact_stillness": result.features.get("post_impact_stillness"),
            "model_source": result.model_source,
            "worker_id": worker_id,
            # small jitter so the wearer pin moves around campus across injects
            "lat": lat + secrets.randbelow(60) / 100000.0,
            "lon": lon + secrets.randbelow(60) / 100000.0,
            "active": True,
            "entity_type": "wearer",
        }
        pushed = push_telemetry_token(tb_cfg.api_base, token, payload)

    return {
        "scenario": scenario,
        "wearer": worker_id,
        "pushed_to_tb": pushed,
        "gps": {"lat": lat, "lon": lon},
        "infer": result.model_dump(),
        "note": "escalation + alarms are computed by the TB GuardianRules rule chain",
    }


# ---------- Startup ----------


@app.on_event("startup")
def on_startup() -> None:
    if classifier.model is None:
        records = load_records(TRAINING_DIR)
        if records:
            status, accuracy, _, used, msg = classifier.train(records)
            logger.info(
                "Startup train status=%s accuracy=%s used=%s msg=%s",
                status, accuracy, used, msg,
            )
        else:
            logger.info("No training data yet; using heuristic situation classifier")
