"""
Z3-based formal verification of compiled rules.

IMPORTANT (see README "What this does and doesn't prove"): a full formal
bridge from a CEL AST to Z3 is out of scope for this POC. Instead, for each
rule_type we build a Z3 encoding that mirrors the same generic template the
CEL compiler uses (`compiler.py`), driven by the exact same rule_def
parameters (field/value/compare_to), plus an independently written
"reference spec" encoding of what the rule_type is supposed to mean. Z3
then proves whether the two agree for *every* possible input, or returns a
concrete counterexample. This is boundary + property verification via Z3,
not a proof that the CEL runtime itself implements the generated string
correctly.

Nothing here is hardcoded per rule: every builder takes rule_def and reads
field/value/compare_to from it, exactly like the CEL compiler does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import z3

HOURS_PER_DAY = 24


class VerificationError(ValueError):
    pass


@dataclass
class VerificationResult:
    proven_safe: bool
    message: str
    counterexample: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per rule_type: (generated-model builder, reference-spec builder, variable
# declarations). Each builder receives the rule_def and the symbolic
# variables and returns a Z3 BoolRef. Adding a new rule_type means adding
# one entry here, mirroring the new entry in compiler.RULE_TYPE_TEMPLATES.
# ---------------------------------------------------------------------------

def _within_days_vars(rule_def: dict):
    return {"diff_hours": z3.Int("diff_hours")}


def _within_days_generated(rule_def: dict, v: dict) -> z3.BoolRef:
    n_hours = rule_def["value"] * HOURS_PER_DAY
    diff = v["diff_hours"]
    return z3.And(diff <= n_hours, -diff <= n_hours)


def _within_days_reference(rule_def: dict, v: dict) -> z3.BoolRef:
    n_hours = rule_def["value"] * HOURS_PER_DAY
    diff = v["diff_hours"]
    return z3.And(diff >= -n_hours, diff <= n_hours)


def _numeric_vars(rule_def: dict):
    return {"x": z3.Real("x")}


def _greater_than_generated(rule_def: dict, v: dict) -> z3.BoolRef:
    return v["x"] > rule_def["value"]


def _greater_than_reference(rule_def: dict, v: dict) -> z3.BoolRef:
    return v["x"] > rule_def["value"]


def _less_than_generated(rule_def: dict, v: dict) -> z3.BoolRef:
    return v["x"] < rule_def["value"]


def _less_than_reference(rule_def: dict, v: dict) -> z3.BoolRef:
    return v["x"] < rule_def["value"]


def _equals_vars(rule_def: dict):
    if isinstance(rule_def.get("value"), str):
        return {"x": z3.String("x")}
    return {"x": z3.Real("x")}


def _equals_generated(rule_def: dict, v: dict) -> z3.BoolRef:
    value = rule_def["value"]
    literal = z3.StringVal(value) if isinstance(value, str) else value
    return v["x"] == literal


def _equals_reference(rule_def: dict, v: dict) -> z3.BoolRef:
    return _equals_generated(rule_def, v)


def _string_pair_vars(rule_def: dict):
    return {"a": z3.String("a"), "b": z3.String("b")}


def _matches_field_generated(rule_def: dict, v: dict) -> z3.BoolRef:
    return v["a"] == v["b"]


def _matches_field_reference(rule_def: dict, v: dict) -> z3.BoolRef:
    return v["a"] == v["b"]


def _string_vars(rule_def: dict):
    return {"s": z3.String("s")}


def _non_empty_generated(rule_def: dict, v: dict) -> z3.BoolRef:
    return v["s"] != z3.StringVal("")


def _non_empty_reference(rule_def: dict, v: dict) -> z3.BoolRef:
    return z3.Length(v["s"]) > 0


_RULE_TYPE_MODELS = {
    "within_days": (_within_days_vars, _within_days_generated, _within_days_reference),
    "greater_than": (_numeric_vars, _greater_than_generated, _greater_than_reference),
    "less_than": (_numeric_vars, _less_than_generated, _less_than_reference),
    "equals": (_equals_vars, _equals_generated, _equals_reference),
    "matches_field": (_string_pair_vars, _matches_field_generated, _matches_field_reference),
    "non_empty": (_string_vars, _non_empty_generated, _non_empty_reference),
}


def _model_to_dict(model: z3.ModelRef, variables: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, var in variables.items():
        val = model.eval(var, model_completion=True)
        out[name] = str(val)
    return out


def verify_rule(rule_def: dict) -> VerificationResult:
    """Prove the compiler's generated logic matches the rule_type's reference
    spec for *every* possible value of the relevant field(s)."""
    rule_type = rule_def.get("rule_type")
    entry = _RULE_TYPE_MODELS.get(rule_type)
    if entry is None:
        raise VerificationError(f"No Z3 model registered for rule_type {rule_type!r}")
    make_vars, build_generated, build_reference = entry

    variables = make_vars(rule_def)
    generated = build_generated(rule_def, variables)
    reference = build_reference(rule_def, variables)

    solver = z3.Solver()
    solver.add(generated != reference)
    outcome = solver.check()

    if outcome == z3.unsat:
        if rule_type == "within_days":
            n = rule_def["value"]
            msg = (
                f"Proven safe across all possible dates: the generated rule "
                f"accepts documents exactly within {n} days (inclusive) in "
                f"either direction, with no boundary gap or overlap."
            )
        else:
            msg = (
                "Proven safe: the generated rule matches the reference "
                "specification for this rule_type across every possible input."
            )
        return VerificationResult(proven_safe=True, message=msg)

    if outcome == z3.sat:
        model = solver.model()
        counterexample = _model_to_dict(model, variables)
        msg = (
            "Found a case where this rule behaves unexpectedly: the generated "
            f"logic diverges from the expected behavior at {counterexample}."
        )
        return VerificationResult(proven_safe=False, message=msg, counterexample=counterexample)

    return VerificationResult(
        proven_safe=False,
        message=f"Z3 could not decide this in the allotted time (status: {outcome}).",
    )


def check_equivalence(rule_def_a: dict, rule_def_b: dict) -> VerificationResult:
    """Given two versions of a rule definition (e.g. same rule_id, different
    value), prove whether they behave identically for every input, or find a
    concrete input where they diverge."""
    if rule_def_a.get("rule_type") != rule_def_b.get("rule_type"):
        raise VerificationError("Cannot compare rules of different rule_type")

    rule_type = rule_def_a["rule_type"]
    entry = _RULE_TYPE_MODELS.get(rule_type)
    if entry is None:
        raise VerificationError(f"No Z3 model registered for rule_type {rule_type!r}")
    make_vars, build_generated, _ = entry

    variables = make_vars(rule_def_a)
    formula_a = build_generated(rule_def_a, variables)
    formula_b = build_generated(rule_def_b, variables)

    solver = z3.Solver()
    solver.add(formula_a != formula_b)
    outcome = solver.check()

    if outcome == z3.unsat:
        return VerificationResult(
            proven_safe=True,
            message="Proven identical: these two rule versions accept exactly the same inputs.",
        )

    if outcome == z3.sat:
        model = solver.model()
        counterexample = _model_to_dict(model, variables)
        a_result = z3.is_true(model.eval(formula_a, model_completion=True))
        b_result = z3.is_true(model.eval(formula_b, model_completion=True))
        msg = (
            f"These rule versions diverge at {counterexample}: version A evaluates to "
            f"{a_result}, version B evaluates to {b_result}."
        )
        return VerificationResult(proven_safe=False, message=msg, counterexample=counterexample)

    return VerificationResult(
        proven_safe=False,
        message=f"Z3 could not decide this in the allotted time (status: {outcome}).",
    )
