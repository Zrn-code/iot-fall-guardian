"""Pydantic schemas for the Wearable Guardian API v3.0 (situation + vitals)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class ImuSample(BaseModel):
    timestamp: float | None = None
    acc_x: float
    acc_y: float
    acc_z: float
    gyro_x: float
    gyro_y: float
    gyro_z: float


class HeartRateSample(BaseModel):
    timestamp: float | None = None
    bpm: float
    accuracy: int | None = None


class GpsFix(BaseModel):
    lat: float
    lon: float
    accuracy_m: float | None = None


# situation taxonomy (binary: did the wearer fall or not). sit/lie collapse into
# NORMAL — only FALL drives any downstream action.
# NOTE: escalation (NONE / ASK_OK / SOS_COLLAPSE) is computed by the
# ThingsBoard GuardianRules rule chain now, not this service.
SITUATIONS = ("NORMAL", "FALL")
Situation = Literal["NORMAL", "FALL"]


class InferRequest(BaseModel):
    """One variable-length IMU + HR window to classify.

    No GPS / escalation fields: the watch posts GPS straight to ThingsBoard;
    escalation is computed by the TB rule chain, not this service.
    """

    worker_id: str | None = None
    session_id: str | None = None
    sample_rate_hz: float = Field(default=50.0, ge=1.0, le=400.0)
    samples: list[ImuSample] = Field(..., min_length=16)
    hr_samples: list[HeartRateSample] = Field(default_factory=list)


class InferResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    situation: Situation
    situation_confidence: float
    proba: dict[str, float]
    features: dict[str, float]
    latency_ms: float
    model_source: str


class TrainingRecord(BaseModel):
    situation: Situation | None = None
    recorded_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    source: str = "wear-os"
    worker_id: str | None = None
    samples: list[ImuSample]
    hr_samples: list[HeartRateSample] = Field(default_factory=list)
    gps: GpsFix | None = None


class TrainingUpload(BaseModel):
    records: list[TrainingRecord]


class TrainingUploadResponse(BaseModel):
    status: str
    records_saved: int
    file_path: str
    timestamp: str


class HealthResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    status: str
    model: str
    has_trained_model: bool
    tb_workers_loaded: int = 0


class LabelStats(BaseModel):
    normal: int = 0
    fall: int = 0
    total_records: int = 0
    last_recorded_at: str | None = None


class TrainingStatsResponse(BaseModel):
    stats: LabelStats
    total_files: int


class RebuildResponse(BaseModel):
    model_config = {"protected_namespaces": ()}

    status: str
    model_source: str
    accuracy: float | None = None
    samples_used: int
    confusion: dict[str, dict[str, int]] | None = None
    message: str | None = None
