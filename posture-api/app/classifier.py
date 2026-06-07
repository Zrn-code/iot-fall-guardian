"""Binary situation classifier (FALL vs NORMAL) with a heuristic fallback.

Outputs one of SITUATIONS = NORMAL / FALL.
The point of using ML (vs thresholds): benign motions like sitting/lying down
produce impact-like accelerometer signatures too — only a trained model
separates a real fall from those reliably. Escalation (FALL -> SOS) is decided
downstream in the ThingsBoard rule chain.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import train_test_split

from .baseline import DEFAULT_HR, Baseline
from .features import FEATURE_NAMES, extract_features, features_to_vector
from .schemas import SITUATIONS, HeartRateSample, ImuSample

logger = logging.getLogger("situation-classifier")

MIN_SAMPLES_PER_CLASS = 5
MODEL_KIND = "random-forest-v3"

# Training uses a default baseline so baseline-relative features are computed
# the same way a fresh worker is scored at inference time (consistent space).
_TRAIN_BASELINE = Baseline("_train", DEFAULT_HR, 0.0)


class SituationClassifier:
    def __init__(self, model_path: Path) -> None:
        self.model_path = model_path
        self.model: RandomForestClassifier | None = None
        self.source = "heuristic"
        self._lock = threading.Lock()
        self.try_load()

    # ---------- Load / persist ----------

    def try_load(self) -> bool:
        if not self.model_path.exists():
            return False
        try:
            bundle = joblib.load(self.model_path)
            saved_features = tuple(bundle.get("features", ()))
            if saved_features != FEATURE_NAMES or bundle.get("kind") != MODEL_KIND:
                logger.warning(
                    "Discarding incompatible model (features match=%s, kind=%s)",
                    saved_features == FEATURE_NAMES, bundle.get("kind"),
                )
                self.model_path.unlink(missing_ok=True)
                return False
            self.model = bundle["model"]
            self.source = bundle.get("source", MODEL_KIND)
            logger.info("Loaded situation model from %s", self.model_path)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load model: %s", exc)
            return False

    def save(self, accuracy: float | None) -> None:
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "source": self.source,
                "kind": MODEL_KIND,
                "features": list(FEATURE_NAMES),
                "accuracy": accuracy,
            },
            self.model_path,
        )

    # ---------- Predict ----------

    def predict(
        self,
        samples: Sequence[ImuSample],
        hr_samples: Sequence[HeartRateSample],
        baseline: Baseline | None = None,
    ) -> tuple[str, float, dict[str, float], dict[str, float]]:
        feats = extract_features(samples, hr_samples, baseline)
        with self._lock:
            model = self.model
        if model is None:
            situation, conf, proba = _heuristic_situation(feats)
            return situation, conf, proba, feats

        vec = features_to_vector(feats).reshape(1, -1)
        proba_row = model.predict_proba(vec)[0]
        classes = [str(c) for c in model.classes_]
        proba = {c: float(p) for c, p in zip(classes, proba_row)}
        # ensure all situations present in dict for stable response shape
        for s in SITUATIONS:
            proba.setdefault(s, 0.0)
        situation = str(classes[int(np.argmax(proba_row))])
        return situation, float(proba_row.max()), proba, feats

    # ---------- Train ----------

    def train(
        self, records: list[dict]
    ) -> tuple[str, float | None, dict | None, int, str | None]:
        """Train RandomForest on `situation` (binary FALL vs NORMAL).

        Returns (status, accuracy, confusion, samples_used, message).
        """
        if not records:
            return "no-data", None, None, 0, "No training records found."

        labelled = [r for r in records if r.get("situation") in SITUATIONS]
        if not labelled:
            return "no-data", None, None, len(records), "No records carry a v3 'situation' label."

        counts = {s: sum(1 for r in labelled if r["situation"] == s) for s in SITUATIONS}
        present = {s: n for s, n in counts.items() if n > 0}
        short = [f"{s}({n})" for s, n in present.items() if n < MIN_SAMPLES_PER_CLASS]
        if len(present) < 2 or short:
            return (
                "insufficient-data", None, None, len(labelled),
                f"Need >={MIN_SAMPLES_PER_CLASS} per class and >=2 classes. Counts: {counts}.",
            )

        feature_rows: list[np.ndarray] = []
        y: list[str] = []
        for r in labelled:
            samples = [ImuSample(**s) if not isinstance(s, ImuSample) else s for s in r["samples"]]
            hr = [HeartRateSample(**h) if not isinstance(h, HeartRateSample) else h
                  for h in (r.get("hr_samples") or [])]
            feats = extract_features(samples, hr, _TRAIN_BASELINE)
            feature_rows.append(features_to_vector(feats))
            y.append(r["situation"])

        X = np.vstack(feature_rows)
        y_arr = np.asarray(y)

        can_stratify = len(labelled) >= 2 * len(present) and min(present.values()) >= 2
        if can_stratify:
            X_train, X_test, y_train, y_test = train_test_split(
                X, y_arr, test_size=0.25, random_state=42, stratify=y_arr
            )
        else:
            X_train, X_test, y_train, y_test = X, X, y_arr, y_arr

        model = RandomForestClassifier(
            n_estimators=200, max_depth=None, random_state=42, n_jobs=-1,
            class_weight="balanced",
        )
        model.fit(X_train, y_train)
        y_pred = model.predict(X_test)
        accuracy = float(accuracy_score(y_test, y_pred))

        labels_sorted = sorted(present.keys())
        cm = confusion_matrix(y_test, y_pred, labels=labels_sorted)
        confusion = {
            labels_sorted[i]: {labels_sorted[j]: int(cm[i, j]) for j in range(len(labels_sorted))}
            for i in range(len(labels_sorted))
        }

        with self._lock:
            self.model = model
            self.source = MODEL_KIND
        self.save(accuracy)
        return "success", accuracy, confusion, len(labelled), None


def _heuristic_situation(feats: dict[str, float]) -> tuple[str, float, dict[str, float]]:
    """No-model fallback. Reliable enough for FALL demos."""
    is_fall = (
        feats.get("acc_mag_min", 1.0) < 0.4
        and feats.get("acc_mag_impact", 0.0) > 1.8
        and feats.get("post_impact_stillness", 1.0) < 0.05
    )

    if is_fall:
        situation, conf = "FALL", 0.7
    else:
        situation, conf = "NORMAL", 0.55

    rest = (1.0 - conf) / (len(SITUATIONS) - 1)
    proba = {s: (conf if s == situation else rest) for s in SITUATIONS}
    return situation, conf, proba
