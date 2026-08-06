# Document: tmp6uxiapcv.md


## Page 1

# NovaMind AI — Independent AI Model Audit Report

**Reporting Period:** Q2 2026  |  **Prepared by:** Internal Audit &amp; Model Risk Team  |  **Distribution:** Audit Committee, Portfolio Operations

## 1. Audit Scope

This audit reviewed NovaMind AI's model outputs, data pipeline compliance, and incident handling for the quarter ended 30 June 2026, sampling production decisions from the underwriting and fraud-detection model suites.

## 2. AI Output Review Sample

| Output ID   | Evaluated Output ID   | Output Criticality   | Hallucination Flag   | Human Review Completed   |
|-------------|-----------------------|----------------------|----------------------|--------------------------|
| OUT-22014   | EVAL-22014            | high                 | false                | true                     |
| OUT-22018   | EVAL-22018            | high                 | false                | true                     |
| OUT-22041   | EVAL-22041            | medium               | true                 | true                     |
| OUT-22055   | EVAL-22055            | low                  | false                | true                     |
| OUT-22067   | EVAL-22067            | high                 | false                | true                     |

Of 5 sampled high- and medium-criticality AI outputs (decision criticality level), all 5 were reviewed by a human (human-in-the-loop review status: completed), and 1 was flagged for a factual error (hallucination detected indicator), consistent with the telemetry log for the same period.

## 3. Model Bias &amp; Risk Scoring

The model bias risk score (AI bias risk rating) assigned by the independent audit team this quarter: 14.0/100, within acceptable risk tolerance for production lending models.

## 4. Data Pipeline Compliance

Total AI data pipelines audited: 25. Compliant AI pipelines (privacy-compliant pipelines count): 24, yielding a 96% pipeline compliance rate, consistent with the governance team's self-reported figure.

Data privacy compliance score (GDPR/CCPA compliance score): 88.0/100.

## 5. Governance &amp; Regulatory Findings

- **Governance maturity rating** (portco governance rating): 4.1/5
- **Regulatory readiness rating** (AI regulation preparedness rating / portfolio regulatory readiness): 70.0/100

## 6. Vendor Security Review

| Vendor ID    | Vendor              | Vendor Security Compliance Status   |
|--------------|---------------------|-------------------------------------|
| VEND-OAI-001 | OpenAI              | compliant                           |
| VEND-ANT-002 | Anthropic           | compliant                           |
| VEND-AWS-003 | Amazon Web Services | compliant                           |

## 7. Incident Review

| Incident ID   | Incident Type       | Incident Severity   | Incident Status   | Incident Period   | Incident Resolution Time   |
|---------------|---------------------|---------------------|-------------------|-------------------|----------------------------|
| INC-2026-0411 | model_drift_alert   | major               | resolved          | 2026-05           | 90 minutes                 |
| INC-2026-0419 | data_access_anomaly | major               | resolved          | 2026-06           | 105 minutes                |

Total entities in scope for this audit cycle: 220, of which 207 were found policy-compliant (compliant entities), matching the Risk &amp; Compliance Office's quarterly self-assessment.

## 8. Audit Opinion

Based on the sample reviewed, Internal Audit finds NovaMind AI's model governance controls to be operating effectively, with one minor finding related to hallucination detection latency on the fraud-detection endpoint, to be remediated in Q3.