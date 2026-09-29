"""
Rule-definition -> CEL compiler.

Takes a rule definition (plain JSON/dict matching the generic schema) and
produces a CEL expression string at runtime. The Python code below only
knows the *shape* of each rule_type ("a within_days rule looks like this").
It never contains a specific field name, threshold, or comparison target --
those are substituted in from whatever rule definition is loaded.

Adding a brand-new rule_type means adding one entry to RULE_TYPE_TEMPLATES
below (still generic, not tied to any field/value) -- see README for a
worked example.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class RuleCompileError(ValueError):
    pass


def _validate_ident(name: str, what: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise RuleCompileError(f"Invalid {what}: {name!r} is not a safe identifier")
    return name


def _validate_path(path: str, what: str) -> str:
    if not isinstance(path, str) or not _PATH_RE.match(path):
        raise RuleCompileError(f"Invalid {what}: {path!r} is not a safe dotted path")
    return path


def _cel_number(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuleCompileError(f"Expected a number, got {value!r}")
    return repr(value)


def _cel_string_literal(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _cel_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _cel_number(value)
    if isinstance(value, str):
        return _cel_string_literal(value)
    raise RuleCompileError(f"Unsupported literal type for value: {type(value)}")


# ---------------------------------------------------------------------------
# Generic rule_type -> CEL template builders.
#
# Each builder receives ONLY the rule definition dict and returns a CEL
# expression string. It must never hardcode a field name or a threshold --
# those always come from `rule_def`.
# ---------------------------------------------------------------------------

def _build_within_days(rule_def: dict) -> str:
    field = _validate_ident(rule_def["field"], "field")
    days = rule_def["value"]
    if isinstance(days, bool) or not isinstance(days, (int, float)) or days < 0:
        raise RuleCompileError(f"within_days requires a non-negative numeric value, got {days!r}")
    hours_literal = _cel_string_literal(f"{days * 24}h")
    return (
        f"has(doc.{field}) && "
        f"(timestamp(now) - timestamp(doc.{field}) <= duration({hours_literal})) && "
        f"(timestamp(doc.{field}) - timestamp(now) <= duration({hours_literal}))"
    )


def _build_greater_than(rule_def: dict) -> str:
    field = _validate_ident(rule_def["field"], "field")
    value = _cel_number(rule_def["value"])
    return f"has(doc.{field}) && doc.{field} > {value}"


def _build_less_than(rule_def: dict) -> str:
    field = _validate_ident(rule_def["field"], "field")
    value = _cel_number(rule_def["value"])
    return f"has(doc.{field}) && doc.{field} < {value}"


def _build_equals(rule_def: dict) -> str:
    field = _validate_ident(rule_def["field"], "field")
    value = _cel_literal(rule_def["value"])
    return f"has(doc.{field}) && doc.{field} == {value}"


def _build_matches_field(rule_def: dict) -> str:
    field = _validate_ident(rule_def["field"], "field")
    compare_to = _validate_path(rule_def["compare_to"], "compare_to")
    return f"has(doc.{field}) && doc.{field} == {compare_to}"


def _build_non_empty(rule_def: dict) -> str:
    field = _validate_ident(rule_def["field"], "field")
    return f"has(doc.{field}) && doc.{field} != ''"


RULE_TYPE_TEMPLATES: dict[str, Callable[[dict], str]] = {
    "within_days": _build_within_days,
    "greater_than": _build_greater_than,
    "less_than": _build_less_than,
    "equals": _build_equals,
    "matches_field": _build_matches_field,
    "non_empty": _build_non_empty,
}


@dataclass
class CompiledRule:
    rule_id: str
    rule_type: str
    cel_expression: str
    rule_def: dict


def compile_rule(rule_def: dict) -> CompiledRule:
    """Compile a single rule definition dict into a CEL expression string."""
    rule_type = rule_def.get("rule_type")
    builder = RULE_TYPE_TEMPLATES.get(rule_type)
    if builder is None:
        known = ", ".join(sorted(RULE_TYPE_TEMPLATES))
        raise RuleCompileError(f"Unknown rule_type {rule_type!r}. Known types: {known}")
    expression = builder(rule_def)
    return CompiledRule(
        rule_id=rule_def.get("rule_id", "<unnamed>"),
        rule_type=rule_type,
        cel_expression=expression,
        rule_def=rule_def,
    )


def compile_ruleset(rule_defs: list[dict]) -> list[CompiledRule]:
    return [compile_rule(rd) for rd in rule_defs]
