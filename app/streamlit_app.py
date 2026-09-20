"""Analyst workbench (Streamlit).  Run:  streamlit run app/streamlit_app.py   (after `python -m hxh.pipeline`)"""
from __future__ import annotations
import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from hxh.config import DEFAULT_DB  # noqa: E402

st.set_page_config(page_title="Health expenditure harmoniser", layout="wide")


@st.cache_resource
def conn() -> sqlite3.Connection:
    if not Path(DEFAULT_DB).exists():
        st.error(f"Database not found at {DEFAULT_DB}. Run `python -m hxh.pipeline` first."); st.stop()
    c = sqlite3.connect(str(DEFAULT_DB), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def q(sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, conn(), params=params)


STATUS_LABEL = {"AUTO": "Auto-accepted", "REVIEW": "Needs review", "UNMAPPED": "No supported target",
                "REVIEWED": "Analyst-reviewed", "UNMAPPABLE_CONFIRMED": "Confirmed unmappable"}

page = st.sidebar.radio("Workbench", ["Overview", "Review queue", "Data quality", "Explore results", "Trace a record", "Mappings & audit"])
st.sidebar.caption("Prototype - synthetic data. Confidence values are ordinal priors, not calibrated probabilities.")
run = q("SELECT * FROM pipeline_run ORDER BY run_id DESC LIMIT 1")
if len(run):
    st.sidebar.caption(f"Pipeline run {int(run.run_id[0])} - config {run.config_hash[0]}")

# ------------------------------------------------------------------ Overview
if page == "Overview":
    st.title("Harmonised health-expenditure workbench")
    kp = q("""SELECT COUNT(*) n, SUM(final_status='AUTO') auto, SUM(final_status='REVIEW') rev, SUM(final_status='UNMAPPED') unm,
              SUM(final_status IN ('REVIEWED','UNMAPPABLE_CONFIRMED')) done, SUM(amount_usable=0) noamt FROM v_effective""").iloc[0]
    c = st.columns(5)
    c[0].metric("Records", f"{int(kp.n):,}"); c[1].metric("Auto-accepted", f"{int(kp.auto):,}")
    c[2].metric("Needs review", f"{int(kp.rev):,}"); c[3].metric("No supported target", f"{int(kp.unm):,}")
    c[4].metric("Excluded from totals (no amount)", f"{int(kp.noamt):,}")
    st.subheader("Classification status by country")
    by = st.radio("Measure", ["Record count", "Value (USD, reference FX)"], horizontal=True)
    if by == "Record count":
        d = q("SELECT country_code, final_status s, COUNT(*) v FROM v_effective GROUP BY 1,2")
    else:
        d = q("SELECT country_code, final_status s, SUM(amount_usd_ref) v FROM v_effective WHERE amount_usable=1 GROUP BY 1,2")
    d["s"] = d.s.map(STATUS_LABEL)
    st.bar_chart(d.pivot(index="country_code", columns="s", values="v").fillna(0))
    st.subheader("Source reconciliation")
    st.dataframe(q("""SELECT s.country_code country, s.file_name, s.format, s.n_raw_rows rows_read, s.control_total_raw control_in_source,
        (SELECT COUNT(*) FROM expenditure e WHERE e.country_code=s.country_code) harmonised_rows,
        (SELECT ROUND(SUM(amount_original),0) FROM expenditure e WHERE e.country_code=s.country_code AND amount_usable=1) sum_of_parsed_amounts,
        substr(s.sha256,1,12) sha256 FROM source_file s"""), width="stretch", hide_index=True)
    st.caption("Totals mix currencies inside Country C (RWF + USD); per-country values above are in the original currency of each record. "
               "Cross-country USD values use illustrative FX (config/fx_rates.csv) and different fiscal calendars - do not compare countries without SME sign-off.")

# ------------------------------------------------------------------ Review queue
elif page == "Review queue":
    st.title("Review queue")
    st.write("Records the rules could not classify reliably. Decide once per **account** (bulk) or per **record**; every decision is stored with reviewer, time and comment.")
    sha = q("SELECT sha_code, sha_description FROM ref_sha"); srhr = q("SELECT srhr_code, srhr_description FROM ref_srhr")
    sha_opts = [f"{r.sha_code} - {r.sha_description}" for r in sha.itertuples()]
    srhr_opts = [f"{r.srhr_code} - {r.srhr_description}" for r in srhr.itertuples()]

    def decision_form(key: str, scope: str, country: str, account: str | None, record_key: str | None, has_proposal: bool, proposal_txt: str):
        with st.form(key):
            st.markdown(f"**Decision scope:** {scope}")
            opts = (["Accept proposal"] if has_proposal else []) + ["Override with SHA/SRHR codes", "Mark unmappable (no supported target)"]
            choice = st.radio("Decision", opts)
            c1, c2 = st.columns(2)
            s_pick = c1.selectbox("SHA code (if overriding)", sha_opts); r_pick = c2.selectbox("SRHR code (if overriding)", srhr_opts)
            comment = st.text_input("Comment / evidence (required for override or unmappable)")
            reviewer = st.text_input("Reviewer", value="analyst")
            if st.form_submit_button("Save decision"):
                dec = "ACCEPT" if choice.startswith("Accept") else "OVERRIDE" if choice.startswith("Override") else "UNMAPPABLE"
                if dec != "ACCEPT" and not comment.strip():
                    st.error("Please add a comment."); return
                conn().execute("""INSERT INTO review_decision (scope,record_key,country_code,account_code,decision,sha_code,srhr_code,comment,reviewer,decided_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?)""",
                                (("account" if scope.startswith("All") else "record"), record_key, country, account, dec,
                                 s_pick.split(" - ")[0] if dec == "OVERRIDE" else None, r_pick.split(" - ")[0] if dec == "OVERRIDE" else None,
                                 comment, reviewer, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                conn().commit(); st.success("Saved."); st.rerun()

    tab1, tab2 = st.tabs(["By account (bulk)", "Individual records"])
    with tab1:
        acc = q("""SELECT country_code, account_code, account_label, concept_id, auto_status, ROUND(AVG(confidence),2) conf, COUNT(*) records,
                   ROUND(SUM(amount_usd_ref),0) usd_ref, MIN(auto_sha) proposed_sha, MIN(auto_srhr) proposed_srhr, MIN(rationale) why
                   FROM v_effective WHERE final_status IN ('REVIEW','UNMAPPED') GROUP BY 1,2,3,4,5 ORDER BY records DESC""")
        st.caption("Accounts still open. Exceptions (suspect text, code/text conflicts) are excluded from bulk decisions and remain individually reviewable.")
        sel = st.dataframe(acc, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row")
        if sel.selection.rows:
            r = acc.iloc[sel.selection.rows[0]]
            st.markdown(f"**{r.country_code} / {r.account_code} - {r.account_label}** ({int(r.records)} records)")
            st.info(r.why)
            decision_form(f"acc_{r.country_code}_{r.account_code}", "All records of this account (except flagged exceptions)", r.country_code, r.account_code, None,
                          r.proposed_sha is not None, r.proposed_sha or "")
    with tab2:
        f1, f2, f3 = st.columns(3)
        cty = f1.multiselect("Country", ["CTA", "CTB", "CTC"], default=["CTA", "CTB", "CTC"])
        kind = f2.selectbox("Reason", ["All open", "Code/text conflict", "Suspect text (instruction-like)", "Unknown account", "Needs review (low confidence)", "No supported target"])
        txt = f3.text_input("Search description / txn id")
        where, par = ["final_status IN ('REVIEW','UNMAPPED')", f"country_code IN ({','.join('?' * len(cty))})"], list(cty)
        if kind == "Code/text conflict": where.append("method LIKE 'code_text_conflict%'")
        elif kind == "Suspect text (instruction-like)": where.append("text_trust='SUSPECT'")
        elif kind == "Unknown account": where.append("concept_id IS NULL")
        elif kind == "Needs review (low confidence)": where.append("final_status='REVIEW'")
        elif kind == "No supported target": where.append("final_status='UNMAPPED'")
        if txt: where.append("(description_original LIKE ? OR source_txn_id LIKE ?)"); par += [f"%{txt}%"] * 2
        recs = q(f"""SELECT record_key, source_txn_id txn_id, txn_date, account_code, account_label, description_original, concept_id, auto_sha proposed_sha,
                     auto_srhr proposed_srhr, confidence, auto_status status, amount_original amount, currency_original ccy, rationale FROM v_effective
                     WHERE {' AND '.join(where)} ORDER BY (text_trust='SUSPECT') DESC, confidence LIMIT 500""", par)
        st.caption(f"{len(recs)} shown (max 500). Suspect-text and conflict records are listed first.")
        s2 = st.dataframe(recs.drop(columns=["rationale"]), width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row")
        if s2.selection.rows:
            r = recs.iloc[s2.selection.rows[0]]
            raw = q("SELECT payload_json FROM raw_record WHERE record_key=?", (r.record_key,)).payload_json[0]
            a, b = st.columns([3, 2])
            with a:
                st.markdown(f"**{r.record_key}** - {r.description_original}"); st.info(r.rationale)
                st.caption("Original source payload"); st.code(json.dumps(json.loads(raw), indent=2, ensure_ascii=False), language="json")
                dq = q("SELECT rule, severity, message FROM dq_issue WHERE exp_id=(SELECT exp_id FROM expenditure WHERE record_key=?)", (r.record_key,))
                if len(dq): st.dataframe(dq, hide_index=True, width="stretch")
            with b:
                decision_form(f"rec_{r.record_key}", "This record only", r.record_key.split(":")[0], r.account_code, r.record_key,
                              r.proposed_sha is not None, r.proposed_sha or "")

# ------------------------------------------------------------------ Data quality
elif page == "Data quality":
    st.title("Data-quality findings")
    base = """FROM dq_issue i LEFT JOIN expenditure e ON e.exp_id=i.exp_id LEFT JOIN source_file s ON s.file_id=i.file_id"""
    summ = q(f"""SELECT COALESCE(e.country_code, s.country_code) country, i.severity, i.rule, COUNT(*) n {base} GROUP BY 1,2,3""")
    order = {"critical": 0, "error": 1, "warning": 2, "info": 3}
    pv = summ.pivot_table(index=["severity", "rule"], columns="country", values="n", aggfunc="sum", fill_value=0).reset_index()
    pv = pv.sort_values("severity", key=lambda s: s.map(order))
    st.dataframe(pv, width="stretch", hide_index=True)
    rule = st.selectbox("Drill into a rule", sorted(summ.rule.unique()))
    st.dataframe(q(f"""SELECT COALESCE(e.country_code, s.country_code) country, e.record_key, e.source_txn_id, i.severity, i.message, i.action_taken {base}
                       WHERE i.rule=? LIMIT 500""", (rule,)), width="stretch", hide_index=True)

# ------------------------------------------------------------------ Explore
elif page == "Explore results":
    st.title("Classified expenditure")
    m = st.radio("Measure", ["USD reference", "Records"], horizontal=True)
    inc = st.multiselect("Include statuses", ["AUTO", "REVIEWED", "REVIEW"], default=["AUTO", "REVIEWED"],
                         help="'REVIEW' = machine proposal not yet confirmed by an analyst; keep it out for defensible totals.")
    dim = st.radio("Dimension", ["SHA", "SRHR"], horizontal=True)
    col = "final_sha" if dim == "SHA" else "final_srhr"
    df = q(f"""SELECT country_code, final_status, {col} code, amount_usd_ref, amount_usable FROM v_effective""")
    df["bucket"] = df.apply(lambda r: r.code if r.final_status in inc and r.code else "(not classified / unresolved)", axis=1)
    if m == "Records": g = df.groupby(["bucket", "country_code"]).size().unstack(fill_value=0)
    else: g = df[df.amount_usable == 1].groupby(["bucket", "country_code"]).amount_usd_ref.sum().unstack(fill_value=0).round(0)
    ref = q(f"SELECT {'sha_code code, sha_description' if dim == 'SHA' else 'srhr_code code, srhr_description'} descr FROM ref_{dim.lower()}").set_index("code")
    g.insert(0, "description", [ref.descr.get(i, "") for i in g.index])
    st.dataframe(g, width="stretch")
    st.caption("USD values use illustrative FX and countries have different fiscal years (A/C Jul-Jun, B Oct-Sep). Negative amounts are kept (net). Records with no amount are excluded from value views.")

# ------------------------------------------------------------------ Trace
elif page == "Trace a record":
    st.title("Trace a record to its source")
    t = st.text_input("Transaction id (e.g. KE-2400001, SN-2024000000, RW-2024000012) or record key")
    if t:
        e = q("SELECT * FROM v_effective WHERE source_txn_id=? OR record_key=?", (t, t))
        if e.empty: st.warning("Not found.")
        for _, r in e.iterrows():
            st.markdown(f"### {r.record_key}")
            lin = q("""SELECT s.file_name, s.sha256, s.format, s.loaded_at, r.source_locator, p.run_id, p.config_hash FROM raw_record r
                       JOIN source_file s ON s.file_id=r.file_id JOIN pipeline_run p ON p.run_id=s.run_id WHERE r.record_key=?""", (r.record_key,)).iloc[0]
            st.write(f"**Source:** `{lin.file_name}` ({lin.format}) - **{lin.source_locator}** - sha256 `{lin.sha256[:16]}...` - loaded {lin.loaded_at} - pipeline run {lin.run_id} / config `{lin.config_hash}`")
            c1, c2 = st.columns(2)
            with c1:
                st.caption("Original payload"); st.code(json.dumps(json.loads(q("SELECT payload_json FROM raw_record WHERE record_key=?", (r.record_key,)).payload_json[0]), indent=2, ensure_ascii=False), language="json")
            with c2:
                st.caption("Harmonised + classification")
                st.json({k: (None if pd.isna(v) else v) for k, v in r.items() if k not in ("description_used",)})
            st.caption("Data-quality issues"); st.dataframe(q("SELECT rule, severity, message, action_taken FROM dq_issue WHERE exp_id=?", (int(r.exp_id),)), hide_index=True, width="stretch")
            sp = q("SELECT source_split_id, description, amount FROM expenditure_split WHERE exp_id=?", (int(r.exp_id),))
            if len(sp): st.caption("Sub-transactions (not summed)"); st.dataframe(sp, hide_index=True)

# ------------------------------------------------------------------ Mappings & audit
else:
    st.title("Mappings and audit trail")
    st.subheader("Country account -> concept -> SHA / SRHR")
    mp = q("""SELECT a.country_code, a.account_code, a.account_label, a.concept_id, c.target, c.sha_code, c.srhr_code, c.base_confidence, c.alt_sha_code, c.rationale
              FROM coa_account a JOIN concept c USING(concept_id) ORDER BY a.concept_id, a.country_code""")
    st.dataframe(mp, width="stretch", hide_index=True)
    st.caption("Source of truth: config/classification/*.csv (version-controlled). Editing those files and re-running the pipeline is how mappings evolve.")
    st.subheader("Review decisions (append-only)")
    st.dataframe(q("SELECT * FROM review_decision ORDER BY decision_id DESC"), width="stretch", hide_index=True)
    st.download_button("Download effective classification (CSV)", q("SELECT * FROM v_effective").to_csv(index=False), "effective_classification.csv")
