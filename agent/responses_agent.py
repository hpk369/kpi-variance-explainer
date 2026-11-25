"""MLflow ResponsesAgent wrapper, so the agent can be logged, registered in Unity Catalog and served.

The answer is the text output item; the "show your work" payload goes in custom_outputs.
Options in custom_inputs: {"inject_wrong_number": true, "use_vector_search": false}.
Logged as "models from code" by agent/log_and_register.py. Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Generator

import mlflow
from mlflow.pyfunc import ResponsesAgent
from mlflow.types.responses import ResponsesAgentRequest, ResponsesAgentResponse, ResponsesAgentStreamEvent

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _last_user_text(request: ResponsesAgentRequest) -> str:
    for item in reversed(request.input):
        item = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        if item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, list):
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
            return str(content or "").strip()
    raise ValueError("The request has no user message.")


class CibInsightsAgent(ResponsesAgent):
    def _run(self, request: ResponsesAgentRequest) -> dict:
        from agent.graph import run

        opts = request.custom_inputs or {}
        return run(_last_user_text(request), inject_wrong_number=bool(opts.get("inject_wrong_number")),
                   use_vector_search=opts.get("use_vector_search", True))

    @staticmethod
    def _custom(work: dict) -> dict:
        return {k: work[k] for k in ("outcome", "as_of", "tool_calls", "retrieved", "retrieval_mode", "number_check",
                                     "models", "latency_ms")}

    def predict(self, request: ResponsesAgentRequest) -> ResponsesAgentResponse:
        work = self._run(request)
        item = self.create_text_output_item(text=work["answer"], id=f"msg_{uuid.uuid4().hex[:12]}")
        return ResponsesAgentResponse(output=[item], custom_outputs=self._custom(work))

    def predict_stream(self, request: ResponsesAgentRequest) -> Generator[ResponsesAgentStreamEvent, None, None]:
        # The answer is only released after the number check, so it is sent as one final item.
        work = self._run(request)
        item = self.create_text_output_item(text=work["answer"], id=f"msg_{uuid.uuid4().hex[:12]}")
        yield ResponsesAgentStreamEvent(type="response.output_item.done", item=item, custom_outputs=self._custom(work))


AGENT = CibInsightsAgent()
mlflow.models.set_model(AGENT)
