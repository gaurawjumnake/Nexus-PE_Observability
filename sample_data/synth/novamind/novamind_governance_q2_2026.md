# NovaMind AI — AI Governance & Compliance Report
**Reporting Period:** Q2 2026 &nbsp;|&nbsp; **Prepared by:** Risk & Compliance Office &nbsp;|&nbsp; **Distribution:** Board Risk Committee, Portfolio Operations

## 1. Purpose

This report documents NovaMind AI's governance posture across its AI policy population, data pipeline compliance, model risk, and regulatory readiness for the quarter ended 30 June 2026.

## 2. AI Policy Population & Conformance

The total employees and vendors in scope under the AI usage policy is 220 (total entities). Of these, 207 are policy-compliant employees and vendors (compliant entities), yielding an AI policy conformance rate of 94.1% this quarter, a 2-point improvement over Q1.

| Metric | Value |
|---|---|
| Total entities (AI policy population count) | 220 |
| Compliant entities | 207 |
| Policy conformance rate | 94.1% |

## 3. Data Pipeline Privacy Compliance

Total data pipelines audited this quarter: 25. Of these, 24 are privacy-compliant pipelines, reflecting strong adherence to data handling standards across the underwriting and fraud-detection data flows.

The data privacy compliance score (GDPR/CCPA compliance score) for the quarter is 88.0/100.

## 4. Model Risk Assessment

The model bias risk score across NovaMind's production credit models is 14.0 (on a 0–100 scale, lower indicates lower bias risk), reflecting continued investment in fairness testing for the underwriting model suite.

## 5. Regulatory Readiness

The regulatory readiness score (AI regulation preparedness rating) for NovaMind this quarter is 70.0/100, reflecting partial readiness for upcoming state-level AI lending disclosure requirements, with remediation work planned for Q3.

## 6. AI Governance Maturity

The governance maturity rating (portco governance rating / AI governance maturity score per portco) is assessed at 4.1/5 this quarter, driven by the formalization of the model risk committee charter and quarterly bias audits. This also serves as NovaMind's portfolio AI governance maturity score for this cycle.

The portfolio AI maturity score (cross-portfolio AI capability score) for NovaMind is 3.8/5. The portco AI maturity score (individual portco maturity score) and portco technical maturity rating (MLOps maturity rating) are 3.8 and 3.6 respectively, while the technical maturity score (portfolio technical maturity) used for governance weighting purposes is also 3.6.

Talent readiness score (AI talent maturity score) and portco talent readiness rating (company AI talent maturity rating) both stand at 3.4/5 this quarter. Strategic alignment score (AI strategy alignment score) and portco strategic alignment rating (AI strategy alignment rating) are both 4.3/5.

The governance risk composite score (AI governance score / AI compliance composite score) for NovaMind this quarter is 81.0/100.

Composite policy conformance scoring input for the governance score calculation: policy component 76.0/100, weighted alongside data privacy compliance (88.0, 30% weight), inverted model bias risk (86.0, 20% weight), and regulatory readiness score (AI regulation preparedness score / portfolio regulatory readiness) of 70.0 (10% weight).

Peer governance score distribution (industry governance benchmark / benchmark governance distribution) for the fintech panel this quarter: p25: 62, p50: 74, p75: 86, against which NovaMind's 81.0 score places it in the upper-middle tier.

## 7. Open Items / Incident Tracking

| Incident ID | Incident Type | Incident Severity | Incident Status | Incident Period | Incident Resolution Time | Output ID | Output Criticality | Human Review Completed |
|---|---|---|---|---|---|---|---|---|
| INC-2026-0411 | model_drift_alert | major | resolved | 2026-05 | 90 minutes | OUT-22018 | high | true |
| INC-2026-0419 | data_access_anomaly | major | resolved | 2026-06 | 105 minutes | OUT-22041 | medium | true |

Both incidents this quarter were classified as P2 (major) severity with no P1 (critical) incidents recorded. Incident resolution time (time to resolve incident) averaged 97.5 minutes across the two events, both fully resolved within SLA. Both associated AI outputs (review record IDs) had human review completed (reviewed by human flag) confirmed.

Of the 25 total data pipelines audited this quarter, 24 are privacy-compliant pipelines (compliant AI pipelines), unchanged from the figure reported in Section 3 above.

## 8. Vendor Compliance

| Vendor ID | Vendor | Compliance Status |
|---|---|---|
| VEND-OAI-001 | OpenAI | compliant |
| VEND-ANT-002 | Anthropic | compliant |
| VEND-AWS-003 | Amazon Web Services | compliant |

All third-party AI vendor compliance states are confirmed compliant as of this reporting period, with security questionnaires renewed annually.
