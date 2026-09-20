# Architecture

```mermaid
flowchart LR
  subgraph Sources
    A[Country A CSV]:::s
    B[Country B Excel + CoA sheet]:::s
    C[Country C JSON, nested splits]:::s
  end
  Y[(config/countries/*.yaml<br/>columns, formats, currency, fiscal period)]:::c
  A & B & C --> AD[Adapters<br/>csv / excel / json<br/>read only]
  Y -.-> AD
  AD --> RAW[(raw_record<br/>immutable payload + file sha256 + locator)]
  RAW --> H[Harmonise<br/>canonical fields, parse amount/date,<br/>text-trust check, FX]
  Y -.-> H
  H --> EXP[(expenditure<br/>+ expenditure_split)]
  H --> DQ[(dq_issue)]
  EXP --> Q[Cross-record checks<br/>duplicates, control totals, ministry signal]
  Q --> DQ
  MAP[(coa_account → concept → SHA / SRHR<br/>keyword_rule)]:::c --> CL[Classify<br/>code ▸ text corroboration ▸ routing]
  EXP --> CL
  CL --> CLS[(classification<br/>AUTO / REVIEW / UNMAPPED + rationale)]
  CLS --> V{{v_effective}}
  REV[(review_decision<br/>append-only)] --> V
  V --> UI[Streamlit workbench<br/>overview · review · DQ · explore · trace · audit]
  UI --> REV
  classDef s fill:#eef,stroke:#446; classDef c fill:#efe,stroke:#464;
```

## Where logic lives
| Concern | Location | Changes when… |
|---|---|---|
| File layout, column names, date format, currency, fiscal window | `config/countries/<CC>.yaml` | a country/format changes |
| Value parsing (numbers, dates, text hygiene, injection check) | `parsers.py`, `security.py` | a new pattern is discovered |
| Account → concept mapping | `config/classification/coa_map.csv` | a country adds/changes accounts |
| Concept → SHA / SRHR + confidence + rationale | `config/classification/concepts.csv` | the analytical framework or SME view changes |
| Routing thresholds | `classify.py` (`AUTO_THRESHOLD`) | policy on auto-acceptance changes |

## Data model (SQLite; ANSI-friendly)
`country, ref_sha, ref_srhr, fx_rate` (reference) · `concept, coa_account, keyword_rule` (classification config) ·
`pipeline_run, source_file, raw_record` (lineage) · `expenditure, expenditure_split` (harmonised) · `dq_issue` ·
`classification` (machine proposal) · `review_decision` (human, append-only) · view `v_effective` (record decision > account decision > machine proposal).

**Why a "concept" layer?** Country A/B/C use 20/18/16 different account codes for overlapping activities. Mapping each country's
account to a shared concept (≈25 rows) and each concept to SHA/SRHR once means a new country only needs *account → concept* rows; the analytical
mapping (and SME sign-off on it) is done once and is reusable. It also makes divergence visible (e.g. B's "maternal-health awareness" vs A/C's "antenatal outreach" land on different concepts and SHA codes).

## Traceability
Every harmonised row links `expenditure.raw_id → raw_record → source_file (name, sha256, format, load time, pipeline run + config hash)` and stores the exact source
locator (CSV line, Excel sheet!row, JSON path). The original payload is kept as JSON. Human decisions carry reviewer, timestamp, scope and comment.

## Moving to production (not built)
PostgreSQL + migrations; object storage for raw files; orchestrated, incremental loads with schema-drift detection; authentication/roles and row-level audit;
country-specific validation rules owned with country teams; versioned mapping tables with approval workflow; calibrated FX service; SHA-2011 full
implementation (financing schemes, providers, capital account); monitoring of AUTO precision via periodic SME sampling; containerised deployment.
