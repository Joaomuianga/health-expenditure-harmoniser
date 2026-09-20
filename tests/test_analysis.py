import sqlite3
import pytest
from hxh import analysis as an
from hxh.config import DEFAULT_RAW_DIR
from hxh.pipeline import run


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    if not (DEFAULT_RAW_DIR / "country_a_expenditure.csv").exists():
        pytest.skip("assessment data not present in data/raw")
    p = tmp_path_factory.mktemp("db") / "a.sqlite"
    run(DEFAULT_RAW_DIR, p, verbose=False)
    return sqlite3.connect(p)


def test_coverage_sums_to_one(con):
    c = an.coverage(con)
    assert c.groupby("country_code").pct_records.sum().round(6).eq(1).all()
    assert c.groupby("country_code").pct_value.sum().round(6).eq(1).all()


def test_uncertainty_band_is_ordered(con):
    b = an.uncertainty_band(con, "sha")
    assert (b.high_if_review_lands_here + 1e-6 >= b.low_auto_only).all()
    assert b.gap_pct_of_high.between(0, 1).all()


def test_counterparty_fields_carry_no_signal(con):
    s = an.signal_strength(con)
    assert ((s.cramers_v - s.independence_baseline).abs() < 0.03).all()   # ministry / supplier ~ independent of concept


def test_scale_flag_country_b_is_far_larger(con):
    s = an.scale_check(con).set_index("country_code")
    assert s.loc["CTB", "median_vs_lowest"] > 10


def test_reversals_are_unmatched_and_only_in_country_a(con):
    r = an.reversal_analysis(con).set_index("country_code")
    assert r.loc["CTA", "negative_records"] == 54 and r.loc["CTA", "matched_to_original"] == 0
    assert r.loc[["CTB", "CTC"], "negative_records"].sum() == 0
