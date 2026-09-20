# Analysis findings

Generated from the harmonised data (`python -m hxh.analysis`; also the **Analysis** page of the app). Values in USD use the
*illustrative* reference FX in `config/fx_rates.csv`. All numbers are for the synthetic sample.

## 1. What the classification does and does not settle
- Records by status (n = 7,000): 39 % auto-accepted, 30 % needs review, 31 % no supported target.
- Shares by **value** track shares by record (e.g. Country A: 34.7 % / 29.0 % / 36.2 % by value vs 34.9 % / 29.0 % / 36.0 % by count),
  so review is not concentrated in unusually large or small transactions.
- **Uncertainty band** (Analysis page): for each SHA code, *low* = auto-accepted only, *high* = plus every review record whose proposed
  or alternative code is that code. Example, Country B, HC.5.1: USD 8.9 m (auto only) to 17.0 m (if all review records land there).
  Codes such as HC.6.2 (vaccines) have no gap; HC.5.1, HC.6.3, HC.1.3 vs HC.6.4 depend on analyst decisions.
  Report a range, not a point estimate, until the review queue is worked.

## 2. Are the countries comparable? Not yet.
- After conversion with the reference FX, the **median transaction** is about USD 0.7 k (A), 18.6 k (B), 1.3 k (C): B is ~27 x A.
  No plausible exchange rate explains that gap. Possible causes to confirm with the country team: amounts in thousands, a different
  extract scope (e.g. consolidated lines), or a genuinely different scale. **Do not compare countries or sum across them.**
- Country C carries USD and RWF records. The RWF/USD rate that would make USD-row and RWF-row medians agree, account by account, is ~916
  (IQR 527-1,191) against the 1,240 reference. Only ~13 USD rows per account, so weak evidence, but it shows results are sensitive to the FX assumption.
- Fiscal years differ (A, C: Jul-Jun; B: Oct-Sep). Monthly profiles are flat in all three (no seasonality), and 5 Country C postings dated
  2027 carry negligible value.

## 3. Which fields carry signal?
- Association (Cramer's V) between **ministry** or **supplier** and the classified concept equals the value expected under
  independence in all three countries (e.g. Country A: ministry 0.097 vs baseline 0.087; supplier 0.088 vs 0.087).
- Only ~20 % of records on health-specific accounts are booked to the health ministry, i.e. the share you would expect if ministries were
  assigned at random among ~5. Ministry and supplier therefore behave like random labels, which is why classification uses the account code.

## 4. Reversals and outliers
- Country A has 54 negative records (net effect about -2.2 % of gross). **None** has a positive record with the same account and amount, so they
  cannot be confirmed as reversals of known entries. Kept, flagged, totals are net. Countries B and C have none.
- No statistical outliers: within each country and account, no record exceeds a robust z-score of 3.5 on log amount.
  Amounts within an account are wide but smooth, so outlier detection is not a useful control on this sample.

## What this means for next steps
1. Ask the country teams to confirm units and extract scope (Country B first) and currency labelling (Country C).
2. Work the review queue by account for the concepts with the widest bands (HC.5.1, HC.1.3 / HC.6.4, HC.7).
3. Replace illustrative FX with official period averages and re-run; report ranges alongside point values.
