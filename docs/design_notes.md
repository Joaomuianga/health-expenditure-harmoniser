# Design notes / panel preparation

## Interpretation of the problem
The organisation wants a *repeatable* way to turn heterogeneous national expenditure extracts into one analysable, classified dataset - and to be honest about
what is uncertain. The prototype therefore optimises for: (1) config-not-code country onboarding, (2) uncertainty as a first-class output (three statuses, reasons, alternatives),
(3) full traceability, (4) a human-in-the-loop that *learns as mappings* (decisions become reviewable, versioned mapping changes).

## Why this stack
Python + pandas/openpyxl (all three formats, analyst-friendly, team can maintain) · SQLite (zero-setup, ships in the repo, schema is plain SQL → PostgreSQL) ·
Streamlit (analyst UI in ~300 lines) · YAML/CSV config (SMEs can read and review mappings in a diff). No framework lock-in; each layer is replaceable.

## Classification: why rules + reference mappings, not ML/LLM (yet)
- **Structure carries the signal**: each record has a CoA code; classifying the *account* (≈54 rows) is far more tractable, auditable and reviewable than 7,000 free-text lines.
- **No labelled data** exists to train or evaluate a model; rules give a defensible baseline that *creates* labelled data via analyst review.
- **Uncertainty is explicit**: concept-level prior confidence, +0.05 when text agrees, −0.05 when text absent, 0.35 on code/text conflict; ≥0.75 and no flags → AUTO.
- **Evolution**: (1) SME validates the mapping table; (2) stratified sample audit → measured precision per concept; (3) use analyst decisions as training labels for a text model
  that only *suggests* on REVIEW records; (4) if an LLM is used, treat text as untrusted input: isolated per-record calls, closed label set, schema-constrained output,
  no tools, output validated against the reference lists, and never let a text-derived answer auto-accept.

## Validation plan
Precision/recall per concept on an SME-labelled stratified sample (country × concept × amount band); inter-rater agreement; compare aggregate SHA shares with published
national health accounts; monitor REVIEW rate and override rate over time; regression test set of tricky records (the ones in `tests/`).

## Data-quality handling (what is a rule vs. an assumption)
| Finding | Handling |
|---|---|
| Quoted/comma/"FCFA"/decimal-comma amounts | Parsed; each normalisation logged (info); the single ambiguous pattern flagged (warning) |
| Missing amount (A: 31) | Kept, `amount_usable=0`, excluded from totals |
| Negative amounts (A: 54) | Kept as reversals; totals are net |
| Duplicate transaction id (A) with different content | Both kept, both flagged (ID collision ≠ duplicate) |
| Footer TOTAL row + preamble (B) | Excluded from records; used as control total → reconciles exactly to the parsed sum |
| USD records in RWF file (C: 208) | Converted with reference FX; flagged |
| Dates in 2027 (C: 5) | Kept, flagged outside fiscal period |
| Sub-transactions (C: 59) | Stored, reconcile to parent, never summed |
| Missing description/supplier (C) | Classified on code only (lower confidence) |
| Case variants (A) | Matching is case/accent-insensitive; original preserved |
| Instruction-like text in descriptions (B: 4) | Critical DQ issue; payload stripped from working text; forced to review |
| Ministry uninformative (~80 % of health-account spend is outside the health ministry) | Ministry deliberately not used as a signal |
| Different fiscal years (B is Oct–Sep) | Period check per country; cross-country comparisons caveated |

## Likely questions
- *Why not classify by ministry?* Data show it is noise (see above).
- *Why are ~31 % of records `UNMAPPED`?* They are salaries, capital projects and generic overheads. The supplied HC list has no target for them; forcing a code would overstate
  precision. A production system needs functional allocation keys (staffing, facility type) or the SHA capital account.
- *What would you change first for production?* Mapping governance (versioned, approved), incremental loads with schema-drift checks, PostgreSQL, auth/roles, FX service.
- *How do you add a country?* YAML + CoA rows; unknown accounts fall to review automatically.
- *What if a country has a hierarchical CoA?* Extend `coa_account` with `parent_code`/level; map at the most specific level available and inherit upward.
- *How is a reviewer decision reused?* Account-level decisions apply to all records of that account except flagged exceptions; they are append-only with reviewer/comment/time.
