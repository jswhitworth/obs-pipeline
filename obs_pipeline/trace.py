"""Content-addressed derivation trace (design doc §9.2, §9.3).

Trace by construction: a Traced value cannot be created except through
Tracer.step(), so a code path that produces a value necessarily emitted a
step for it. See §9.3 -- reconstruction-style tracing drifts from reality
precisely when the code is buggy, which is when the trace is needed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, InitVar
from typing import Any, Generic, Iterable, TypeVar

T = TypeVar("T")


def canonical_json(obj: Any) -> str:
    """Stable JSON: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def _build():
    """Build Traced and Tracer with a closure-bound sentinel for construction control.

    The sentinel is not a module attribute; it lives only in this closure.
    This prevents external code from forging Traced values via import tricks.
    """
    sentinel = object()

    @dataclass(frozen=True)
    class Traced(Generic[T]):
        """A value plus the id of the step that derived it."""
        value: T
        step_id: str
        _guard: InitVar[object] = None

        def __post_init__(self, _guard: object) -> None:
            if _guard is not sentinel:
                raise TypeError(
                    "Traced values are constructible only through "
                    "Tracer.step() — see module docstring"
                )

    class Tracer:
        def __init__(self) -> None:
            self._steps: dict[str, dict] = {}

        def step(
            self,
            *,
            op: str,
            rule_id: str | None = None,
            inputs: Iterable[str] = (),
            output: Any = None,
            parents: Iterable[Traced] = (),
            **extra: Any,
        ) -> Traced:
            body: dict[str, Any] = {
                "op": op,
                "rule_id": rule_id,
                "inputs": list(inputs),
                "output": output,
                "parents": [p.step_id for p in parents],
            }
            body.update(extra)
            step_id = "sha256:" + hashlib.sha256(
                canonical_json(body).encode("utf-8")
            ).hexdigest()
            self._steps.setdefault(step_id, {"step_id": step_id, **body})
            return Traced(output, step_id, _guard=sentinel)

        def steps(self) -> list[dict]:
            """All steps, sorted by step_id so two identical runs write identical files."""
            return [self._steps[k] for k in sorted(self._steps)]

    return Traced, Tracer


Traced, Tracer = _build()
