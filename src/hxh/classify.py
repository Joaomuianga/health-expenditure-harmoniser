"""Layered, explainable classification.

Layer 1 (authoritative)  country account code -> canonical concept (coa_account table)  -> SHA + SRHR (concept table)
Layer 2 (corroboration)  keyword rules on the SANITISED description -> text concept; agree / disagree / absent
Layer 3 (routing)        confidence + flags -> AUTO | REVIEW | UNMAPPED   (humans resolve REVIEW; UNMAPPED = no supported target)

Confidence values are *ordinal priors set by the mapping author*, not calibrated probabilities (see README, 'Validation').
No free text is ever executed, and no LLM is called: classification is deterministic and reproducible.
"""
from __future__ import annotations
import re, unicodedata
from dataclasses import dataclass

AUTO_THRESHOLD = 0.75
CONFLICT_CONF = 0.35
TEXT_ONLY_CONF = 0.40


def norm_text(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.lower()).strip()


@dataclass
class Result:
    concept_id: str | None
    text_concept_id: str | None
    sha_code: str | None
    srhr_code: str | None
    alt_sha_code: str | None
    confidence: float
    status: str
    method: str
    rationale: str


def text_concept(desc: str | None, rules: list[dict]) -> tuple[str | None, str]:
    """Return (concept, note). Highest-priority (lowest number) rule wins; a tie between different concepts = no signal."""
    d = norm_text(desc)
    if not d:
        return None, "no text"
    hits = sorted((r for r in rules if re.search(r["pattern"], d)), key=lambda r: r["priority"])
    if not hits:
        return None, "no keyword rule matched"
    top = [h for h in hits if h["priority"] == hits[0]["priority"]]
    if len({h["concept_id"] for h in top}) > 1:
        return None, "ambiguous keywords: " + ", ".join(sorted({h['concept_id'] for h in top}))
    return hits[0]["concept_id"], f"rule {hits[0]['rule_id']}"


def classify(country: str, account_code: str, description_used: str | None, text_trust: str,
             coa: dict, concepts: dict, rules: list[dict]) -> Result:
    code_concept = coa.get((country, account_code))
    t_concept, t_note = text_concept(description_used, rules)
    c = concepts.get(code_concept) if code_concept else None

    if c is None:                                      # account not in the mapping table
        if t_concept and concepts.get(t_concept, {}).get("target") == "MAPPABLE":
            tc = concepts[t_concept]
            return Result(None, t_concept, tc["sha_code"], tc["srhr_code"], tc["alt_sha_code"], TEXT_ONLY_CONF, "REVIEW",
                          "text_only", f"Account {account_code} is not in the CoA mapping; keyword text suggests {t_concept} ({t_note}).")
        return Result(None, t_concept, None, None, None, 0.0, "REVIEW", "none",
                      f"Account {account_code} not in the CoA mapping and text gives no usable signal ({t_note}).")

    base = c["base_confidence"]
    if c["target"] == "NO_TARGET":
        note = f"{code_concept}: {c['rationale']}"
        if t_concept and t_concept != code_concept:
            return Result(code_concept, t_concept, None, None, None, 0.0, "REVIEW", "code_text_conflict",
                          f"Code says {code_concept} (no target) but text suggests {t_concept}. {note}")
        if text_trust == "SUSPECT":
            return Result(code_concept, t_concept, None, None, None, 0.0, "REVIEW", "code+suspect_text",
                          "No supported target, but the description contained instruction-like text. " + note)
        return Result(code_concept, t_concept, None, None, None, 0.0, "UNMAPPED", "code",
                      "No supported SHA/SRHR target. " + note)

    # MAPPABLE concept
    if t_concept and t_concept != code_concept:
        return Result(code_concept, t_concept, c["sha_code"], c["srhr_code"], c["alt_sha_code"], CONFLICT_CONF, "REVIEW",
                      "code_text_conflict",
                      f"Code maps to {code_concept} but description suggests {t_concept}; treated as unreliable until reviewed.")
    if t_concept == code_concept:
        conf, method, why = min(1.0, base + 0.05), "code+text", "account code and description agree"
    else:
        conf, method, why = max(0.0, base - 0.05), "code", "account code only (description empty or uninformative)"
    status = "AUTO" if conf >= AUTO_THRESHOLD else "REVIEW"
    rationale = f"{code_concept}: {c['rationale']} [{why}]"
    if status == "REVIEW" and c["alt_sha_code"]:
        rationale += f" Alternative SHA: {c['alt_sha_code']}."
    if text_trust == "SUSPECT":
        status, method = "REVIEW", method + "+suspect_text"
        rationale += " Description contained instruction-like text; only the leading label was used and the record is routed to a human."
    return Result(code_concept, t_concept, c["sha_code"], c["srhr_code"], c["alt_sha_code"], conf, status, method, rationale)
