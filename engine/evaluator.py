"""
Runs a compiled CEL expression against document / application data.

This module has no knowledge of any specific rule -- it just takes whatever
CEL expression string the compiler produced and whatever JSON data the
caller supplies, converts the JSON into CEL runtime types, and evaluates.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import celpy
from celpy import celtypes

_ENV = celpy.Environment()
_PROGRAM_CACHE: dict[str, Any] = {}


class RuleEvaluationError(RuntimeError):
    pass


def _to_cel(value: Any):
    if isinstance(value, bool):
        return celtypes.BoolType(value)
    if isinstance(value, int):
        return celtypes.DoubleType(value)
    if isinstance(value, float):
        return celtypes.DoubleType(value)
    if isinstance(value, str):
        return celtypes.StringType(value)
    if value is None:
        return celtypes.StringType("")
    if isinstance(value, dict):
        return celtypes.MapType({celtypes.StringType(k): _to_cel(v) for k, v in value.items()})
    if isinstance(value, list):
        return celtypes.ListType([_to_cel(v) for v in value])
    raise RuleEvaluationError(f"Cannot convert value of type {type(value)} to CEL")


def _get_program(cel_expression: str):
    program = _PROGRAM_CACHE.get(cel_expression)
    if program is None:
        ast = _ENV.compile(cel_expression)
        program = _ENV.program(ast)
        _PROGRAM_CACHE[cel_expression] = program
    return program


@dataclass
class EvaluationResult:
    passed: bool
    error: str | None = None


def evaluate_expression(
    cel_expression: str,
    document: dict,
    application: dict | None = None,
    now: dt.datetime | None = None,
) -> EvaluationResult:
    """Evaluate one CEL expression string against document/application JSON."""
    now = now or dt.datetime.now(dt.timezone.utc)
    context = {
        "doc": _to_cel(document),
        "application": _to_cel(application or {}),
        "now": celtypes.StringType(now.strftime("%Y-%m-%dT%H:%M:%SZ")),
    }
    try:
        program = _get_program(cel_expression)
        result = program.evaluate(context)
        return EvaluationResult(passed=bool(result))
    except Exception as exc:  # celpy raises various internal error types
        return EvaluationResult(passed=False, error=str(exc))
