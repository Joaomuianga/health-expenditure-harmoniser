"""Country payload -> canonical record. Everything country-specific comes from the YAML config; nothing is hard-coded."""
from __future__ import annotations
from .parsers import parse_amount, parse_date, clean_text
from .security import inspect_text

CANONICAL = ["source_txn_id", "txn_date", "ministry_code", "ministry_name", "account_code", "description", "supplier", "amount"]


def _get(payload: dict, key: str | None):
    return payload.get(key) if key else None


def harmonise_record(cfg: dict, payload: dict, fx: dict[str, float], primary_ccy: str) -> tuple[dict, list[dict]]:
    """Returns (canonical_record, issues). Issues are dicts {rule, severity, message, action}."""
    f, issues = cfg["fields"], []

    def issue(rule, sev, msg, action):
        issues.append({"rule": rule, "severity": sev, "message": msg, "action": action})

    rec = {k: clean_text(_get(payload, f.get(k))) for k in CANONICAL if k != "amount"}
    if rec["ministry_code"]:
        rec["ministry_code"] = rec["ministry_code"].upper()

    # --- date ---
    iso = parse_date(rec["txn_date"], cfg["date_formats"])
    rec["txn_date"] = iso
    lo, hi = cfg["expected_period"]
    rec["in_expected_period"] = 1 if (iso and lo <= iso <= hi) else 0
    if rec["txn_date"] is None:
        issue("INVALID_DATE", "error", f"Date not parseable with {cfg['date_formats']}", "date left NULL")
    elif not rec["in_expected_period"]:
        issue("DATE_OUTSIDE_PERIOD", "warning", f"Posting date {iso} outside expected fiscal period {lo}..{hi}",
              "kept; flagged (cannot be assigned to the reporting year with confidence)")

    # --- text trust (prompt-injection defence) ---
    desc_orig = rec["description"]
    safe, suspicious, matched = inspect_text(desc_orig)
    rec["description_original"] = desc_orig
    rec["description_used"] = safe
    rec["text_trust"] = "SUSPECT" if suspicious else ("OK" if desc_orig else "MISSING")
    if suspicious:
        issue("SUSPECT_TEXT_INSTRUCTION", "critical",
              f"Description contains instruction-like text aimed at an automated classifier (matched: {matched!r})",
              "payload stripped from the text used for classification; original preserved; record routed to review")
    elif not desc_orig:
        issue("MISSING_DESCRIPTION", "info", "Description empty", "classified on account code only")
    for k in ("supplier", "ministry_name"):                               # scan other free-text fields too
        _, susp2, m2 = inspect_text(rec.get(k))
        if susp2:
            issue("SUSPECT_TEXT_INSTRUCTION", "critical", f"{k} contains instruction-like text ({m2!r})", "field not used")
    if not rec["supplier"]:
        issue("MISSING_SUPPLIER", "info", "Supplier empty", "kept")

    # --- amount & currency ---
    val, aflags = parse_amount(_get(payload, f["amount"]))
    cur = cfg["currency"]
    ccy = cur.get("fixed") or clean_text(_get(payload, cur.get("column")))
    ccy = ccy.upper() if ccy else primary_ccy
    rec.update(amount_original=val, currency_original=ccy, amount_usable=1, is_reversal=0, amount_lcu=None, amount_usd_ref=None)
    if "missing" in aflags or "unparseable" in aflags:
        rec["amount_usable"] = 0
        issue("MISSING_AMOUNT" if "missing" in aflags else "UNPARSEABLE_AMOUNT", "error",
              f"Amount {'missing' if 'missing' in aflags else 'not parseable'}", "record kept, excluded from all totals")
    else:
        if "stripped_text" in aflags or "decimal_comma" in aflags or "thousands_comma" in aflags:
            issue("AMOUNT_FORMAT_NORMALISED", "info", f"Amount stored as text/with separators: {_get(payload, f['amount'])!r}",
                  "parsed to number")
        if "assumed_thousands" in aflags:
            issue("AMOUNT_SEPARATOR_ASSUMED", "warning",
                  f"Single comma + 3 digits in {_get(payload, f['amount'])!r}: read as thousands separator", "assumed thousands; verify with country")
        if val < 0:
            rec["is_reversal"] = 1
            issue("NEGATIVE_AMOUNT", "info", f"Negative amount {val:,.2f} (credit / reversal / correction?)",
                  "kept as negative so totals are net; flagged")
        if ccy not in fx:
            issue("NO_FX_RATE", "error", f"No FX rate for {ccy}", "amount_lcu / amount_usd_ref left NULL")
        else:
            usd = val / fx[ccy]
            rec["amount_usd_ref"] = usd
            rec["amount_lcu"] = usd * fx[primary_ccy] if primary_ccy in fx else (val if ccy == primary_ccy else None)
            if ccy == primary_ccy:
                rec["amount_lcu"] = val
        if ccy != primary_ccy:
            issue("CURRENCY_CONVERTED", "warning", f"Record in {ccy}, country primary currency is {primary_ccy}",
                  "converted with reference FX (assumption - see config/fx_rates.csv)")
    return rec, issues
