"""Generate README figures from the real training results -> docs/img/*.png.

Figures:
  1. dataset_distribution.png  - class counts (real fall/ADL data)
  2. confusion_matrix.png      - pooled 5-fold CV confusion (counts + row-normalised)
  3. per_class_metrics.png     - precision / recall / F1 per class
  4. feature_importance.png    - top-15 RandomForest feature importances
  5. signal_examples.png       - one accel-magnitude window per situation
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support
from sklearn.model_selection import StratifiedKFold

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "posture-api"))

from app.baseline import DEFAULT_HR, Baseline  # noqa: E402
from app.features import FEATURE_NAMES, extract_features, features_to_vector  # noqa: E402
from app.training_store import load_records  # noqa: E402

OUT = ROOT / "docs" / "img"
OUT.mkdir(parents=True, exist_ok=True)
TRAIN_BL = Baseline("_train", DEFAULT_HR, 0.0)
LABELS = ["NORMAL", "FALL"]
C = {"real": "#2563eb", "FALL": "#dc2626", "NORMAL": "#16a34a"}
plt.rcParams.update({"font.size": 11, "axes.grid": True, "grid.alpha": 0.25,
                     "figure.dpi": 130, "savefig.bbox": "tight"})


def build_xy(records):
    X = np.vstack([features_to_vector(extract_features(r["samples"], r["hr_samples"], TRAIN_BL))
                   for r in records])
    y = np.array([r["situation"] for r in records])
    return X, y


def cv_predictions(X, y):
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=7)
    yt, yp = [], []
    for tr, te in skf.split(X, y):
        m = RandomForestClassifier(n_estimators=200, random_state=42, n_jobs=-1,
                                   class_weight="balanced").fit(X[tr], y[tr])
        yt.extend(y[te]); yp.extend(m.predict(X[te]))
    return np.array(yt), np.array(yp)


def fig_distribution(records):
    counts = Counter(r["situation"] for r in records)
    vals = [counts[l] for l in LABELS]
    colors = [C[l] for l in LABELS]
    fig, ax = plt.subplots(figsize=(8, 4.2))
    bars = ax.bar(LABELS, vals, color=colors)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v + 3, str(v), ha="center", fontweight="bold")
    ax.set_ylabel("training windows")
    ax.set_title(f"Training set composition ({sum(vals)} real fall/ADL windows, "
                 "Elderly-Fall IoT dataset)")
    ax.set_ylim(0, max(vals) * 1.15)
    fig.savefig(OUT / "dataset_distribution.png"); plt.close(fig)


def fig_confusion(yt, yp):
    cm = confusion_matrix(yt, yp, labels=LABELS)
    cmn = cm / cm.sum(axis=1, keepdims=True)
    fig, axes = plt.subplots(1, 2, figsize=(12, 5))
    n = len(LABELS)
    for ax, mat, title, fmt in ((axes[0], cm, "Counts", "d"),
                                (axes[1], cmn, "Row-normalised (recall)", ".2f")):
        im = ax.imshow(mat, cmap="Blues", vmin=0, vmax=(cm.max() if fmt == "d" else 1))
        ax.set_xticks(range(n)); ax.set_yticks(range(n))
        ax.set_xticklabels(LABELS, rotation=40, ha="right"); ax.set_yticklabels(LABELS)
        ax.set_xlabel("predicted"); ax.set_ylabel("true"); ax.set_title(title)
        ax.grid(False)
        for i in range(n):
            for j in range(n):
                v = mat[i, j]
                txt = f"{v:d}" if fmt == "d" else f"{v:.2f}"
                ax.text(j, i, txt, ha="center", va="center",
                        color="white" if (v > (cm.max()*0.5 if fmt=="d" else 0.5)) else "black",
                        fontsize=10)
    fig.suptitle("5-fold cross-validated confusion matrix (random-forest-v3, 27-dim)",
                 fontweight="bold")
    fig.savefig(OUT / "confusion_matrix.png"); plt.close(fig)


def fig_metrics(yt, yp):
    p, r, f, _ = precision_recall_fscore_support(yt, yp, labels=LABELS, zero_division=0)
    x = np.arange(len(LABELS)); w = 0.26
    fig, ax = plt.subplots(figsize=(9, 4.4))
    ax.bar(x - w, p, w, label="precision", color="#60a5fa")
    ax.bar(x, r, w, label="recall", color="#2563eb")
    ax.bar(x + w, f, w, label="F1", color="#1e3a8a")
    for i in range(len(LABELS)):
        ax.text(i + w, f[i] + 0.02, f"{f[i]:.2f}", ha="center", fontsize=9)
    ax.set_xticks(x); ax.set_xticklabels(LABELS, rotation=20)
    ax.set_ylim(0, 1.1); ax.set_ylabel("score"); ax.legend(ncol=3, loc="lower center")
    ax.set_title("Per-class precision / recall / F1 (5-fold CV)\n"
                 "binary FALL vs NORMAL — fall's impact signature separates cleanly")
    fig.savefig(OUT / "per_class_metrics.png"); plt.close(fig)


def fig_importance(X, y):
    m = RandomForestClassifier(n_estimators=300, random_state=42, n_jobs=-1,
                               class_weight="balanced").fit(X, y)
    imp = m.feature_importances_
    idx = np.argsort(imp)[::-1][:15][::-1]
    fig, ax = plt.subplots(figsize=(8, 5.5))
    ax.barh([FEATURE_NAMES[i] for i in idx], imp[idx], color="#2563eb")
    ax.set_xlabel("importance"); ax.set_title("Top-15 feature importances (random-forest-v3)")
    fig.savefig(OUT / "feature_importance.png"); plt.close(fig)


def fig_signals(records):
    fig, ax = plt.subplots(figsize=(9, 4.6))
    for lab in LABELS:
        rec = next(r for r in records if r["situation"] == lab)
        s = rec["samples"]
        mag = [np.sqrt(p.acc_x**2 + p.acc_y**2 + p.acc_z**2) for p in s]
        t = np.arange(len(mag)) * 0.05
        ax.plot(t, mag, label=lab, color=C[lab], linewidth=1.8)
    ax.axhline(1.0, color="gray", ls=":", lw=1, label="1 g (rest)")
    ax.set_xlabel("time (s)"); ax.set_ylabel("|acc| (g)")
    ax.set_title("Accelerometer magnitude per situation (one window each)\n"
                 "FALL = large impact spike; NORMAL stays near 1 g")
    ax.legend(ncol=3, fontsize=9)
    fig.savefig(OUT / "signal_examples.png"); plt.close(fig)


def main():
    records = load_records(ROOT / "data" / "posture_training")
    X, y = build_xy(records)
    print("records:", len(y), dict(Counter(y)))
    yt, yp = cv_predictions(X, y)
    fig_distribution(records)
    fig_confusion(yt, yp)
    fig_metrics(yt, yp)
    fig_importance(X, y)
    fig_signals(records)
    print("wrote figures to", OUT)
    for p in sorted(OUT.glob("*.png")):
        print("  ", p.name)


if __name__ == "__main__":
    main()
