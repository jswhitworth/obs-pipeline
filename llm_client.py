"""Shared LLM plumbing for the offline tools (free-thinking-llm-options.md).

Every tool that talks to a model imports from here, so the §7 properties are
implemented once:

- **Pin and cache** -- `call_model` keys on the sha256 of the exact request
  body (model id + prompt + schema collapsed into one hash) and caches the
  response. A cache hit never touches the transport, so reruns over
  identical input are byte-identical; changing model or prompt is an
  explicit, diffable event.
- **One transport seam** -- `transport` is injectable everywhere, so the
  test suite never touches the network and the tools stay deterministic
  under test.

Nothing under obs_pipeline/ may import this module: the runtime path stays
model-free (tests/test_rule_compiler.py enforces the absence).

stdlib only (repo constraint), so the Anthropic Messages API is called over
urllib rather than the SDK. Auth resolves in order: ANTHROPIC_API_KEY env,
ANTHROPIC_AUTH_TOKEN env, a repo-root .env file, then the `ant` CLI's
stored profile.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# Sonnet by default; haiku for cheap comparison runs. Opus is reserved for
# occasional deliberate runs (budget decision, 2026-08-05).
DEFAULT_MODEL = "claude-sonnet-5"
MAX_TOKENS = 16000


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def read_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Minimal KEY=VALUE parser. The key must never be committed (.env is
    gitignored) or printed; this returns it only for the request header."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def auth_headers() -> dict[str, str]:
    env = {**read_dotenv(), **{k: v for k, v in os.environ.items()
                               if k.startswith("ANTHROPIC_")}}
    key = env.get("ANTHROPIC_API_KEY")
    if key:
        return {"x-api-key": key}
    token = env.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        try:
            token = subprocess.run(
                ["ant", "auth", "print-credentials", "--access-token"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except Exception:
            token = ""
    if token:
        # OAuth tokens ride Authorization: Bearer, and /v1/messages requires
        # the oauth beta header alongside it.
        return {"Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20"}
    raise RuntimeError(
        "no API credentials: set ANTHROPIC_API_KEY (env or .env), "
        "ANTHROPIC_AUTH_TOKEN, or log in with `ant auth login`"
    )


def request_body(model: str, prompt: str, schema: dict,
                 max_tokens: int = MAX_TOKENS) -> dict:
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }


def cache_key(body: dict) -> str:
    return sha256(canonical(body).encode("utf-8"))


def http_transport(body: dict) -> dict:
    headers = {"anthropic-version": API_VERSION,
               "content-type": "application/json", **auth_headers()}
    data = json.dumps(body).encode("utf-8")
    last = None
    for attempt in range(4):
        req = urllib.request.Request(API_URL, data=data, headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = RuntimeError(f"API {e.code}: {detail}")
            if e.code in (429, 500, 529) and attempt < 3:
                time.sleep(float(e.headers.get("retry-after") or 2 ** attempt))
                continue
            raise last from None
    raise last


def call_model(body: dict, cache_dir,
               transport=None) -> tuple[dict, str, bool, float]:
    """Returns (api_response, cache_key, was_cached, elapsed_ms). Elapsed is
    0.0 on a cache hit -- no model ran."""
    key = cache_key(body)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key.split(':')[1]}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8")), key, True, 0.0
    start = time.monotonic()
    resp = (transport or http_transport)(body)
    elapsed_ms = (time.monotonic() - start) * 1000.0
    path.write_text(json.dumps(resp, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return resp, key, False, elapsed_ms


def parse_response(resp: dict) -> dict:
    stop = resp.get("stop_reason")
    if stop == "refusal":
        raise RuntimeError(f"model declined the request: "
                           f"{resp.get('stop_details')}")
    if stop == "max_tokens":
        raise RuntimeError("response truncated at max_tokens; raise "
                           "MAX_TOKENS or shrink the input batch")
    text = next(b["text"] for b in resp["content"] if b.get("type") == "text")
    return json.loads(text)
