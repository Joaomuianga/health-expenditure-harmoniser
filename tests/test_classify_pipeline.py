import csv, sqlite3
from pathlib import Path
import pytest
from hxh import classify as clf
from hxh.config import CONFIG_DIR, DEFAULT_RAW_DIR
from hxh.pipeline import run


def _load():
    rd = lambda n: list(csv.DictReader(open(CONFIG_DIR / "classification" / n, encoding="utf-8")))
    concepts = {r["concept_id"]: {**r, "base_confidence": float(r["base_confidence"]) if r["base_confidence"] else None,
                                  "sha_code": r["sha_code"] or None, "srhr_code": r["srhr_code"] or None, "alt_sha_code": r["alt_sha_code"] or None} for r in rd("concepts.csv")}
    coa = {(r["country_code"], r["account_code"]): r["concept_id"] for r in rd("coa_map.csv")}
    rules = [{**r, "priority": int(r["priority"])} for r in rd("keyword_rules.csv")]
    return coa, concepts, rules


COA, CONCEPTS, RULES = _load()


def test_every_mapped_concept_points_at_supplied_reference_codes():
    sha = {r["sha_code"] for r in csv.DictReader(open(CONFIG_DIR / "ref_sha_classification.csv"))}
    srhr = {r["srhr_code"] for r in csv.DictReader(open(CONFIG_DIR / "ref_srhr_classification.csv"))}
    for c in CONCEPTS.values():
        if c["target"] == "MAPPABLE":
            assert c["sha_code"] in sha and c["srhr_code"] in srhr and (c["alt_sha_code"] in sha | {None})
    assert {c for c in COA.values()} <= set(CONCEPTS)


def test_clear_case_is_auto():
    r = clf.classify("CTA", "2211102", "CONTRACEPTIVES AND FAMILY PLANNING COMMODITIES", "OK", COA, CONCEPTS, RULES)
    assert (r.status, r.sha_code, r.srhr_code) == ("AUTO", "HC.5.1", "SRHR.FP")


def test_missing_text_falls_back_to_code_with_lower_confidence():
    a = clf.classify("CTC", "2211001", "Essential medicines", "OK", COA, CONCEPTS, RULES)
    b = clf.classify("CTC", "2211001", None, "MISSING", COA, CONCEPTS, RULES)
    assert b.method == "code" and b.confidence < a.confidence and b.sha_code == a.sha_code


def test_ambiguous_sha_boundary_goes_to_review_with_alternative():
    r = clf.classify("CTA", "2211306", "Antenatal outreach services", "OK", COA, CONCEPTS, RULES)
    assert r.status == "REVIEW" and r.alt_sha_code == "HC.1.3"


def test_capital_and_overhead_are_unmapped_not_forced():
    for country, code in [("CTA", "3110101"), ("CTB", "610100"), ("CTC", "2210105")]:
        r = clf.classify(country, code, None, "MISSING", COA, CONCEPTS, RULES)
        assert r.status == "UNMAPPED" and r.sha_code is None


def test_code_text_conflict_is_never_auto():
    r = clf.classify("CTB", "611040", "Fournitures medicales de base", "SUSPECT", COA, CONCEPTS, RULES)
    assert r.status == "REVIEW" and r.method == "code_text_conflict"


def test_unknown_account_never_auto():
    r = clf.classify("CTA", "9999999", "Vaccines", "OK", COA, CONCEPTS, RULES)
    assert r.status == "REVIEW" and r.method == "text_only"


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    if not (DEFAULT_RAW_DIR / "country_a_expenditure.csv").exists():
        pytest.skip("assessment data not present in data/raw")
    p = tmp_path_factory.mktemp("db") / "t.sqlite"
    run(DEFAULT_RAW_DIR, p, verbose=False)
    c = sqlite3.connect(p); c.row_factory = sqlite3.Row
    return c


def test_counts_and_reconciliation(db):
    n = dict(db.execute("SELECT country_code, COUNT(*) FROM expenditure GROUP BY 1").fetchall())
    assert n == {"CTA": 2500, "CTB": 2000, "CTC": 2500}                     # B: footer TOTAL row excluded
    assert db.execute("SELECT COUNT(*) FROM dq_issue WHERE rule='CONTROL_TOTAL_OK'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 7001  # 7000 data rows + 1 control row, nothing dropped


def test_injected_rows_are_flagged_and_never_auto(db):
    rows = db.execute("SELECT auto_status, method FROM v_effective WHERE text_trust='SUSPECT'").fetchall()
    assert len(rows) == 4 and all(r["auto_status"] == "REVIEW" for r in rows)
    assert db.execute("SELECT COUNT(*) FROM v_effective WHERE auto_status='AUTO' AND text_trust='SUSPECT'").fetchone()[0] == 0


def test_no_double_counting_of_subtransactions(db):
    parent = db.execute("SELECT SUM(amount_original) FROM expenditure WHERE country_code='CTC' AND n_splits>0").fetchone()[0]
    splits = db.execute("SELECT SUM(amount) FROM expenditure_split").fetchone()[0]
    assert parent == pytest.approx(splits, rel=1e-9)                        # splits reconcile to parents, only parents are in totals


def test_review_decisions_apply_and_exceptions_stay_open(db):
    db.execute("INSERT INTO review_decision (scope,country_code,account_code,decision,sha_code,srhr_code,comment,reviewer,decided_at) "
               "VALUES ('account','CTB','611040','OVERRIDE','HC.4','SRHR.NA','test','t','now')")
    st = dict(db.execute("SELECT text_trust, final_status FROM v_effective WHERE country_code='CTB' AND account_code='611040' GROUP BY 1").fetchall())
    assert st["OK"] == "REVIEWED" and st["SUSPECT"] == "REVIEW"             # bulk decision never clears flagged exceptions
