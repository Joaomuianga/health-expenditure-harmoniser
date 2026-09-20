"""Analytical checks on the harmonised + classified data.

Each function returns a DataFrame (or dict) and is used by both the Streamlit 'Analysis' page and the CLI
(`python -m hxh.analysis`). They answer questions an analyst or panel would ask *after* harmonisation:
how much of the money is classified, how wide is the uncertainty, are the countries even comparable,
and which fields carry real signal?
"""
from __future__ import annotations
import sqlite3
import numpy as np
import pandas as pd
from .config import DEFAULT_DB


def _load(con: sqlite3.Connection) -> pd.DataFrame:
    return pd.read_sql_query("SELECT * FROM v_effective", con)


def coverage(con) -> pd.DataFrame:
    """Share of records and of value (USD ref, usable amounts, net) by machine status, per country."""
    d = _load(con)
    n = d.groupby(["country_code", "auto_status"]).size().rename("records")
    v = d[d.amount_usable == 1].groupby(["country_code", "auto_status"]).amount_usd_ref.sum().rename("usd_ref")
    out = pd.concat([n, v], axis=1).reset_index()
    out["pct_records"] = out.records / out.groupby("country_code").records.transform("sum")
    out["pct_value"] = out.usd_ref / out.groupby("country_code").usd_ref.transform("sum")
    return out


def uncertainty_band(con, dimension: str = "sha") -> pd.DataFrame:
    """Range of spending per SHA/SRHR code that the classification leaves open.

    low  = only AUTO records (what we are confident about)
    high = AUTO + every REVIEW record whose proposed OR alternative code is this code (SHA only)
    A wide gap = the conclusion for that code depends on analyst decisions.
    """
    d = _load(con)
    d = d[d.amount_usable == 1]
    col = "auto_sha" if dimension == "sha" else "auto_srhr"
    low = d[(d.auto_status == "AUTO") & d[col].notna()].groupby(["country_code", col]).amount_usd_ref.sum()
    rev = d[(d.auto_status == "REVIEW") & d[col].notna()]
    hi_parts = [rev.groupby(["country_code", col]).amount_usd_ref.sum()]
    if dimension == "sha":
        alt = rev[rev.alt_sha_code.notna()].groupby(["country_code", "alt_sha_code"]).amount_usd_ref.sum()
        alt.index.names = ["country_code", col]
        hi_parts.append(alt)
    extra = pd.concat(hi_parts).groupby(level=[0, 1]).sum()
    out = pd.concat([low.rename("low_auto_only"), (low.add(extra, fill_value=0)).rename("high_if_review_lands_here")], axis=1).fillna(0)
    out.index.names = ["country_code", "code"]
    out = out.reset_index()
    out["gap_pct_of_high"] = np.where(out.high_if_review_lands_here > 0, 1 - out.low_auto_only / out.high_if_review_lands_here, 0.0)
    return out


def scale_check(con) -> pd.DataFrame:
    """Are transaction sizes comparable across countries once converted with the reference FX?"""
    d = _load(con)
    d = d[(d.amount_usable == 1) & (d.amount_usd_ref > 0)]
    g = d.groupby("country_code").amount_usd_ref
    out = pd.DataFrame({"n": g.size(), "median_usd": g.median(), "mean_usd": g.mean(), "p90_usd": g.quantile(0.9)})
    out["median_vs_lowest"] = out.median_usd / out.median_usd.min()
    return out.reset_index()


def usd_row_consistency(con) -> dict:
    """Country C carries both RWF and USD records. If the two are on the same footing, the USD rows converted to
    RWF should look like the RWF rows (same account, similar size). Returns the implied RWF/USD rate that would make
    the medians agree, compared with the reference rate used."""
    d = _load(con)
    d = d[(d.country_code == "CTC") & (d.amount_usable == 1) & (d.amount_original > 0)]
    rate = pd.read_sql_query("SELECT per_usd FROM fx_rate WHERE currency='RWF'", con).per_usd[0]
    per_acct = d.groupby(["account_code", "currency_original"]).amount_original.median().unstack()
    per_acct = per_acct.dropna()
    ratio = (per_acct["RWF"] / per_acct["USD"])                   # RWF-per-USD that equalises medians, account by account
    return {"reference_rate": float(rate), "implied_rate_median": float(ratio.median()),
            "implied_rate_iqr": (float(ratio.quantile(.25)), float(ratio.quantile(.75))),
            "n_usd_rows": int((d.currency_original == "USD").sum()), "n_accounts": int(len(per_acct))}


