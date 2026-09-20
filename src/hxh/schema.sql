-- Health-expenditure harmoniser: SQLite schema (portable to PostgreSQL with minor type changes)
-- Layers: reference/config -> raw (landing, immutable copy of source) -> harmonised -> classification -> review

-- ---------- reference & configuration (seeded from /config, version-controlled) ----------
CREATE TABLE IF NOT EXISTS country     (country_code TEXT PRIMARY KEY, country_name TEXT, primary_currency TEXT, language TEXT);
CREATE TABLE IF NOT EXISTS ref_sha     (sha_code TEXT PRIMARY KEY, sha_description TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS ref_srhr    (srhr_code TEXT PRIMARY KEY, srhr_description TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS fx_rate     (currency TEXT PRIMARY KEY, per_usd REAL NOT NULL, note TEXT);
-- canonical "concept" layer: the bridge between country-specific accounts and the analytical classifications
CREATE TABLE IF NOT EXISTS concept (
  concept_id TEXT PRIMARY KEY, description TEXT, target TEXT CHECK (target IN ('MAPPABLE','NO_TARGET')),
  sha_code TEXT REFERENCES ref_sha(sha_code), srhr_code TEXT REFERENCES ref_srhr(srhr_code),
  base_confidence REAL, alt_sha_code TEXT, rationale TEXT);
CREATE TABLE IF NOT EXISTS coa_account (          -- one row per country-specific account code
  country_code TEXT REFERENCES country(country_code), account_code TEXT, account_label TEXT,
  concept_id TEXT REFERENCES concept(concept_id), PRIMARY KEY (country_code, account_code));
CREATE TABLE IF NOT EXISTS keyword_rule (rule_id TEXT PRIMARY KEY, priority INT, pattern TEXT, concept_id TEXT, lang TEXT);

-- ---------- lineage ----------
CREATE TABLE IF NOT EXISTS pipeline_run (run_id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, finished_at TEXT, config_hash TEXT, notes TEXT);
CREATE TABLE IF NOT EXISTS source_file (
  file_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INT, country_code TEXT, file_name TEXT, sha256 TEXT, format TEXT,
  n_raw_rows INT, control_total_raw TEXT, control_total_value REAL, metadata_json TEXT, loaded_at TEXT);
CREATE TABLE IF NOT EXISTS raw_record (            -- exact source payload, untouched
  raw_id INTEGER PRIMARY KEY AUTOINCREMENT, file_id INT REFERENCES source_file(file_id),
  source_locator TEXT, record_key TEXT UNIQUE, record_type TEXT, payload_json TEXT);

-- ---------- harmonised ----------
CREATE TABLE IF NOT EXISTS expenditure (
  exp_id INTEGER PRIMARY KEY AUTOINCREMENT, raw_id INT UNIQUE REFERENCES raw_record(raw_id), record_key TEXT UNIQUE,
  run_id INT, country_code TEXT REFERENCES country(country_code), source_txn_id TEXT,
  txn_date TEXT, in_expected_period INT, ministry_code TEXT, ministry_name TEXT,
  account_code TEXT, description_original TEXT, description_used TEXT, text_trust TEXT,   -- OK | SUSPECT | MISSING
  supplier TEXT, amount_original REAL, currency_original TEXT, amount_lcu REAL, amount_usd_ref REAL,
  is_reversal INT DEFAULT 0, amount_usable INT DEFAULT 1, n_splits INT DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_exp_country ON expenditure(country_code, account_code);
CREATE TABLE IF NOT EXISTS expenditure_split (split_id INTEGER PRIMARY KEY AUTOINCREMENT, exp_id INT REFERENCES expenditure(exp_id),
  source_split_id TEXT, description TEXT, amount REAL);   -- kept for traceability, NEVER summed (parent already carries the total)

CREATE TABLE IF NOT EXISTS dq_issue (
  issue_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INT, file_id INT, raw_id INT, exp_id INT,
  rule TEXT, severity TEXT CHECK (severity IN ('info','warning','error','critical')), message TEXT, action_taken TEXT);
CREATE INDEX IF NOT EXISTS ix_dq_rule ON dq_issue(rule);
CREATE INDEX IF NOT EXISTS ix_dq_exp ON dq_issue(exp_id);

-- ---------- classification (machine proposal) ----------
CREATE TABLE IF NOT EXISTS classification (
  exp_id INTEGER PRIMARY KEY REFERENCES expenditure(exp_id), run_id INT,
  concept_id TEXT, text_concept_id TEXT, sha_code TEXT, srhr_code TEXT, alt_sha_code TEXT,
  confidence REAL, status TEXT CHECK (status IN ('AUTO','REVIEW','UNMAPPED')), method TEXT, rationale TEXT);

-- ---------- human review (append-only; survives pipeline re-runs because keyed on natural keys) ----------
CREATE TABLE IF NOT EXISTS review_decision (
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT CHECK (scope IN ('record','account')),
  record_key TEXT, country_code TEXT, account_code TEXT,
  decision TEXT CHECK (decision IN ('ACCEPT','OVERRIDE','UNMAPPABLE')), sha_code TEXT, srhr_code TEXT,
  comment TEXT, reviewer TEXT, decided_at TEXT);

-- ---------- views ----------
DROP VIEW IF EXISTS v_effective;
CREATE VIEW v_effective AS
WITH rd AS (SELECT * FROM review_decision d WHERE scope='record'  AND decision_id=(SELECT MAX(decision_id) FROM review_decision x WHERE x.scope='record'  AND x.record_key=d.record_key)),
     ad AS (SELECT * FROM review_decision d WHERE scope='account' AND decision_id=(SELECT MAX(decision_id) FROM review_decision x WHERE x.scope='account' AND x.country_code=d.country_code AND x.account_code=d.account_code))
SELECT e.exp_id, e.record_key, e.country_code, e.source_txn_id, e.txn_date, e.ministry_code, e.account_code,
       a.account_label, e.description_original, e.description_used, e.text_trust, e.supplier,
       e.amount_original, e.currency_original, e.amount_lcu, e.amount_usd_ref, e.amount_usable, e.is_reversal,
       c.concept_id, c.sha_code AS auto_sha, c.srhr_code AS auto_srhr, c.alt_sha_code, c.confidence, c.status AS auto_status,
       c.method, c.rationale,
       CASE WHEN rd.decision_id IS NOT NULL THEN rd.decision WHEN ad.decision_id IS NOT NULL THEN ad.decision END AS review_decision,
       CASE WHEN rd.decision_id IS NOT NULL THEN 'record'    WHEN ad.decision_id IS NOT NULL THEN 'account'  END AS review_scope,
       CASE WHEN rd.decision_id IS NOT NULL THEN rd.comment  WHEN ad.decision_id IS NOT NULL THEN ad.comment END AS review_comment,
       CASE WHEN rd.decision_id IS NOT NULL THEN (CASE rd.decision WHEN 'OVERRIDE' THEN rd.sha_code WHEN 'ACCEPT' THEN c.sha_code END)
            WHEN ad.decision_id IS NOT NULL THEN (CASE ad.decision WHEN 'OVERRIDE' THEN ad.sha_code WHEN 'ACCEPT' THEN c.sha_code END)
            ELSE c.sha_code END AS final_sha,
       CASE WHEN rd.decision_id IS NOT NULL THEN (CASE rd.decision WHEN 'OVERRIDE' THEN rd.srhr_code WHEN 'ACCEPT' THEN c.srhr_code END)
            WHEN ad.decision_id IS NOT NULL THEN (CASE ad.decision WHEN 'OVERRIDE' THEN ad.srhr_code WHEN 'ACCEPT' THEN c.srhr_code END)
            ELSE c.srhr_code END AS final_srhr,
       CASE WHEN COALESCE(rd.decision, ad.decision) IN ('ACCEPT','OVERRIDE') THEN 'REVIEWED'
            WHEN COALESCE(rd.decision, ad.decision)='UNMAPPABLE' THEN 'UNMAPPABLE_CONFIRMED'
            ELSE c.status END AS final_status
FROM expenditure e
JOIN classification c ON c.exp_id=e.exp_id
LEFT JOIN coa_account a ON a.country_code=e.country_code AND a.account_code=e.account_code
LEFT JOIN rd ON rd.record_key=e.record_key
-- account-level decisions never bulk-clear exceptions: suspect-text and code/text-conflict records stay individually reviewable
LEFT JOIN ad ON ad.country_code=e.country_code AND ad.account_code=e.account_code
             AND e.text_trust<>'SUSPECT' AND c.method NOT LIKE 'code_text_conflict%';
