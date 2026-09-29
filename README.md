# CEL + Z3 Rule Validation POC (for CORA)

This is a proof of concept for one specific architectural claim: **acceptance
criteria for document review can live entirely as data (JSON), and a small,
generic compiler can turn that data into a working, executable check at
runtime — with zero hand-written, per-rule code.** If Fortress changes "60
days" to "90 days", or adds a brand-new rule tomorrow, that should only ever
require an edit to a JSON file, never a code change.

It only covers **deterministic rules** (structured yes/no checks like "date
must be within N days" or "employer name must match the application"). It
does not cover AI-judgment rules (legibility, completeness) — that's out of
scope here.

## What CEL and Z3 are, and why they're paired here

**[CEL](https://cel.dev/)** (Common Expression Language) is Google's small,
sandboxed expression language, purpose-built for exactly this kind of thing:
a short expression that reads some structured data and evaluates to `true`
or `false`. It's safe to evaluate untrusted, dynamically-generated
expressions with it — there's no way to write a CEL expression that reads a
file, makes a network call, or loops forever. That safety property is why
it's the right fit for "compile Fortress's rule JSON into an expression and
run it" instead of, say, `eval()`-ing Python.

**[Z3](https://github.com/Z3Prover/z3)** is Microsoft Research's SMT
(theorem-proving) solver. Where testing a rule against a handful of sample
documents can only ever show "it worked on the cases I thought of," Z3 can
reason about *every possible input at once* and either prove a property
holds for all of them, or hand back a concrete counterexample where it
doesn't. That's exactly what you want for boundary conditions — "is a
document dated *exactly* 60 days ago accepted or rejected, and is that
consistent with the compiled expression for every possible date?" — and for
regression checking when a rule changes ("did raising the threshold from 60
to 90 days change anything besides the threshold?").

They're paired because CEL produces the rule as a runtime artifact and Z3
independently checks that artifact's *logic* for correctness across the
entire input space, not just the cases someone happened to type into a
sample document.

### What this POC does and doesn't prove (read this before trusting the "Verify" button)

A full formal bridge from a parsed CEL AST into Z3 constraints is a bigger
undertaking than a POC calls for, and a mature, off-the-shelf one isn't
readily available. So the verifier (`engine/verifier.py`) does the following
instead, and the UI labels it accordingly:

For each `rule_type`, there are two independent Z3 encodings, both built
generically from whatever `rule_def` is loaded (never a hardcoded field name
or threshold):

1. A **generated-model** encoding that mirrors the same template the CEL
   compiler uses (e.g. for `within_days`, it encodes the same closed-interval
   `<=` comparison in both directions that `compiler.py` emits).
2. An independently-written **reference-spec** encoding of what the rule
   *should* mean in plain English (e.g. "within N days" = the closed
   interval `[-N days, +N days]`).

Z3 then proves whether these two agree for every possible input, or returns
a concrete counterexample where they don't. This is **boundary + property
verification via Z3** — it proves the generic *logic* is boundary-correct
and internally consistent, not that the CEL runtime's actual execution of
the generated string is bug-free. (If you introduce a bug in the generated
model — e.g. change `<=` to `<` — Z3 will find the exact boundary value
where it diverges from the reference spec; this was verified while building
this POC.)

## The core thing that got built: a rule-definition-to-CEL compiler

`engine/compiler.py` defines a generic rule schema:

```json
{
  "rule_id": "date_within_range",
  "rule_type": "within_days",
  "field": "document_date",
  "value": 60
}
```

Supported `rule_type`s: `within_days`, `greater_than`, `less_than`, `equals`,
`matches_field` (compares a document field to another field, e.g.
`application.employer_name`), `non_empty`.

`compile_rule(rule_def)` reads the `rule_type` and builds the matching CEL
expression string, substituting in `field` / `value` / `compare_to` from the
data. **No field name, threshold, or comparison target is ever written
directly into a CEL string in the code** — the code only knows the *shape*
of a `within_days`-style expression in general.

Document type definitions (`data/documents/*.json`) are data too: they
declare what fields exist for a document type and which rule-definitions
file applies to it.

```
engine/
  compiler.py   rule_def -> CEL expression string (the compiler)
  evaluator.py  runs a CEL expression against document/application JSON (cel-python)
  verifier.py   Z3 boundary + equivalence checks, generic per rule_type
data/
  documents/            document type definitions (fields + which rules file applies)
  rules/                rule definitions per document type
  sample_docs/          sample extracted-document JSON to test against
  reference_now.json    the "as of" date the sample documents were authored against
app.py          Streamlit demo UI
```

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

(On Windows, if `streamlit` isn't on PATH, use `python -m streamlit run app.py`.)

Opens at `http://localhost:8501`.

### Run with Docker

Port `8501` is used because it is the Streamlit default and was free when
the Docker setup was added.

```bash
docker compose up --build
```

Then open `http://localhost:8501`.

## Using the demo

1. Pick a **document type** (`pay_stub` or `income_letter`) and a **rule**.
2. Pick a **sample document**, or select `(custom)` and type your own.
3. The **rule definition** panel (top-left) is live JSON — edit it (e.g.
   change `"value": 60` to `"value": 90`, or change the `field`) and the
   **generated CEL expression** panel updates immediately with zero code
   changes.
4. The **result panel** shows live PASS/FAIL for the sample document against
   the (possibly edited) rule.
5. Click **"Verify this rule with Z3"** to run the boundary/property proof
   described above, in plain language.
6. Further down, the **rule equivalence** section lets you compare two
   versions of a rule definition (it pre-fills version B with the threshold
   raised by 30) and asks Z3 to prove they're identical or find a concrete
   date/value where they diverge.

The demo moment that matters: change `60` to `90` in the rule definition
panel and watch the generated expression, the PASS/FAIL result, and the Z3
proof all update correctly — nothing here is hardcoded per rule.

## Adding a brand-new rule type or document type (config/data only)

**New rule on an existing document type:** add an entry to
`data/rules/<doc_type>_rules.json`, e.g.:

```json
{
  "rule_id": "ytd_gross_below_cap",
  "rule_type": "less_than",
  "field": "ytd_gross",
  "value": 500000
}
```

No code changes — the compiler already knows how to build a `less_than`
expression generically.

**New document type:** add `data/documents/bank_statement.json` (fields +
`rule_definitions_file`) and `data/rules/bank_statement_rules.json` (its
rules), plus optionally `data/sample_docs/bank_statement_samples.json`. The
UI's document-type dropdown picks it up automatically because it scans
`data/documents/*.json`.

**New rule_type** (worked example — adding `not_equals`):

1. In `engine/compiler.py`, add a builder function:
   ```python
   def _build_not_equals(rule_def: dict) -> str:
       field = _validate_ident(rule_def["field"], "field")
       value = _cel_literal(rule_def["value"])
       return f"has(doc.{field}) && doc.{field} != {value}"
   ```
   and register it: `RULE_TYPE_TEMPLATES["not_equals"] = _build_not_equals`.
2. (Optional, for Z3 verification) add a matching entry to
   `_RULE_TYPE_MODELS` in `engine/verifier.py` with a generated/reference Z3
   encoding, mirroring `_build_not_equals`'s logic in Z3 terms.
3. Any rule definition JSON can now use `"rule_type": "not_equals"` —
   nothing else changes.

## Sample data

- `data/rules/pay_stub_rules.json`, `data/rules/income_letter_rules.json` —
  a handful of rules per document type covering date ranges, positive-number
  checks, field matching, and presence checks.
- `data/sample_docs/pay_stub_samples.json`,
  `data/sample_docs/income_letter_samples.json` — sample documents that
  clearly PASS every rule, clearly FAIL specific rules, and at least one
  genuine boundary case per date/number rule (e.g. a document dated *exactly*
  on the day-limit, or a value of exactly `0` against a `greater_than 0`
  check) to demonstrate why boundary testing matters.
- `data/reference_now.json` — the fixed "as of" date the sample documents'
  dates are relative to. If it goes stale, either bump this file or just
  change the "Evaluate as of" field in the UI.

All of the above is input data. None of it requires touching compiler,
evaluator, or verifier code to add, remove, or change a rule or document
type.

## What NOT to build (scope boundaries, for reference)

- No AI-judgment rules (legibility, completeness) — deterministic rules only.
- No real integration with Fortress or CORA's APIs — standalone, local
  sample data only.
- No authentication, database, or persistence layer — in-memory / file-based
  sample data is fine for a POC.
