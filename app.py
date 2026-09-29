"""
Streamlit demo: rule-definition-driven CEL compilation + Z3 verification.

Panels:
  1. Rule definition (editable JSON)         -> compiler.compile_rule
  2. Generated CEL expression (read-only)    -> shown as-is, never hand-edited
  3. Sample document JSON (editable)         -> evaluator.evaluate_expression
  4. Result panel (PASS/FAIL + Z3 "Verify")  -> verifier.verify_rule / check_equivalence

Nothing here is hardcoded per rule: every rule_type, field, value, and
document type comes from the JSON files under data/.
"""
import copy
import datetime as dt
import json
from pathlib import Path

import streamlit as st

from engine.compiler import RuleCompileError, compile_rule
from engine.evaluator import evaluate_expression
from engine.verifier import VerificationError, check_equivalence, verify_rule

DATA_DIR = Path(__file__).parent / "data"

st.set_page_config(page_title="CEL + Z3 Rule Validation POC", layout="wide")


@st.cache_data
def load_json(path: Path):
    return json.loads(path.read_text())


def document_type_options():
    doc_files = sorted((DATA_DIR / "documents").glob("*.json"))
    return {load_json(f)["document_type"]: f for f in doc_files}


def rules_for(doc_type_file: Path):
    doc_def = load_json(doc_type_file)
    rules_file = DATA_DIR / doc_def["rule_definitions_file"]
    return doc_def, load_json(rules_file)


def samples_for(doc_type: str):
    sample_file = DATA_DIR / "sample_docs" / f"{doc_type}_samples.json"
    if sample_file.exists():
        return load_json(sample_file)
    return []


st.title("CEL + Z3 Rule Validation POC")
st.caption(
    "Fortress publishes acceptance criteria as data (JSON). This app compiles that data "
    "into a CEL expression at runtime, evaluates it against a document, and can formally "
    "verify it with Z3 — with zero rule-specific code."
)

doc_types = document_type_options()
col_a, col_b = st.columns([1, 1])
with col_a:
    doc_type = st.selectbox("Document type", list(doc_types.keys()))

doc_def, rules = rules_for(doc_types[doc_type])
samples = samples_for(doc_type)

with col_b:
    rule_ids = [r["rule_id"] for r in rules]
    selected_rule_id = st.selectbox("Rule", rule_ids)

selected_rule = next(r for r in rules if r["rule_id"] == selected_rule_id)

sample_labels = [s["label"] for s in samples] + ["(custom)"]
selected_sample_label = st.selectbox("Sample document", sample_labels)

if selected_sample_label == "(custom)":
    default_document = {f["name"]: "" for f in doc_def["fields"]}
    default_application = {}
else:
    sample = next(s for s in samples if s["label"] == selected_sample_label)
    default_document = sample["document"]
    default_application = sample["application"]

now_default = load_json(DATA_DIR / "reference_now.json")["now"]

st.divider()
left, right = st.columns(2)

with left:
    st.subheader("1. Rule definition (editable JSON)")
    rule_def_text = st.text_area(
        "This is the data Fortress would publish for this rule.",
        value=json.dumps(selected_rule, indent=2),
        height=220,
        key=f"rule_def_{doc_type}_{selected_rule_id}",
    )

    st.subheader("3. Sample document JSON (editable)")
    doc_json_text = st.text_area(
        "Document fields extracted from the upload",
        value=json.dumps(default_document, indent=2),
        height=180,
        key=f"doc_{doc_type}_{selected_rule_id}_{selected_sample_label}",
    )
    app_json_text = st.text_area(
        "Application fields (used by matches_field rules)",
        value=json.dumps(default_application, indent=2),
        height=100,
        key=f"app_{doc_type}_{selected_rule_id}_{selected_sample_label}",
    )
    now_text = st.text_input("Evaluate as of (RFC3339 timestamp)", value=now_default)

with right:
    st.subheader("2. Generated CEL expression")
    st.caption("Auto-generated from the rule definition above — never hand-written.")

    rule_def = None
    document = None
    application = None
    now = None
    parse_error = None

    try:
        rule_def = json.loads(rule_def_text)
    except json.JSONDecodeError as e:
        parse_error = f"Rule definition JSON error: {e}"

    try:
        document = json.loads(doc_json_text)
    except json.JSONDecodeError as e:
        parse_error = parse_error or f"Document JSON error: {e}"

    try:
        application = json.loads(app_json_text)
    except json.JSONDecodeError as e:
        parse_error = parse_error or f"Application JSON error: {e}"

    try:
        now = dt.datetime.strptime(now_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError as e:
        parse_error = parse_error or f"'Evaluate as of' timestamp error: {e}"

    compiled = None
    if parse_error is None:
        try:
            compiled = compile_rule(rule_def)
            st.code(compiled.cel_expression, language="text")
        except RuleCompileError as e:
            parse_error = f"Compile error: {e}"

    if parse_error:
        st.error(parse_error)

    st.subheader("4. Result")
    if compiled is not None:
        eval_result = evaluate_expression(compiled.cel_expression, document, application, now)
        if eval_result.error:
            st.error(f"Evaluation error: {eval_result.error}")
        elif eval_result.passed:
            st.success("PASS")
        else:
            st.error("FAIL")

        if st.button("Verify this rule with Z3"):
            try:
                verification = verify_rule(rule_def)
                if verification.proven_safe:
                    st.success(verification.message)
                else:
                    st.warning(verification.message)
                    if verification.counterexample:
                        st.json(verification.counterexample)
            except VerificationError as e:
                st.info(f"No Z3 model registered for this rule_type yet: {e}")

st.divider()
st.subheader("Rule equivalence check (Z3)")
st.caption(
    "Compare two versions of the same rule_type (e.g. the same rule_id with a different "
    "threshold) and let Z3 prove whether they behave identically or find where they diverge."
)

eq_col1, eq_col2 = st.columns(2)
with eq_col1:
    st.markdown("**Version A**")
    version_a_text = st.text_area(
        "Version A rule definition",
        value=json.dumps(selected_rule, indent=2),
        height=180,
        key="version_a",
        label_visibility="collapsed",
    )
with eq_col2:
    st.markdown("**Version B**")
    changed_rule = copy.deepcopy(selected_rule)
    if "value" in changed_rule and isinstance(changed_rule["value"], (int, float)):
        changed_rule["value"] = changed_rule["value"] + 30
    version_b_text = st.text_area(
        "Version B rule definition",
        value=json.dumps(changed_rule, indent=2),
        height=180,
        key="version_b",
        label_visibility="collapsed",
    )

if st.button("Check equivalence with Z3"):
    try:
        rule_a = json.loads(version_a_text)
        rule_b = json.loads(version_b_text)
        eq_result = check_equivalence(rule_a, rule_b)
        if eq_result.proven_safe:
            st.success(eq_result.message)
        else:
            st.warning(eq_result.message)
            if eq_result.counterexample:
                st.json(eq_result.counterexample)
    except (json.JSONDecodeError, VerificationError) as e:
        st.error(str(e))
