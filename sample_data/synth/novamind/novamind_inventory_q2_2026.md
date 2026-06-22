# NovaMind AI — AI Asset & Vendor Inventory Register
**Reporting Period:** Q2 2026 (Apr–Jun) &nbsp;|&nbsp; **Prepared by:** Platform Engineering / FinOps &nbsp;|&nbsp; **Distribution:** Portfolio Operations Team

## 1. Overview

This register tracks all active AI vendor contracts, cloud infrastructure invoices, and model licensing spend for NovaMind AI's AI platform, broken out by spend category and workload tag for cost attribution and audit purposes.

## 2. Vendor Invoice Detail

| Vendor Invoice ID | Vendor | Spend Category | Workload Tag | Vendor Invoice Amount |
|---|---|---|---|---|
| INV-2026Q2-0114 | Amazon Web Services | cloud_compute | underwriting-inference | $1.15M |
| INV-2026Q2-0115 | Google Cloud Platform | cloud_compute | model-training | $0.75M |
| INV-2026Q2-0201 | OpenAI | api_licensing | underwriting-llm | $0.52M |
| INV-2026Q2-0202 | Anthropic | api_licensing | fraud-detection-llm | $0.33M |
| INV-2026Q2-0301 | Snowflake | data_storage | feature-store | $0.38M |
| INV-2026Q2-0302 | Databricks | data_storage | data-pipeline | $0.22M |

The cloud invoice amount across AWS and GCP totals $1.9M for the quarter, matching cloud spend reported in the Q2 financial statement. Combined vendor invoice amount across all API licensing vendors (OpenAI and Anthropic) is $0.85M, consistent with reported API licensing spend.

## 3. Spend by Model Family

Total AI spend for the quarter is $5.2M. The breakdown of model family spend across primary LLM vendors is:

| Model Family | Model Family Spend | % of Total AI Spend |
|---|---|---|
| OpenAI (GPT family) | $0.52M | 10.0% |
| Anthropic (Claude family) | $0.33M | 6.3% |
| Internal proprietary credit-risk models | $0.00M (talent-driven, no external license fee) | 0.0% |

## 4. Committed & Run-Rate Spend (Forecast Inputs)

- **Committed spend:** $2.1M — represents contracted AI commitments already locked in for the remainder of fiscal year 2026, including reserved cloud instances and the annual enterprise API contract renewal with Anthropic.
- **Current run-rate spend:** $1.733M per month — current monthly AI run rate based on Q2 actuals (total AI spend of $5.2M ÷ 3 months).
- **Remaining months in year:** 6 — months left in fiscal year from end of Q2 (July through December 2026).

Using these inputs, forecasted AI spend for the remainder of FY2026 is projected at approximately $12.5M (committed spend plus current run-rate spend extended across the remaining months in year).

## 5. Cost Category Summary

| Cost Category | Quarterly Spend |
|---|---|
| Cloud compute cost | $1.9M |
| Model licensing cost | $0.85M |
| Data infrastructure cost | $0.6M |
| AI team compensation cost | $1.85M |
| **Total AI operating cost** | **$5.2M** |

## 6. Notes

All invoices have been reconciled against the vendor portal and matched to internal cost center codes. No discrepancies were identified during this quarter's close. AI ROI calculated against this spend base is 3.1x, consistent with the Finance team's Q2 financial report.
