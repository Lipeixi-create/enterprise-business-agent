from __future__ import annotations

import json
import unicodedata
from pathlib import Path

from .schemas import DimensionDefinition, MetricDefinition


def _semantic_dir(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    candidate = Path(__file__).resolve().parents[2] / "semantic_layer"
    if candidate.exists():
        return candidate
    return Path(__file__).resolve().parents[3] / "semantic_layer"


def load_metrics(path: str | Path | None = None) -> list[MetricDefinition]:
    """Load canonical metric definitions from metrics.json."""
    if path is not None and Path(path).suffix == ".json":
        metric_path = Path(path)
    else:
        metric_path = _semantic_dir(path) / "metrics.json"
    raw = json.loads(metric_path.read_text(encoding="utf-8"))
    return [MetricDefinition.model_validate(item) for item in raw["metrics"]]


def load_dimensions(path: str | Path | None = None) -> list[DimensionDefinition]:
    if path is not None and Path(path).suffix == ".json":
        dimension_path = Path(path)
    else:
        dimension_path = _semantic_dir(path) / "dimensions.json"
    raw = json.loads(dimension_path.read_text(encoding="utf-8"))
    return [DimensionDefinition.model_validate(item) for item in raw["dimensions"]]


def load_version(path: str | Path | None = None) -> str:
    metric_path = _semantic_dir(path) / "metrics.json"
    return str(json.loads(metric_path.read_text(encoding="utf-8"))["version"])


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().lower()
    return "".join(text.split())
