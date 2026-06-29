# Document: tmpex4i1t06.md


## Page 1

# NovaMind AI — AI System Telemetry &amp; Usage Report

**Reporting Period:** Q2 2026 (01 Apr – 30 Jun, 129,600 total minutes in period)  |  **Prepared by:** Platform SRE Team

## 1. System Reliability

Total minutes in period for Q2 2026: 129,600 (90 days). Total downtime minutes recorded across all production AI services: 388.8 minutes, yielding 99.7% uptime for the quarter — a 0.2 percentage-point improvement over Q1.

API response time (LLM inference latency, P95): 340 milliseconds average across the underwriting and fraud-detection inference endpoints.

## 2. API Call Log Summary

All API calls below are logged against project stage "production" projects (project status: production) actively serving traffic.

| API Call ID   | Project ID   |   API Status Code | API Response Time   | Hallucination Flag   | Fallback Triggered   | Evaluated Output ID   | Request ID   | Interaction Date   |
|---------------|--------------|-------------------|---------------------|----------------------|----------------------|-----------------------|--------------|--------------------|
| CALL-88421    | PRJ-NM-001   |               200 | 312ms               | false                | false                | EVAL-22014            | REQ-88421    | 2026-04-12         |
| CALL-88422    | PRJ-NM-001   |               200 | 298ms               | false                | false                | EVAL-22015            | REQ-88422    | 2026-04-14         |
| CALL-88450    | PRJ-NM-002   |               200 | 355ms               | false                | false                | EVAL-22018            | REQ-88450    | 2026-05-02         |
| CALL-88475    | PRJ-NM-002   |               500 | 1100ms              | false                | true                 | EVAL-22030            | REQ-88475    | 2026-05-19         |
| CALL-88502    | PRJ-NM-003   |               200 | 340ms               | true                 | true                 | EVAL-22041            | REQ-88502    | 2026-06-08         |

Hallucination detected indicator was flagged on the evaluated output identifier EVAL-22041 (1 of 5 sampled calls this period), which triggered a secondary system fallback and was routed for human review.

## 3. AI Incident Log

| Incident ID   | Incident Type       | Incident Severity   | Incident Status   | Incident Period   | Incident Resolution Time   |
|---------------|---------------------|---------------------|-------------------|-------------------|----------------------------|
| INC-2026-0411 | model_drift_alert   | major               | resolved          | 2026-05           | 90 minutes                 |
| INC-2026-0419 | data_access_anomaly | major               | resolved          | 2026-06           | 105 minutes                |

No critical (P1) incidents were recorded this quarter. Both major (P2) incidents were resolved within internal SLA targets.

## 4. User Activity &amp; Tool Usage

A sample of weekly active employee sessions is tracked by staff ID (employee identifier) for adoption analytics, e.g. employee ID EMP-3301, EMP-3402, and EMP-3588 among the top power users this quarter.

Active AI users (weekly active AI users) this quarter: 482, representing 71% of the eligible workforce — a 14% increase quarter-over-quarter.

| Usage Metric                                          |   Weekly Value |
|-------------------------------------------------------|----------------|
| Weekly AI interactions (weekly AI tool sessions)      |          1,928 |
| Copilot weekly sessions (copilot tool usage sessions) |          1,200 |
| Custom AI agent usage sessions                        |            650 |
| Embedded analytics weekly sessions                    |            870 |

Daily AI interactions (daily AI tool sessions) averaged 386 across the engineering and underwriting teams.

## 5. AI Spend Reference

Total AI spend for the quarter, as reported by Finance and reconciled against platform billing telemetry: $5.2M. Quarterly AI ROI (return on AI investment) calculated from this spend base: 3.1x.

## 6. Successful Outcomes

Total AI successful outcomes (AI outcomes delivered) recorded across all production projects this quarter: 14,820, spanning automated underwriting decisions, fraud flags resolved, and customer support deflections.