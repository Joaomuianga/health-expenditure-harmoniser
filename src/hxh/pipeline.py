"""End-to-end batch pipeline:  seed reference -> ingest (raw) -> harmonise -> data-quality -> classify.

    python -m hxh.pipeline [--raw-dir data/raw] [--db db/health_expenditure.sqlite]

Idempotent: every run rebuilds all derived tables from the source files; human review decisions are preserved.
"""
from __future__ import annotations
import argparse, csv, json, sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import adapters, classify as clf
from .config import CONFIG_DIR, DEFAULT_DB, DEFAULT_RAW_DIR, config_hash, load_country_configs
from .db import connect, reset_and_init
from .harmonise import harmonise_record
from .parsers import parse_amount

now = lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")


def _csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def seed_reference(con) -> None:
    for r in _csv(CONFIG_DIR / "ref_countries.csv"):
        con.execute("INSERT INTO country VALUES (?,?,?,?)", (r["country_code"], r["country_name"], r["primary_currency"], r["language"]))
    for r in _csv(CONFIG_DIR / "ref_sha_classification.csv"):
        con.execute("INSERT INTO ref_sha VALUES (?,?,?)", (r["sha_code"], r["sha_description"], r["notes"]))
    for r in _csv(CONFIG_DIR / "ref_srhr_classification.csv"):
        con.execute("INSERT INTO ref_srhr VALUES (?,?,?)", (r["srhr_code"], r["srhr_description"], r["notes"]))
    for r in _csv(CONFIG_DIR / "fx_rates.csv"):
        con.execute("INSERT INTO fx_rate VALUES (?,?,?)", (r["currency"], float(r["per_usd"]), r["note"]))
    for r in _csv(CONFIG_DIR / "classification" / "concepts.csv"):
        con.execute("INSERT INTO concept VALUES (?,?,?,?,?,?,?,?)", (
            r["concept_id"], r["description"], r["target"], r["sha_code"] or None, r["srhr_code"] or None,
            float(r["base_confidence"]) if r["base_confidence"] else None, r["alt_sha_code"] or None, r["rationale"]))
    for r in _csv(CONFIG_DIR / "classification" / "coa_map.csv"):
        con.execute("INSERT INTO coa_account VALUES (?,?,?,?)", (r["country_code"], r["account_code"], r["account_label"], r["concept_id"]))
    for r in _csv(CONFIG_DIR / "classification" / "keyword_rules.csv"):
        con.execute("INSERT INTO keyword_rule VALUES (?,?,?,?,?)", (r["rule_id"], int(r["priority"]), r["pattern"], r["concept_id"], r["lang"]))


