"""Small, well-tested value parsers. All heterogeneity in number/date formats is absorbed here."""
from __future__ import annotations
import math, re
from datetime import datetime

_MISSING = {"", "nan", "null", "none", "n/a", "na", "-"}
_CCY_WORDS = re.compile(r"(?i)\b(fcfa|xof|kes|ksh|rwf|frw|usd)\b|[$€]|[\"']")


def parse_amount(raw) -> tuple[float | None, list[str]]:
    """Return (value, flags). Flags describe what had to be normalised so it can be logged.

    Handles: plain numbers, quoted values, thousands separators ("1,234.50", "7,782,082"),
    decimal comma ("11548910,00"), trailing currency words ("... FCFA"), (negative) and -negative.
    A single comma followed by exactly three digits is read as a thousands separator (flag 'assumed_thousands');
    this is the one genuinely ambiguous pattern and is surfaced as a data-quality note.
    """
    flags: list[str] = []
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        return None, ["missing"]
    if isinstance(raw, (int, float)):
        return float(raw), []
    s = str(raw).strip()
    if s.lower() in _MISSING:
        return None, ["missing"]
    s2 = _CCY_WORDS.sub("", s).strip()
    if s2 != s:
        flags.append("stripped_text")
    s2 = s2.replace("\u00a0", "").replace("\u202f", "").replace(" ", "")
    neg = s2.startswith("-") or (s2.startswith("(") and s2.endswith(")"))
    s2 = s2.strip("-()")
    if "," in s2 and "." in s2:
        if s2.rfind(",") > s2.rfind("."):           # 1.234,56
            s2 = s2.replace(".", "").replace(",", ".")
            flags.append("decimal_comma")
        else:                                        # 1,234.56
            s2 = s2.replace(",", "")
            flags.append("thousands_comma")
    elif "," in s2:
        parts = s2.split(",")
        if len(parts) > 2:                           # 7,782,082
            s2 = s2.replace(",", "")
            flags.append("thousands_comma")
        elif len(parts[1]) == 3:                     # 782,082  (ambiguous vs 782.082)
            s2 = s2.replace(",", "")
            flags += ["thousands_comma", "assumed_thousands"]
        elif len(parts[1]) in (1, 2):                # 11548910,00
            s2 = s2.replace(",", ".")
            flags.append("decimal_comma")
        else:
            return None, ["unparseable"]
    try:
        v = float(s2)
    except ValueError:
        return None, ["unparseable"]
    return (-v if neg else v), flags


def parse_date(raw, formats: list[str]) -> str | None:
    """Parse with explicit per-country formats (never guess dd/mm vs mm/dd). Returns ISO date or None."""
    if raw is None:
        return None
    s = str(raw).strip()
    for f in formats:
        try:
            return datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    return None


def clean_text(s) -> str | None:
    if s is None or (isinstance(s, float) and math.isnan(s)):
        return None
    s = re.sub(r"\s+", " ", str(s).replace("\u00a0", " ")).strip()
    return s or None