def signal_strength(con) -> pd.DataFrame:
    """Association (Cramer's V) between a field and the classified concept. V near its independence baseline = no signal.
    Used to justify NOT classifying on ministry or supplier."""
    d = _load(con)
    e = pd.read_sql_query("SELECT exp_id, ministry_code, supplier FROM expenditure", con)
    d = d.merge(e, on="exp_id", suffixes=("", "_e"))
    d = d[d.concept_id.notna()]
    rows = []
    for cc, x in d.groupby("country_code"):
        for field in ("ministry_code", "supplier"):
            xt = pd.crosstab(x[field].fillna("(none)"), x.concept_id)
            o = xt.values.astype(float); n = o.sum()
            ex = o.sum(1, keepdims=True) * o.sum(0, keepdims=True) / n
            chi2 = ((o - ex) ** 2 / ex).sum(); k = min(o.shape) - 1
            v = float(np.sqrt(chi2 / (n * k)))
            baseline = float(np.sqrt((o.shape[0] - 1) * (o.shape[1] - 1) / (n * k)))   # expected V if truly independent
            rows.append({"country_code": cc, "field": field, "cramers_v": v, "independence_baseline": baseline, "n": int(n)})
    return pd.DataFrame(rows)


def health_ministry_share(con) -> pd.DataFrame:
    """Of records on health-specific accounts, what share is booked to the health ministry?"""
    import yaml
    from .config import load_country_configs
    hm = {c["country_code"]: set(c.get("health_ministry_codes", [])) for c in load_country_configs()}
    d = _load(con)
    e = pd.read_sql_query("SELECT exp_id, ministry_code FROM expenditure", con)
    d = d.merge(e, on="exp_id", suffixes=("", "_e"))
    m = pd.read_sql_query("SELECT a.country_code, a.account_code FROM coa_account a JOIN concept c USING(concept_id) WHERE c.target='MAPPABLE'", con)
    d = d.merge(m, on=["country_code", "account_code"])
    d["in_health_ministry"] = [r.ministry_code in hm[r.country_code] for r in d.itertuples()]
    return d.groupby("country_code").in_health_ministry.agg(share="mean", records="size").reset_index()


def reversal_analysis(con) -> pd.DataFrame:
    d = _load(con)
    d = d[d.amount_usable == 1]
    out = []
    for cc, x in d.groupby("country_code"):
        neg = x[x.is_reversal == 1]; pos = x[x.is_reversal == 0]
        keys = set(zip(pos.account_code, pos.amount_original.round(2)))
        matched = sum((r.account_code, round(-r.amount_original, 2)) in keys for r in neg.itertuples())
        gross = pos.amount_lcu.sum()
        out.append({"country_code": cc, "negative_records": len(neg), "matched_to_original": matched,
                    "net_effect_pct": (neg.amount_lcu.sum() / gross) if gross else 0.0})
    return pd.DataFrame(out)


def monthly_profile(con) -> pd.DataFrame:
    d = _load(con)
    d = d[(d.amount_usable == 1) & d.txn_date.notna()]
    d = d.assign(month=d.txn_date.str[:7])
    return d.groupby(["month", "country_code"]).amount_usd_ref.sum().unstack().reset_index()


def outlier_scan(con, threshold: float = 3.5) -> pd.DataFrame:
    """Robust (median/MAD) outliers on log10 local-currency amount within country+account."""
    d = _load(con)
    d = d[(d.amount_usable == 1) & d.amount_lcu.notna()].copy()
    d["log_amt"] = np.log10(d.amount_lcu.abs().clip(lower=1))
    g = d.groupby(["country_code", "account_code"]).log_amt
    med = g.transform("median"); mad = g.transform(lambda s: (s - s.median()).abs().median())
    d["robust_z"] = (d.log_amt - med) / (1.4826 * mad + 1e-9)
    return d.loc[d.robust_z.abs() > threshold, ["record_key", "account_code", "amount_lcu", "robust_z"]]


def summary(con) -> dict:
    return {"coverage": coverage(con), "band_sha": uncertainty_band(con, "sha"), "scale": scale_check(con),
            "usd_consistency": usd_row_consistency(con), "signal": signal_strength(con),
            "ministry": health_ministry_share(con), "reversals": reversal_analysis(con), "outliers": outlier_scan(con)}


if __name__ == "__main__":
    pd.set_option("display.width", 200); pd.set_option("display.float_format", lambda v: f"{v:,.3f}")
    con = sqlite3.connect(str(DEFAULT_DB))
    for k, v in summary(con).items():
        print(f"\n=== {k}\n{v}")