def log_issue(con, run_id, file_id, raw_id, exp_id, i: dict) -> None:
    con.execute("INSERT INTO dq_issue (run_id,file_id,raw_id,exp_id,rule,severity,message,action_taken) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, file_id, raw_id, exp_id, i["rule"], i["severity"], i["message"], i.get("action")))


def ingest_country(con, run_id: int, cfg: dict, raw_dir: Path, fx: dict, primary: dict, known_accounts: set) -> None:
    cc = cfg["country_code"]
    sd = adapters.read_source(cfg, raw_dir)
    cur = con.execute("INSERT INTO source_file (run_id,country_code,file_name,sha256,format,n_raw_rows,metadata_json,loaded_at) VALUES (?,?,?,?,?,?,?,?)",
                      (run_id, cc, sd.path.name, sd.sha256, sd.fmt, len(sd.records), json.dumps(sd.metadata, default=str), now()))
    file_id = cur.lastrowid
    splits_field = cfg["source"].get("splits_field")
    n_ok_amount = 0
    for locator, payload in sd.records:
        key = f"{cc}:{locator}"
        raw_id = con.execute("INSERT INTO raw_record (file_id,source_locator,record_key,record_type,payload_json) VALUES (?,?,?,?,?)",
                             (file_id, locator, key, "DATA", json.dumps(payload, ensure_ascii=False, default=str))).lastrowid
        rec, issues = harmonise_record(cfg, payload, fx, primary[cc])
        if rec["account_code"] not in known_accounts:
            issues.append({"rule": "UNKNOWN_ACCOUNT_CODE", "severity": "error",
                           "message": f"Account {rec['account_code']!r} is not in the {cc} chart of accounts mapping", "action": "routed to review"})
        splits = payload.get(splits_field) if splits_field else None
        exp_id = con.execute(
            """INSERT INTO expenditure (raw_id,record_key,run_id,country_code,source_txn_id,txn_date,in_expected_period,ministry_code,ministry_name,
               account_code,description_original,description_used,text_trust,supplier,amount_original,currency_original,amount_lcu,amount_usd_ref,
               is_reversal,amount_usable,n_splits) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (raw_id, key, run_id, cc, rec["source_txn_id"], rec["txn_date"], rec["in_expected_period"], rec["ministry_code"], rec["ministry_name"],
             rec["account_code"], rec["description_original"], rec["description_used"], rec["text_trust"], rec["supplier"], rec["amount_original"],
             rec["currency_original"], rec["amount_lcu"], rec["amount_usd_ref"], rec["is_reversal"], rec["amount_usable"], len(splits or []))).lastrowid
        if splits:
            tot = 0.0
            for s in splits:
                con.execute("INSERT INTO expenditure_split (exp_id,source_split_id,description,amount) VALUES (?,?,?,?)",
                            (exp_id, s.get("subId"), s.get("description"), s.get("amount")))
                tot += float(s.get("amount") or 0)
            if rec["amount_original"] is not None and abs(tot - rec["amount_original"]) > 0.05:
                issues.append({"rule": "SPLIT_TOTAL_MISMATCH", "severity": "error",
                               "message": f"Sub-transactions sum {tot:,.2f} != parent {rec['amount_original']:,.2f}", "action": "parent amount used"})
            issues.append({"rule": "SUB_TRANSACTIONS_PRESENT", "severity": "info",
                           "message": f"{len(splits)} sub-transactions", "action": "parent amount used; splits stored for traceability, never summed (double-count risk)"})
        for i in issues:
            log_issue(con, run_id, file_id, raw_id, exp_id, i)

    # ---- control totals / record counts ----
    used = con.execute("SELECT COALESCE(SUM(amount_original),0) FROM expenditure WHERE raw_id IN (SELECT raw_id FROM raw_record WHERE file_id=?) AND amount_usable=1", (file_id,)).fetchone()[0]
    ct = cfg["source"].get("control_total")
    if sd.control and ct:
        loc, row = sd.control
        v, _ = parse_amount(row.get(ct["amount_column"]))
        con.execute("INSERT INTO raw_record (file_id,source_locator,record_key,record_type,payload_json) VALUES (?,?,?,?,?)",
                    (file_id, loc, f"{cc}:{loc}", "CONTROL_TOTAL", json.dumps(row, ensure_ascii=False)))
        con.execute("UPDATE source_file SET control_total_raw=?, control_total_value=? WHERE file_id=?", (row.get(ct["amount_column"]), v, file_id))
        ok = v is not None and abs(v - used) < 1.0
        log_issue(con, run_id, file_id, None, None, {"rule": "CONTROL_TOTAL_OK" if ok else "CONTROL_TOTAL_MISMATCH", "severity": "info" if ok else "error",
                  "message": f"Footer TOTAL row ({v:,.0f}) vs sum of parsed records ({used:,.0f}); TOTAL row excluded from records",
                  "action": "reconciled" if ok else "investigate"})
    rc = cfg["source"].get("record_count_field")
    if rc and rc in sd.metadata:
        ok = int(sd.metadata[rc]) == len(sd.records)
        con.execute("UPDATE source_file SET control_total_raw=?, control_total_value=? WHERE file_id=?", (f"recordCount={sd.metadata[rc]}", float(sd.metadata[rc]), file_id))
        log_issue(con, run_id, file_id, None, None, {"rule": "RECORD_COUNT_OK" if ok else "RECORD_COUNT_MISMATCH", "severity": "info" if ok else "error",
                  "message": f"Metadata recordCount={sd.metadata[rc]} vs {len(sd.records)} parsed", "action": "reconciled" if ok else "investigate"})
    # CoA shipped with the source vs our mapping
    for code, label in sd.coa_labels.items():
        if code not in known_accounts and code != "code_budgetaire":
            log_issue(con, run_id, file_id, None, None, {"rule": "COA_CODE_UNMAPPED", "severity": "warning", "message": f"Source CoA lists {code} ({label}) with no mapping", "action": "add to coa_map.csv"})


def cross_record_quality(con, run_id: int, cfgs: list[dict]) -> None:
    # duplicate source ids
    for r in con.execute("""SELECT country_code, source_txn_id, COUNT(*) n FROM expenditure GROUP BY 1,2 HAVING n>1""").fetchall():
        for e in con.execute("SELECT exp_id, raw_id FROM expenditure WHERE country_code=? AND source_txn_id=?", (r["country_code"], r["source_txn_id"])).fetchall():
            log_issue(con, run_id, None, e["raw_id"], e["exp_id"], {"rule": "DUPLICATE_SOURCE_ID", "severity": "warning",
                      "message": f"Transaction id {r['source_txn_id']} occurs {r['n']} times with different content", "action": "both kept (different content = ID collision, not duplicate); flagged"})
    # identical content, different ids
    for r in con.execute("""SELECT country_code, txn_date, account_code, amount_original, supplier, ministry_code, GROUP_CONCAT(exp_id) ids, COUNT(*) n
                            FROM expenditure WHERE amount_usable=1 GROUP BY 1,2,3,4,5,6 HAVING n>1""").fetchall():
        for eid in r["ids"].split(","):
            log_issue(con, run_id, None, None, int(eid), {"rule": "POSSIBLE_DUPLICATE", "severity": "warning",
                      "message": "Same date/account/amount/supplier/ministry as another record", "action": "kept; flagged"})
    # aggregated observations (file level)
    for cfg in cfgs:
        cc = cfg["country_code"]; fid = con.execute("SELECT file_id FROM source_file WHERE country_code=?", (cc,)).fetchone()[0]
        n_var = con.execute("""SELECT COUNT(*) FROM expenditure e WHERE country_code=? AND description_original IS NOT NULL AND text_trust='OK' AND description_original <>
                 (SELECT description_original FROM expenditure x WHERE x.country_code=e.country_code AND x.account_code=e.account_code AND x.text_trust='OK'
                  GROUP BY description_original ORDER BY COUNT(*) DESC LIMIT 1)""", (cc,)).fetchone()[0]
        if n_var:
            log_issue(con, run_id, fid, None, None, {"rule": "DESCRIPTION_VARIANTS", "severity": "info", "message": f"{n_var} descriptions differ from the account's usual wording (case variants)", "action": "matching is case/accent-insensitive"})
        hm = tuple(cfg.get("health_ministry_codes", []))
        tot, off = con.execute("""SELECT COUNT(*), SUM(CASE WHEN e.ministry_code NOT IN (%s) THEN 1 ELSE 0 END) FROM expenditure e
                 JOIN coa_account a ON a.country_code=e.country_code AND a.account_code=e.account_code
                 JOIN concept c ON c.concept_id=a.concept_id
                 WHERE e.country_code=? AND c.target='MAPPABLE'""" % ",".join("?" * len(hm)), (*hm, cc)).fetchone()
        if tot:
            log_issue(con, run_id, fid, None, None, {"rule": "MINISTRY_NOT_INFORMATIVE", "severity": "warning",
                      "message": f"{off} of {tot} records on health-specific accounts ({off / tot:.0%}) are booked to non-health ministries (e.g. Education buying vaccines)",
                      "action": "ministry is NOT used as a classification signal; 'health expenditure' cannot be identified by ministry"})


def classify_all(con, run_id: int) -> None:
    concepts = {r["concept_id"]: dict(r) for r in con.execute("SELECT * FROM concept")}
    coa = {(r["country_code"], r["account_code"]): r["concept_id"] for r in con.execute("SELECT * FROM coa_account")}
    rules = [dict(r) for r in con.execute("SELECT * FROM keyword_rule")]
    for e in con.execute("SELECT exp_id, country_code, account_code, description_used, text_trust FROM expenditure").fetchall():
        r = clf.classify(e["country_code"], e["account_code"], e["description_used"], e["text_trust"], coa, concepts, rules)
        con.execute("INSERT INTO classification VALUES (?,?,?,?,?,?,?,?,?,?,?)", (e["exp_id"], run_id, r.concept_id, r.text_concept_id, r.sha_code, r.srhr_code,
                    r.alt_sha_code, r.confidence, r.status, r.method, r.rationale))
        if r.method.startswith("code_text_conflict"):
            log_issue(con, run_id, None, None, e["exp_id"], {"rule": "DESCRIPTION_CODE_CONFLICT", "severity": "warning",
                      "message": f"Account maps to {r.concept_id}, description suggests {r.text_concept_id}", "action": "routed to review"})


def run(raw_dir: Path = DEFAULT_RAW_DIR, db_path: Path = DEFAULT_DB, verbose: bool = True) -> None:
    con = connect(db_path)
    reset_and_init(con)
    seed_reference(con)
    run_id = con.execute("INSERT INTO pipeline_run (started_at,config_hash) VALUES (?,?)", (now(), config_hash())).lastrowid
    fx = {r["currency"]: r["per_usd"] for r in con.execute("SELECT * FROM fx_rate")}
    primary = {r["country_code"]: r["primary_currency"] for r in con.execute("SELECT * FROM country")}
    known = {(r["country_code"], r["account_code"]) for r in con.execute("SELECT * FROM coa_account")}
    cfgs = load_country_configs()
    for cfg in cfgs:
        ingest_country(con, run_id, cfg, raw_dir, fx, primary, {a for (c, a) in known if c == cfg["country_code"]})
    cross_record_quality(con, run_id, cfgs)
    classify_all(con, run_id)
    con.execute("UPDATE pipeline_run SET finished_at=? WHERE run_id=?", (now(), run_id))
    con.commit()
    if verbose:
        print(f"Run {run_id} complete -> {db_path}")
        for r in con.execute("""SELECT e.country_code cc, c.status, COUNT(*) n FROM expenditure e JOIN classification c USING(exp_id) GROUP BY 1,2 ORDER BY 1,2"""):
            print(f"  {r['cc']}  {r['status']:9s} {r['n']:5d}")
        for r in con.execute("SELECT rule, severity, COUNT(*) n FROM dq_issue GROUP BY 1,2 ORDER BY 3 DESC"):
            print(f"  DQ {r['severity']:8s} {r['rule']:28s} {r['n']}")
    con.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    a = ap.parse_args(argv)
    try:
        run(a.raw_dir, a.db)
    except FileNotFoundError as e:
        print(e, file=sys.stderr); return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
