"""MLflow tracing that switches itself off when MLflow isn't installed or no destination is set.

On Databricks (notebook or Model Serving) tracing is on. On the Mac it's off unless you run the CLI with --trace,
which sends traces to the Databricks experiment /Users/<DATABRICKS_USER>/northlake-cib-agent.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

try:
    import mlflow
    from mlflow.entities import SpanType
except ImportError:  # pragma: no cover
    mlflow = None
    SpanType = None

_state = {"on": bool(mlflow) and bool(os.environ.get("DATABRICKS_RUNTIME_VERSION") or os.environ.get("MLFLOW_TRACKING_URI"))}


def enabled() -> bool:
    return _state["on"]


def setup_databricks(experiment: str | None = None) -> str:
    """Send traces to the Databricks workspace in .env. Returns the experiment path."""
    if mlflow is None:
        raise RuntimeError("pip install 'mlflow-skinny>=3.1' to trace")
    from common import config

    os.environ.setdefault("DATABRICKS_HOST", config.host())
    mlflow.set_tracking_uri("databricks")
    experiment = experiment or f"/Users/{config.env('DATABRICKS_USER')}/northlake-cib-agent"
    mlflow.set_experiment(experiment)
    _state["on"] = True
    return experiment


def autolog_llm_calls() -> None:
    """Child spans for every chat completion (prompts, tool calls, tokens)."""
    if enabled():
        try:
            mlflow.openai.autolog()
        except Exception:  # openai flavour unavailable: node spans still work
            pass


class _NullSpan:
    def set_inputs(self, *_a, **_k): ...
    def set_outputs(self, *_a, **_k): ...
    def set_attribute(self, *_a, **_k): ...
    def set_attributes(self, *_a, **_k): ...


@contextmanager
def span(name: str, kind: str = "CHAIN", inputs=None):
    if not enabled():
        yield _NullSpan()
        return
    with mlflow.start_span(name=name, span_type=getattr(SpanType, kind, kind)) as s:
        if inputs is not None:
            s.set_inputs(inputs)
        yield s


def last_trace_id() -> str | None:
    if not enabled():
        return None
    try:
        return mlflow.get_last_active_trace_id()
    except Exception:
        return None
