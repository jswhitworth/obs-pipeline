"""Optional Langfuse telemetry for the offline LLM tools.

Observability for the NON-deterministic edge only: one Langfuse trace per
tool run that actually called a model (generation + validation spans +
eval-derived scores). The pipeline itself is never instrumented -- its own
trace.jsonl is content-addressed and replay-checked, which Langfuse is not,
so mirroring it there would duplicate a stronger native artifact.

Wiring: the tools accept an injectable `sink` callable (default None, so
library calls and the test suite emit nothing); each CLI main() passes
`emit_tool_trace` when `enabled()` -- i.e. when LANGFUSE_* keys are present
in the environment or .env. Emission failures warn on stderr and never fail
the tool: telemetry must not be able to break a proposal run.

Traces are created with public=true (explicitly fine for this demo -- the
prompts embed device payloads, so flip `public` before pointing these tools
at non-synthetic data). stdlib urllib against /api/public/ingestion; no
Langfuse SDK (repo constraint).
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

import llm_client

_INGEST = "/api/public/ingestion"
_PROJECTS = "/api/public/projects"


def _keys() -> tuple[str | None, str | None, str]:
    env = {**llm_client.read_dotenv(),
           **{k: v for k, v in os.environ.items()
              if k.startswith("LANGFUSE_")}}
    return (env.get("LANGFUSE_PUBLIC_KEY"), env.get("LANGFUSE_SECRET_KEY"),
            (env.get("LANGFUSE_BASE_URL") or
             "https://cloud.langfuse.com").rstrip("/"))


def enabled() -> bool:
    pk, sk, _ = _keys()
    return bool(pk and sk)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _request(path: str, payload: dict | None, transport=None):
    """POST when payload is given, GET otherwise. `transport` is the test
    seam: transport(path, payload) -> parsed response."""
    if transport is not None:
        return transport(path, payload)
    pk, sk, base = _keys()
    auth = base64.b64encode(f"{pk}:{sk}".encode()).decode()
    req = urllib.request.Request(
        base + path,
        data=None if payload is None else json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json",
                 "Authorization": f"Basic {auth}"},
        method="GET" if payload is None else "POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _event(kind: str, body: dict) -> dict:
    return {"id": str(uuid.uuid4()), "timestamp": _now(), "type": kind,
            "body": body}


def emit_tool_trace(*, tool: str, model: str, prompt: str, output_text: str,
                    usage: dict | None, cached: bool, cache_key: str,
                    elapsed_ms: float, metadata: dict | None = None,
                    spans: list[tuple[str, dict]] = (),
                    scores: list[tuple[str, float]] = (),
                    session_id: str | None = None, public: bool = True,
                    transport=None) -> str | None:
    """One trace per tool run. Returns the trace URL (public when
    public=True), or None when emission failed or telemetry is off."""
    if transport is None and not enabled():
        return None
    try:
        trace_id = str(uuid.uuid4())
        now = _now()
        u = usage or {}
        batch = [
            _event("trace-create", {
                "id": trace_id, "name": tool, "timestamp": now,
                "public": public, "sessionId": session_id,
                "input": prompt, "output": output_text,
                "metadata": {**(metadata or {}), "cache_key": cache_key,
                             "cached": cached, "origin": "llm_proposed"},
                "tags": [model, "cached" if cached else "fresh"],
            }),
            _event("generation-create", {
                "id": str(uuid.uuid4()), "traceId": trace_id,
                "name": "anthropic.messages", "startTime": now,
                "endTime": now, "model": model,
                "input": prompt, "output": output_text,
                "metadata": {"cached": cached, "cache_key": cache_key,
                             "elapsed_ms": round(elapsed_ms, 1)},
                "usage": {"input": u.get("input_tokens", 0),
                          "output": u.get("output_tokens", 0),
                          "unit": "TOKENS"},
            }),
        ]
        for name, data in spans:
            batch.append(_event("span-create", {
                "id": str(uuid.uuid4()), "traceId": trace_id, "name": name,
                "startTime": now, "endTime": now, "output": data}))
        for name, value in scores:
            batch.append(_event("score-create", {
                "id": str(uuid.uuid4()), "traceId": trace_id, "name": name,
                "value": float(value)}))
        _request(_INGEST, {"batch": batch}, transport)

        _, _, base = _keys()
        try:
            projects = _request(_PROJECTS, None, transport)
            project_id = projects["data"][0]["id"]
            return f"{base}/project/{project_id}/traces/{trace_id}"
        except Exception:
            return f"{base}/trace/{trace_id}"
    except Exception as e:  # telemetry must never break the tool
        print(f"langfuse: emission failed: {e}", file=sys.stderr)
        return None
