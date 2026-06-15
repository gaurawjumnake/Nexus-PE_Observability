import json, os, yaml

# facts_map = json.load(open('/home/claude/facts_map.json'))  # fact -> [kpi_ids]
# docs_map = json.load(open('/home/claude/docs_map.json'))    # kpi_id -> [doc sources]



# Helper to compute document sources for a fact = union of doc sources of KPIs using it
# def doc_sources_for(fact):
#     docs = set()
#     for kpi in facts_map[fact]:
#         docs.update(docs_map.get(kpi, []))
#     # order by canonical priority
#     order = ["financial","governance","project_portfolio","telemetry","operations",
#              "crm","hr","benchmark","inventory","audit","board_deck"]
#     return [d for d in order if d in docs]

def src_priority(docs):
    # source_priority subset of: financial, board_deck, crm, telemetry, governance, audit, hr, operations, project_portfolio, benchmark, inventory
    pri_order = ["financial","board_deck","governance","audit","telemetry","operations",
                  "project_portfolio","crm","hr","inventory","benchmark"]
    return [d for d in pri_order if d in docs]

# ===========================================================================
# FACT DEFINITIONS
# Each entry: category, data_type, unit, fact_type, name override (optional),
# description, business_definition, aggregation_strategy, related_facts,
# example_values, validation overrides, normalization overrides,
# alias extra words, extraction extra patterns, missing_value_strategy,
# confidence overrides
# ===========================================================================

# Category mapping by domain
CATS = {
    "financial": "financial", "revenue": "revenue", "cost": "cost",
    "adoption": "adoption", "usage": "usage", "project": "project",
    "governance": "governance", "risk": "risk", "security": "security",
    "reliability": "reliability", "telemetry": "telemetry",
    "benchmark": "benchmark", "maturity": "maturity", "operations": "operations",
    "hr": "hr", "inventory": "inventory"
}

DOC_SOURCES_ENUM = ["financial","governance","project_portfolio","telemetry","operations",
                     "crm","hr","benchmark","inventory","audit","board_deck"]

def base_validation(data_type):
    if data_type == "currency":
        return {"min": 0, "required_type": "currency"}
    if data_type == "percentage":
        return {"min": 0, "max": 100, "required_type": "percentage"}
    if data_type == "integer":
        return {"min": 0, "required_type": "integer"}
    if data_type == "float":
        return {"min": 0, "required_type": "float"}
    if data_type == "enum":
        return {"required_type": "enum"}
    if data_type == "string":
        return {"required_type": "string"}
    if data_type == "date":
        return {"required_type": "date"}
    if data_type == "boolean":
        return {"required_type": "boolean"}
    return {"required_type": data_type}

def base_normalization(data_type, unit):
    if data_type == "currency":
        return {"currency": "USD", "scale": {"K": 1000, "M": 1000000, "B": 1000000000}}
    if unit == "percentage" or data_type == "percentage":
        return {"scale_to": "0-100", "decimal_to_percent_multiplier": 100}
    if unit == "minutes":
        return {"normalize_to": "minutes", "hours_to_minutes_multiplier": 60}
    if unit == "milliseconds":
        return {"normalize_to": "milliseconds", "seconds_to_ms_multiplier": 1000}
    return {}

def confidence_rules(critical=False):
    if critical:
        return {"minimum_confidence": 0.90, "auto_approve": 0.97, "manual_review_below": 0.90}
    return {"minimum_confidence": 0.80, "auto_approve": 0.95, "manual_review_below": 0.80}

# -----------------------------------------------------------------------
# Per-fact definitions. fact_id -> dict
# -----------------------------------------------------------------------
F = {}

def add(fact_id, name, category, data_type, unit, fact_type, description,
        business_definition, aggregation_strategy, related_facts=None,
        example_values=None, possible_aliases=None, extraction_patterns=None,
        validation_rules=None, normalization_rules=None,
        missing_value_strategy="ask_user", confidence_critical=False, status="active"):
    F[fact_id] = dict(
        fact_id=fact_id, name=name, category=category, data_type=data_type, unit=unit,
        fact_type=fact_type, description=description, business_definition=business_definition,
        aggregation_strategy=aggregation_strategy,
        related_facts=related_facts or [],
        example_values=example_values or [],
        possible_aliases=possible_aliases or [],
        extraction_patterns=extraction_patterns or [],
        validation_rules=validation_rules or base_validation(data_type),
        normalization_rules=normalization_rules if normalization_rules is not None else base_normalization(data_type, unit),
        missing_value_strategy=missing_value_strategy,
        confidence_critical=confidence_critical,
        status=status,
    )

# ============================ REVENUE FACTS ===============================
add("direct_ai_revenue","Direct AI Revenue","revenue","currency","USD","raw",
    "Revenue from products or features where AI is the primary value driver.",
    "Revenue generated directly by AI-native products or AI-led feature lines, where AI is the core value proposition rather than a supporting capability.",
    "sum",
    related_facts=["ai_assisted_revenue","pipeline_influenced_revenue","ai_revenue","revenue_line_amount","ai_attribution_type"],
    example_values=[14100000, 5200000],
    possible_aliases=["direct ai revenue","direct AI product revenue","AI-native revenue","pure AI revenue","AI primary revenue"],
    extraction_patterns=["direct ai revenue","revenue from ai-native products","ai-led feature revenue","ai primary driver revenue"],
    missing_value_strategy="insufficient_data")

add("ai_assisted_revenue","AI-Assisted Revenue","revenue","currency","USD","raw",
    "Revenue from deals or renewals where AI tools materially assisted sales or customer success workflows.",
    "Revenue attributable to deals, upsells, or renewals where AI tooling (e.g., AI-powered sales enablement) materially influenced the outcome but was not the core product.",
    "sum",
    related_facts=["direct_ai_revenue","pipeline_influenced_revenue","ai_revenue","revenue_line_amount","ai_attribution_type"],
    example_values=[6400000, 2100000],
    possible_aliases=["ai assisted revenue","ai-supported revenue","revenue assisted by ai tools","ai-enabled sales revenue"],
    extraction_patterns=["ai-assisted revenue","revenue assisted by ai","ai supported deals revenue","sales accelerated by ai"],
    missing_value_strategy="insufficient_data")

add("pipeline_influenced_revenue","Pipeline Influenced Revenue","revenue","currency","USD","raw",
    "Revenue from pipeline opportunities generated or materially influenced by AI-driven marketing or outreach.",
    "Revenue from sales pipeline opportunities that were sourced, qualified, or substantially influenced by AI-driven marketing, lead-scoring, or outreach systems.",
    "sum",
    related_facts=["direct_ai_revenue","ai_assisted_revenue","ai_revenue","revenue_line_amount","ai_attribution_type"],
    example_values=[18900000, 7300000],
    possible_aliases=["pipeline influenced revenue","ai-sourced pipeline revenue","ai-driven pipeline revenue","ai marketing influenced revenue"],
    extraction_patterns=["pipeline influenced by ai","revenue from ai-generated pipeline","ai-driven outreach revenue"],
    missing_value_strategy="insufficient_data")

add("revenue_line_amount","Revenue Line Amount","revenue","currency","USD","raw",
    "Individual revenue line item amount from financial or CRM systems, prior to AI attribution categorization.",
    "The raw dollar amount of an individual revenue transaction or line item, used as the base unit for AI attribution classification (direct/assisted/pipeline_influenced).",
    "sum",
    related_facts=["ai_attribution_type","direct_ai_revenue","ai_assisted_revenue","pipeline_influenced_revenue"],
    example_values=[125000, 48000],
    possible_aliases=["revenue line item","revenue amount","line item revenue","deal revenue amount"],
    extraction_patterns=["revenue line","deal amount","revenue line item amount","transaction revenue amount"],
    missing_value_strategy="insufficient_data")

add("ai_attribution_type","AI Attribution Type","revenue","enum","string","raw",
    "Classification of how a revenue line is attributed to AI: direct, assisted, or pipeline_influenced.",
    "A categorical label assigned to each revenue line indicating the nature of AI's contribution to that revenue, used to segment AI revenue KPIs.",
    "latest_value",
    related_facts=["revenue_line_amount","direct_ai_revenue","ai_assisted_revenue","pipeline_influenced_revenue"],
    example_values=["direct","assisted","pipeline_influenced"],
    possible_aliases=["ai attribution","ai revenue category","ai contribution type","revenue attribution classification"],
    extraction_patterns=["ai attribution type","classified as direct ai revenue","classified as ai-assisted","pipeline influenced classification"],
    validation_rules={"required_type":"enum","allowed_values":["direct","assisted","pipeline_influenced"]},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("cross_sell_revenue_with_ai","Cross-Sell Revenue With AI","revenue","currency","USD","raw",
    "Cross-sell revenue generated with the assistance of AI-driven customer intelligence.",
    "Total cross-sell/upsell revenue realized in the period where AI-driven customer intelligence tools (recommendation engines, propensity models) were used in the sales process.",
    "sum",
    related_facts=["cross_sell_revenue_baseline","cross_sell_uplift"],
    example_values=[5000000],
    possible_aliases=["cross-sell revenue with ai","ai-enabled cross-sell revenue","cross sell revenue ai-assisted"],
    extraction_patterns=["cross-sell revenue with ai","ai-driven cross-sell revenue"],
    missing_value_strategy="exclude_portco")

add("cross_sell_revenue_baseline","Cross-Sell Revenue Baseline","revenue","currency","USD","raw",
    "Baseline (pre-AI or non-AI) cross-sell revenue used for comparison.",
    "Cross-sell/upsell revenue that would be expected without AI-driven customer intelligence, used as the comparison baseline for the cross-sell uplift calculation.",
    "sum",
    related_facts=["cross_sell_revenue_with_ai","cross_sell_uplift"],
    example_values=[3500000],
    possible_aliases=["cross-sell baseline revenue","pre-ai cross-sell revenue","baseline cross-sell revenue"],
    extraction_patterns=["cross-sell baseline","baseline cross-sell revenue","pre-ai cross-sell revenue"],
    missing_value_strategy="exclude_portco")

# ============================ FINOPS / COST FACTS ==========================
add("total_ai_spend","Total AI Spend","cost","currency","USD","derived",
    "Year-to-date cloud, licensing, and compute costs for all AI initiatives across the portfolio.",
    "The aggregate operating cost of AI initiatives, summing cloud compute, API/model licensing, storage, and dedicated AI talent costs across all PortCos.",
    "sum",
    related_facts=["cloud_spend","api_licensing_spend","storage_data_spend","talent_ai_teams_spend","ai_roi","budget_adherence","budget_variance","cost_per_outcome","payback_period","spend_by_model_family"],
    example_values=[12800000],
    possible_aliases=["total ai spend","total ai cost","aggregate ai expenditure","total ai operating cost"],
    extraction_patterns=["total ai spend","total ai cost","aggregate ai expenditure ytd"],
    confidence_critical=True,
    missing_value_strategy="insufficient_data")

add("cloud_invoice_amount","Cloud Invoice Amount","cost","currency","USD","raw",
    "Individual cloud infrastructure invoice amount, prior to AI workload tagging.",
    "The raw dollar amount on a cloud provider invoice line item, prior to filtering by AI workload tag.",
    "sum",
    related_facts=["workload_tag","cloud_spend"],
    example_values=[450000, 120000],
    possible_aliases=["cloud invoice amount","cloud bill amount","cloud cost line item"],
    extraction_patterns=["cloud invoice amount","cloud bill line item"],
    missing_value_strategy="use_last_known")

add("workload_tag","Workload Tag","cost","string","string","raw",
    "Tag applied to cloud resources/invoices indicating workload type (e.g., 'ai').",
    "A metadata tag attached to cloud cost line items used to identify whether the underlying compute/storage is attributable to AI workloads.",
    "latest_value",
    related_facts=["cloud_invoice_amount","cloud_spend"],
    example_values=["ai", "non-ai"],
    possible_aliases=["workload tag","cost tag","resource tag","ai workload label"],
    extraction_patterns=["workload tag = ai","tagged as ai workload"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("cloud_spend","Cloud Compute Spend","cost","currency","USD","derived",
    "Total cloud infrastructure costs for AI workloads including GPU instances and storage.",
    "The sum of all cloud provider invoice line items tagged as AI workloads, representing the largest single cost driver for AI operations.",
    "sum",
    related_facts=["cloud_invoice_amount","workload_tag","total_ai_spend"],
    example_values=[6400000],
    possible_aliases=["cloud spend","cloud compute cost","ai cloud cost","gpu compute spend"],
    extraction_patterns=["cloud compute spend","ai cloud infrastructure cost","gpu instance spend"],
    missing_value_strategy="use_last_known")

add("api_licensing_spend","API Licensing Spend","cost","currency","USD","derived",
    "Total spend on AI API access and model licensing from commercial providers.",
    "The sum of vendor invoices categorized as API licensing spend, covering commercial LLM/API access fees (e.g., OpenAI, Anthropic).",
    "sum",
    related_facts=["vendor_invoice_amount","spend_category","total_ai_spend"],
    example_values=[2900000],
    possible_aliases=["api licensing spend","model licensing cost","llm api cost","ai api spend"],
    extraction_patterns=["api licensing spend","model licensing fees","llm api subscription cost"],
    missing_value_strategy="use_last_known")

add("vendor_invoice_amount","Vendor Invoice Amount","cost","currency","USD","raw",
    "Individual vendor invoice amount, prior to spend category classification.",
    "The raw dollar amount on a vendor invoice, used as the base unit for spend categorization (e.g., api_licensing).",
    "sum",
    related_facts=["spend_category","api_licensing_spend"],
    example_values=[180000, 95000],
    possible_aliases=["vendor invoice amount","vendor bill amount","invoice line amount"],
    extraction_patterns=["vendor invoice amount","vendor bill line item"],
    missing_value_strategy="use_last_known")

add("spend_category","Spend Category","cost","enum","string","raw",
    "Classification of vendor invoice spend (e.g., api_licensing, cloud, storage, talent).",
    "A categorical label assigned to vendor invoices indicating the type of AI-related cost, used to segment total AI spend into sub-components.",
    "latest_value",
    related_facts=["vendor_invoice_amount","api_licensing_spend"],
    example_values=["api_licensing","cloud","storage","talent"],
    possible_aliases=["spend category","cost category","expense classification"],
    extraction_patterns=["spend category = api_licensing","categorized as api licensing"],
    validation_rules={"required_type":"enum","allowed_values":["api_licensing","cloud","storage","talent","other"]},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("storage_data_spend","Storage & Data Spend","cost","currency","USD","raw",
    "Total spend on data storage and data pipeline infrastructure for AI workloads.",
    "The portion of AI operating cost attributable to data storage, data lakes, and pipeline infrastructure supporting AI systems.",
    "sum",
    related_facts=["total_ai_spend","cloud_spend"],
    example_values=[3500000],
    possible_aliases=["storage and data spend","data infrastructure cost","data pipeline spend"],
    extraction_patterns=["storage and data spend","data infrastructure cost for ai"],
    missing_value_strategy="use_last_known")

add("talent_ai_teams_spend","AI Talent Teams Spend","cost","currency","USD","raw",
    "Total compensation and contractor spend for dedicated AI engineering and data science teams.",
    "The portion of AI operating cost attributable to salaries, benefits, and contractor fees for personnel dedicated to AI initiatives.",
    "sum",
    related_facts=["total_ai_spend"],
    example_values=[3000000],
    possible_aliases=["ai talent spend","ai team compensation cost","ai staffing cost"],
    extraction_patterns=["ai talent team spend","ai engineering team cost","dedicated ai staff cost"],
    missing_value_strategy="use_last_known")

add("approved_ai_budget","Approved AI Budget","cost","currency","USD","raw",
    "The board/finance-approved budget allocation for AI spend in the period.",
    "The officially approved budget figure for AI-related expenditure for the reporting period, against which actual spend is measured for adherence and variance.",
    "latest_value",
    related_facts=["total_ai_spend","budget_adherence","budget_variance"],
    example_values=[13000000, 13300000],
    possible_aliases=["approved ai budget","ai budget allocation","board-approved ai budget"],
    extraction_patterns=["approved ai budget","ai budget for the period","board-approved ai spend allocation"],
    missing_value_strategy="use_last_known")

add("committed_spend","Committed Spend","cost","currency","USD","raw",
    "Contractually committed AI spend for the remainder of the fiscal year.",
    "The portion of AI spend already committed via signed contracts or purchase commitments for the remainder of the fiscal year.",
    "sum",
    related_facts=["current_run_rate_spend","forecasted_ai_spend"],
    example_values=[14100000],
    possible_aliases=["committed spend","contracted ai spend","committed ai contracts"],
    extraction_patterns=["committed spend","contractually committed ai spend"],
    missing_value_strategy="use_last_known")

add("current_run_rate_spend","Current Run-Rate Spend","cost","currency","USD","raw",
    "Current monthly run-rate of AI spend, used to project forward spend.",
    "The most recent monthly AI spend figure, used as the basis for projecting future months' spend in the forecast calculation.",
    "latest_value",
    related_facts=["committed_spend","forecasted_ai_spend","remaining_months_in_year"],
    example_values=[4100000],
    possible_aliases=["current run-rate spend","monthly ai run rate","current monthly ai spend"],
    extraction_patterns=["current run-rate spend","monthly ai spend run rate"],
    missing_value_strategy="use_last_known")

add("remaining_months_in_year","Remaining Months In Year","cost","integer","count","raw",
    "Number of months remaining in the current fiscal year from the reporting period.",
    "A calendar-derived count of months remaining until fiscal year end, used to extrapolate run-rate spend into a full-year forecast.",
    "latest_value",
    related_facts=["current_run_rate_spend","forecasted_ai_spend"],
    example_values=[1, 3, 6],
    possible_aliases=["remaining months in year","months left in fiscal year"],
    extraction_patterns=["remaining months in fiscal year","months left in year"],
    validation_rules={"min":0,"max":12,"required_type":"integer"},
    normalization_rules={},
    missing_value_strategy="use_last_known")

add("model_family_spend","Model Family Spend","cost","currency","USD","raw",
    "AI spend broken down by model family (e.g., OpenAI, Anthropic, Llama, other open-source).",
    "The portion of total AI spend attributable to a specific model family or vendor, used to assess vendor concentration risk.",
    "sum",
    related_facts=["total_ai_spend","spend_by_model_family"],
    example_values=[7900000, 2300000],
    possible_aliases=["model family spend","spend by vendor model family","ai vendor spend breakdown"],
    extraction_patterns=["spend by model family","model family cost breakdown","openai spend","anthropic spend"],
    missing_value_strategy="use_last_known")

add("total_ai_successful_outcomes","Total AI Successful Outcomes","operations","integer","count","raw",
    "Count of meaningful business outcomes successfully delivered by AI systems in the period.",
    "The total count of business-meaningful outcomes (e.g., tickets resolved, sales assists completed) successfully delivered by AI systems, used as the denominator for unit-economics KPIs.",
    "sum",
    related_facts=["total_ai_spend","cost_per_outcome"],
    example_values=[30500000],
    possible_aliases=["total ai successful outcomes","ai outcomes delivered","successful ai-driven outcomes"],
    extraction_patterns=["total ai successful outcomes","ai-driven outcomes completed","outcomes resolved by ai"],
    confidence_critical=False,
    missing_value_strategy="exclude_portco")

# ============================ VALUE CREATION FACTS =========================
add("ai_revenue","AI Revenue","revenue","currency","USD","derived",
    "Total incremental revenue directly attributable to AI-driven products, features, and services.",
    "The portfolio-wide sum of direct AI revenue, AI-assisted revenue, and pipeline-influenced revenue, representing AI's total top-line contribution.",
    "sum",
    related_facts=["direct_ai_revenue","ai_assisted_revenue","pipeline_influenced_revenue","ai_roi","payback_period"],
    example_values=[39400000],
    possible_aliases=["ai revenue","ai generated revenue","revenue from ai","ai attributable revenue","ai sales"],
    extraction_patterns=["ai revenue","revenue generated by ai","attributable ai revenue","total ai revenue"],
    confidence_critical=True,
    missing_value_strategy="insufficient_data")

add("cost_savings","Cost Savings (OpEx)","cost","currency","USD","raw",
    "Realized reduction in operational expenses resulting from AI deployment vs pre-AI baseline.",
    "The dollar amount of operating expense reduction attributable to AI deployment, computed as the difference between pre-AI OpEx baseline and current-period OpEx.",
    "sum",
    related_facts=["opex_pre_ai_baseline","opex_current_period","ai_roi","payback_period"],
    example_values=[12800000],
    possible_aliases=["cost savings","ai cost savings","opex reduction from ai","ai-driven savings"],
    extraction_patterns=["cost savings from ai","opex reduction attributable to ai","ai-driven cost savings"],
    confidence_critical=True,
    missing_value_strategy="use_last_known")

add("opex_pre_ai_baseline","OpEx Pre-AI Baseline","cost","currency","USD","raw",
    "Operating expense baseline measured before AI deployment.",
    "The operating expense figure for the relevant cost center prior to AI deployment, used as the comparison baseline for cost savings calculations.",
    "latest_value",
    related_facts=["opex_current_period","cost_savings"],
    example_values=[50000000],
    possible_aliases=["pre-ai opex baseline","opex before ai","baseline operating expense"],
    extraction_patterns=["pre-ai opex baseline","operating expense before ai deployment"],
    missing_value_strategy="use_last_known")

add("opex_current_period","OpEx Current Period","cost","currency","USD","raw",
    "Operating expense for the current reporting period, post-AI deployment.",
    "The current-period operating expense figure for the relevant cost center, used to compute realized cost savings versus the pre-AI baseline.",
    "latest_value",
    related_facts=["opex_pre_ai_baseline","cost_savings"],
    example_values=[37200000],
    possible_aliases=["current opex","opex current period","post-ai operating expense"],
    extraction_patterns=["current period opex","operating expense this period","post-ai opex"],
    missing_value_strategy="use_last_known")

add("ebitda_post_ai","EBITDA Post-AI","financial","currency","USD","raw",
    "EBITDA figure for the period following AI-driven initiatives.",
    "The company's EBITDA for the current reporting period, reflecting the impact of AI-driven cost reduction and process efficiency initiatives.",
    "latest_value",
    related_facts=["ebitda_pre_ai","ebitda_delta","ebitda_uplift","revenue_base"],
    example_values=[25000000],
    possible_aliases=["ebitda post ai","current ebitda","post-ai ebitda"],
    extraction_patterns=["ebitda post-ai","ebitda after ai initiatives","current period ebitda"],
    confidence_critical=True,
    missing_value_strategy="insufficient_data")

add("ebitda_pre_ai","EBITDA Pre-AI","financial","currency","USD","raw",
    "EBITDA figure for the baseline period prior to AI-driven initiatives.",
    "The company's EBITDA for a prior baseline period before AI-driven initiatives took effect, used as the comparison point for EBITDA uplift.",
    "latest_value",
    related_facts=["ebitda_post_ai","ebitda_delta","ebitda_uplift","revenue_base"],
    example_values=[21000000],
    possible_aliases=["ebitda pre ai","baseline ebitda","pre-ai ebitda"],
    extraction_patterns=["ebitda pre-ai","ebitda before ai initiatives","baseline period ebitda"],
    confidence_critical=True,
    missing_value_strategy="insufficient_data")

add("ebitda_delta","EBITDA Delta","financial","currency","USD","derived",
    "Absolute dollar change in EBITDA between pre-AI and post-AI periods.",
    "The computed difference between post-AI EBITDA and pre-AI EBITDA, serving as the numerator for the EBITDA uplift percentage calculation.",
    "latest_value",
    related_facts=["ebitda_post_ai","ebitda_pre_ai","ebitda_uplift"],
    example_values=[4000000],
    possible_aliases=["ebitda delta","ebitda change","ebitda difference"],
    extraction_patterns=["ebitda delta","change in ebitda","ebitda difference pre vs post ai"],
    missing_value_strategy="insufficient_data")

add("revenue_base","Revenue Base","financial","currency","USD","raw",
    "Total revenue base used as the denominator for EBITDA uplift percentage calculation.",
    "The total revenue figure for the relevant period/entity, used as the normalization base when expressing EBITDA changes as a percentage of revenue.",
    "latest_value",
    related_facts=["ebitda_post_ai","ebitda_pre_ai","ebitda_uplift"],
    example_values=[100000000],
    possible_aliases=["revenue base","total revenue base","baseline revenue"],
    extraction_patterns=["revenue base","total company revenue for period"],
    missing_value_strategy="insufficient_data")

# ============================ ADOPTION / USAGE FACTS ========================
add("employee_id","Employee ID","hr","string","string","raw",
    "Unique identifier for an employee record used in adoption and HR analytics.",
    "A unique key identifying an individual employee, used to join HR records with telemetry usage data for adoption metrics.",
    "latest_value",
    related_facts=["weekly_ai_interactions","active_ai_users"],
    example_values=["EMP-10234","EMP-88291"],
    possible_aliases=["employee id","employee identifier","staff id"],
    extraction_patterns=["employee id","employee identifier"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("weekly_ai_interactions","Weekly AI Interactions","usage","integer","count","raw",
    "Count of an employee's meaningful AI tool interactions in a given week.",
    "The number of times an individual employee meaningfully interacted with AI tools (queries, completions, sessions) within a calendar week, used to assess adoption depth.",
    "sum",
    related_facts=["employee_id","active_ai_users","power_user_ratio"],
    example_values=[3, 22, 47],
    possible_aliases=["weekly ai interactions","weekly ai usage count","weekly ai tool sessions"],
    extraction_patterns=["weekly ai interactions","ai tool interactions per week","weekly ai usage count"],
    missing_value_strategy="use_last_known")

add("active_ai_users","Active AI Users","adoption","integer","count","derived",
    "Total employees with at least 5 meaningful AI tool interactions per week across portfolio.",
    "The count of employees meeting the minimum weekly engagement threshold (>=5 meaningful AI interactions), used as the primary measure of genuine workforce AI adoption.",
    "sum",
    related_facts=["weekly_ai_interactions","employee_id","power_user_ratio"],
    example_values=[2772],
    possible_aliases=["active ai users","active ai adoption count","weekly active ai users"],
    extraction_patterns=["active ai users","employees actively using ai weekly","weekly active ai user count"],
    missing_value_strategy="use_last_known")

add("copilot_tool_weekly_sessions","Copilot Tool Weekly Sessions","usage","integer","count","raw",
    "Count of an employee's weekly sessions using AI copilot tools (coding/writing assistants).",
    "The number of weekly sessions an employee has with AI copilot-type tools such as coding assistants or writing assistants, used to measure copilot adoption.",
    "sum",
    related_facts=["copilot_adoption"],
    example_values=[1, 5, 12],
    possible_aliases=["copilot weekly sessions","copilot tool usage sessions","weekly copilot interactions"],
    extraction_patterns=["copilot weekly sessions","weekly copilot tool usage","coding assistant sessions per week"],
    missing_value_strategy="use_last_known")

add("custom_agent_weekly_sessions","Custom Agent Weekly Sessions","usage","integer","count","raw",
    "Count of an employee's weekly sessions using custom-built AI agents.",
    "The number of weekly sessions an employee has with custom-built AI agents deployed internally by the PortCo, used to measure adoption of bespoke automation.",
    "sum",
    related_facts=["custom_agent_adoption"],
    example_values=[0, 3, 9],
    possible_aliases=["custom agent weekly sessions","custom ai agent usage sessions"],
    extraction_patterns=["custom agent weekly sessions","weekly custom ai agent usage"],
    missing_value_strategy="use_last_known")

add("embedded_analytics_weekly_sessions","Embedded Analytics Weekly Sessions","usage","integer","count","raw",
    "Count of an employee's weekly sessions using AI-powered embedded analytics features.",
    "The number of weekly sessions an employee has with AI-powered embedded analytics features within business applications, used to measure adoption of analytics AI.",
    "sum",
    related_facts=["embedded_analytics_adoption"],
    example_values=[0, 2, 6],
    possible_aliases=["embedded analytics weekly sessions","ai analytics feature usage sessions"],
    extraction_patterns=["embedded analytics weekly sessions","weekly ai analytics feature usage"],
    missing_value_strategy="use_last_known")

add("daily_ai_interactions","Daily AI Interactions","usage","integer","count","raw",
    "Count of an employee's meaningful AI tool interactions on a given day.",
    "The number of times an individual employee meaningfully interacted with AI production tools on a given calendar day, used to compute daily active AI users.",
    "sum",
    related_facts=["interaction_date","dau_mau_intensity"],
    example_values=[0, 4, 9],
    possible_aliases=["daily ai interactions","daily ai usage count","daily ai tool sessions"],
    extraction_patterns=["daily ai interactions","ai interactions per day","daily ai usage count"],
    missing_value_strategy="use_last_known")

add("interaction_date","Interaction Date","usage","date","date","raw",
    "Calendar date on which an AI interaction occurred.",
    "The calendar date associated with a recorded AI tool interaction, used to filter daily interaction counts to the current reporting date.",
    "latest_value",
    related_facts=["daily_ai_interactions","dau_mau_intensity"],
    example_values=["2026-06-11","2026-06-12"],
    possible_aliases=["interaction date","usage date","activity date"],
    extraction_patterns=["interaction date","date of ai usage"],
    validation_rules={"required_type":"date"},
    normalization_rules={"date_format":"YYYY-MM-DD"},
    missing_value_strategy="mark_unavailable")

# ============================ PROJECT PORTFOLIO FACTS =======================
add("project_id","Project ID","project","string","string","raw",
    "Unique identifier for an AI project record in the portfolio.",
    "A unique key identifying an individual AI initiative within the project portfolio, used to count and segment projects by stage and status.",
    "latest_value",
    related_facts=["project_stage","ai_flag","days_since_last_update"],
    example_values=["PRJ-2031","PRJ-4502"],
    possible_aliases=["project id","initiative id","ai project identifier"],
    extraction_patterns=["project id","ai initiative identifier"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("ai_flag","AI Flag","project","boolean","boolean","raw",
    "Boolean indicator of whether a project is classified as an AI initiative.",
    "A true/false flag applied to project portfolio records indicating whether the initiative is classified as an AI project, used to scope the total AI project count.",
    "latest_value",
    related_facts=["project_id","total_ai_projects"],
    example_values=[True, False],
    possible_aliases=["ai flag","ai project indicator","is ai project"],
    extraction_patterns=["ai flag = true","classified as ai project","ai project indicator"],
    validation_rules={"required_type":"boolean"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("project_stage","Project Stage","project","enum","string","raw",
    "Current pipeline stage of an AI project (e.g., poc, pilot, production, stalled, deprioritized).",
    "A categorical label indicating where in the delivery lifecycle an AI project currently sits, used to compute project portfolio KPIs such as production ratio and stalled project counts.",
    "latest_value",
    related_facts=["project_id","projects_in_production","projects_in_poc","stalled_projects","production_ratio","percent_ai_in_production"],
    example_values=["poc","pilot","production","stalled","deprioritized"],
    possible_aliases=["project stage","project status","initiative pipeline stage"],
    extraction_patterns=["project stage = production","project currently in pilot stage","stage: poc"],
    validation_rules={"required_type":"enum","allowed_values":["poc","pilot","production","stalled","deprioritized"]},
    normalization_rules={},
    confidence_critical=True,
    missing_value_strategy="use_last_known")

add("days_since_last_update","Days Since Last Update","project","integer","days","raw",
    "Number of days elapsed since an AI project record was last updated.",
    "The count of calendar days since the last activity or status update on an AI project record, used to identify stalled or deprioritized initiatives.",
    "latest_value",
    related_facts=["project_id","project_stage","stalled_projects"],
    example_values=[5, 35, 90],
    possible_aliases=["days since last update","days inactive","project staleness days"],
    extraction_patterns=["days since last update","days since last activity on project"],
    validation_rules={"min":0,"required_type":"integer"},
    normalization_rules={},
    missing_value_strategy="use_worst_case")

add("projects_in_production","Projects in Production","project","integer","count","derived",
    "Count of AI projects currently live in production environments.",
    "The number of AI projects whose project_stage equals 'production', representing initiatives that have reached value-generating deployment.",
    "sum",
    related_facts=["project_id","project_stage","percent_ai_in_production","production_ratio"],
    example_values=[53],
    possible_aliases=["projects in production","live ai projects","production-stage ai projects"],
    extraction_patterns=["projects in production","ai projects live in production","production deployments count"],
    missing_value_strategy="use_last_known")

add("projects_in_poc","Projects in PoC / Pilot","project","integer","count","derived",
    "Count of AI projects currently in proof-of-concept or pilot stage.",
    "The number of AI projects whose project_stage is 'poc' or 'pilot', representing the experimentation pipeline feeding future production deployments.",
    "sum",
    related_facts=["project_id","project_stage","production_ratio"],
    example_values=[12],
    possible_aliases=["projects in poc","pilot stage ai projects","proof of concept ai projects"],
    extraction_patterns=["projects in poc or pilot stage","pilot-stage ai projects count"],
    missing_value_strategy="use_last_known")

add("total_approved_ai_projects","Total Approved AI Projects","project","integer","count","raw",
    "Total count of AI use cases formally approved for development or deployment.",
    "The total number of AI use cases that have received formal approval (e.g., from governance committee), used as the denominator for the percent-in-production KPI.",
    "latest_value",
    related_facts=["projects_in_production","percent_ai_in_production"],
    example_values=[37],
    possible_aliases=["total approved ai projects","approved ai use cases","approved ai initiatives count"],
    extraction_patterns=["total approved ai projects","approved ai use cases count"],
    missing_value_strategy="exclude_portco")

# ============================ MATURITY / BENCHMARK RATING FACTS ============
add("portco_adoption_score","PortCo Adoption Score","maturity","float","score_0_100","raw",
    "Individual PortCo's AI adoption score on a 0-100 scale.",
    "A 0-100 composite score representing a single PortCo's depth and breadth of AI deployment, used as input to the revenue-weighted portfolio adoption score.",
    "latest_value",
    related_facts=["portco_revenue_weight","portfolio_ai_adoption_score"],
    example_values=[76, 62, 81],
    possible_aliases=["portco adoption score","company ai adoption score","individual portco adoption rating"],
    extraction_patterns=["portco adoption score","ai adoption score for portco"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("portco_revenue_weight","PortCo Revenue Weight","financial","float","weight","raw",
    "Revenue-based weighting factor for a PortCo used in portfolio-wide weighted averages.",
    "A weighting factor proportional to a PortCo's revenue contribution to the portfolio, used to compute revenue-weighted composite KPIs such as the portfolio AI adoption score.",
    "latest_value",
    related_facts=["portco_adoption_score","portfolio_ai_adoption_score"],
    example_values=[0.45, 0.30, 0.25],
    possible_aliases=["portco revenue weight","revenue weighting factor","portco weight by revenue"],
    extraction_patterns=["portco revenue weight","revenue-based weighting for portco"],
    validation_rules={"min":0,"max":1,"required_type":"float"},
    normalization_rules={},
    missing_value_strategy="exclude_portco")

add("portco_ai_maturity_score","PortCo AI Maturity Score","maturity","float","score_0_5","raw",
    "Individual PortCo's overall AI maturity score on a 0-5 scale.",
    "A 0-5 composite maturity score for an individual PortCo, combining strategic, technical, talent, and governance dimensions, used to rank PortCos within the portfolio.",
    "latest_value",
    related_facts=["company_maturity_rank","ai_maturity_score"],
    example_values=[4.8, 4.6, 2.8],
    possible_aliases=["portco ai maturity score","company ai maturity rating","individual portco maturity score"],
    extraction_patterns=["portco ai maturity score","ai maturity rating for portco"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("portco_governance_maturity_rating","PortCo Governance Maturity Rating","governance","float","score_0_5","raw",
    "Individual PortCo's governance maturity rating on a 0-5 scale.",
    "A 0-5 assessment rating of an individual PortCo's AI governance frameworks, policy completeness, and audit trail quality, used to compute the portfolio governance maturity score.",
    "latest_value",
    related_facts=["governance_maturity_score","ai_maturity_score"],
    example_values=[3.4, 4.0, 2.5],
    possible_aliases=["governance maturity rating","portco governance rating","ai governance maturity score per portco"],
    extraction_patterns=["governance maturity rating for portco","portco governance maturity assessment score"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("portco_strategic_alignment_rating","PortCo Strategic Alignment Rating","maturity","float","score_0_5","raw",
    "Individual PortCo's strategic alignment rating on a 0-5 scale.",
    "A 0-5 assessment rating of how well an individual PortCo's AI initiatives align with its core business strategy and goals, used to compute the portfolio strategic alignment score.",
    "latest_value",
    related_facts=["strategic_alignment_score","ai_maturity_score"],
    example_values=[4.1, 3.8, 2.9],
    possible_aliases=["strategic alignment rating","portco strategic alignment score","ai strategy alignment rating"],
    extraction_patterns=["strategic alignment rating for portco","portco ai strategic alignment assessment"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("portco_talent_readiness_rating","PortCo Talent Readiness Rating","hr","float","score_0_5","raw",
    "Individual PortCo's talent readiness rating on a 0-5 scale.",
    "A 0-5 assessment rating of an individual PortCo's workforce AI skill levels, training completion, and AI talent density, used to compute the portfolio talent readiness score.",
    "latest_value",
    related_facts=["talent_readiness_score","ai_maturity_score"],
    example_values=[3.3, 3.9, 2.2],
    possible_aliases=["talent readiness rating","portco talent readiness score","ai talent maturity rating"],
    extraction_patterns=["talent readiness rating for portco","portco ai talent readiness assessment"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("portco_technical_maturity_rating","PortCo Technical Maturity Rating","maturity","float","score_0_5","raw",
    "Individual PortCo's technical maturity rating on a 0-5 scale.",
    "A 0-5 assessment rating of an individual PortCo's MLOps rigor, data infrastructure quality, and AI engineering practices, used to compute the portfolio technical maturity score.",
    "latest_value",
    related_facts=["technical_maturity_score","ai_maturity_score"],
    example_values=[2.8, 3.5, 4.0],
    possible_aliases=["technical maturity rating","portco technical maturity score","mlops maturity rating"],
    extraction_patterns=["technical maturity rating for portco","portco mlops and engineering maturity assessment"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("portco_regulatory_readiness_rating","PortCo Regulatory Readiness Rating","governance","float","score_0_100","raw",
    "Individual PortCo's regulatory readiness rating on a 0-100 scale.",
    "A 0-100 assessment rating of an individual PortCo's preparedness for current and upcoming AI regulations, used to compute the portfolio regulatory readiness score.",
    "latest_value",
    related_facts=["regulatory_readiness","ai_governance_score"],
    example_values=[68, 75, 55],
    possible_aliases=["regulatory readiness rating","portco regulatory readiness score","ai regulation preparedness rating"],
    extraction_patterns=["regulatory readiness rating for portco","portco ai regulation preparedness assessment"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="use_worst_case")

# ============================ MATURITY COMPOSITE FACTS ======================
add("strategic_alignment_score","Strategic Alignment Score","maturity","float","score_0_5","derived",
    "Portfolio-wide average of how well AI initiatives align with PortCo business strategy.",
    "The portfolio-wide average of individual PortCo strategic alignment ratings, used as one of four inputs to the AI Maturity Score.",
    "average",
    related_facts=["portco_strategic_alignment_rating","ai_maturity_score"],
    example_values=[4.1],
    possible_aliases=["strategic alignment score","ai strategy alignment score","portfolio strategic alignment"],
    extraction_patterns=["strategic alignment score","portfolio average strategic alignment"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("technical_maturity_score","Technical Maturity Score","maturity","float","score_0_5","derived",
    "Portfolio-wide average assessment of MLOps rigor, data infrastructure quality, and AI engineering practices.",
    "The portfolio-wide average of individual PortCo technical maturity ratings, used as one of four inputs to the AI Maturity Score.",
    "average",
    related_facts=["portco_technical_maturity_rating","ai_maturity_score"],
    example_values=[2.8],
    possible_aliases=["technical maturity score","mlops maturity score","portfolio technical maturity"],
    extraction_patterns=["technical maturity score","portfolio average technical maturity"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("talent_readiness_score","Talent Readiness Score","maturity","float","score_0_5","derived",
    "Portfolio-wide average assessment of workforce AI skill levels, training completion, and AI talent density.",
    "The portfolio-wide average of individual PortCo talent readiness ratings, used as one of four inputs to the AI Maturity Score.",
    "average",
    related_facts=["portco_talent_readiness_rating","ai_maturity_score"],
    example_values=[3.3],
    possible_aliases=["talent readiness score","ai talent maturity score","portfolio talent readiness"],
    extraction_patterns=["talent readiness score","portfolio average talent readiness"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

add("governance_maturity_score","Governance Maturity Score","maturity","float","score_0_5","derived",
    "Portfolio-wide average assessment of AI governance frameworks, policy completeness, and audit trail quality.",
    "The portfolio-wide average of individual PortCo governance maturity ratings, used as one of four inputs to the AI Maturity Score and feeding governance benchmarking.",
    "average",
    related_facts=["portco_governance_maturity_rating","ai_maturity_score"],
    example_values=[3.4],
    possible_aliases=["governance maturity score","ai governance maturity score","portfolio governance maturity"],
    extraction_patterns=["governance maturity score","portfolio average governance maturity"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

# ============================ GOVERNANCE FACTS ===============================
add("data_privacy_compliance","Data Privacy Compliance","governance","float","score_0_100","derived",
    "Compliance score measuring adherence to data privacy regulations (GDPR, CCPA) in AI data pipelines.",
    "A 0-100 score representing the percentage of AI data pipelines that meet data privacy regulatory requirements (e.g., GDPR, CCPA), used as input to the AI Governance Score.",
    "average",
    related_facts=["compliant_data_pipelines","total_data_pipelines","ai_governance_score"],
    example_values=[76],
    possible_aliases=["data privacy compliance score","gdpr/ccpa compliance score","privacy compliance rating"],
    extraction_patterns=["data privacy compliance score","gdpr ccpa compliance rate for ai pipelines"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="use_worst_case")

add("compliant_data_pipelines","Compliant Data Pipelines","governance","integer","count","raw",
    "Count of AI data pipelines that meet data privacy regulatory requirements.",
    "The number of AI data pipelines audited and confirmed compliant with applicable data privacy regulations, used as the numerator for the data privacy compliance score.",
    "sum",
    related_facts=["total_data_pipelines","data_privacy_compliance"],
    example_values=[76],
    possible_aliases=["compliant data pipelines","privacy-compliant pipelines count","compliant ai pipelines"],
    extraction_patterns=["compliant data pipelines count","data pipelines meeting privacy requirements"],
    missing_value_strategy="use_worst_case")

add("total_data_pipelines","Total Data Pipelines","governance","integer","count","raw",
    "Total count of AI data pipelines subject to privacy compliance audit.",
    "The total number of AI data pipelines in scope for the privacy compliance audit, used as the denominator for the data privacy compliance score.",
    "sum",
    related_facts=["compliant_data_pipelines","data_privacy_compliance"],
    example_values=[100],
    possible_aliases=["total data pipelines","total ai data pipelines audited","data pipeline count"],
    extraction_patterns=["total data pipelines","total ai data pipelines in scope"],
    missing_value_strategy="use_worst_case")

add("model_bias_risk_score","Model Bias Risk Score","governance","float","score_0_100","raw",
    "Composite score measuring the risk of bias in AI model outputs.",
    "A 0-100 score representing the assessed risk level of bias in AI model outputs across the portfolio, used as an input to the AI Governance Score.",
    "average",
    related_facts=["ai_governance_score"],
    example_values=[71],
    possible_aliases=["model bias risk score","ai bias risk rating","bias risk assessment score"],
    extraction_patterns=["model bias risk score","ai model bias risk assessment"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="use_worst_case")

add("regulatory_readiness","Regulatory Readiness","governance","float","score_0_100","derived",
    "Portfolio-wide average assessment of preparedness for current and upcoming AI regulations.",
    "The portfolio-wide average of individual PortCo regulatory readiness ratings, used as an input to the AI Governance Score.",
    "average",
    related_facts=["portco_regulatory_readiness_rating","ai_governance_score"],
    example_values=[68],
    possible_aliases=["regulatory readiness score","ai regulation preparedness score","portfolio regulatory readiness"],
    extraction_patterns=["regulatory readiness score","portfolio average regulatory readiness"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="use_worst_case")

add("ai_governance_score","AI Governance Score","governance","float","score_0_100","derived",
    "Composite risk metric evaluating policy adherence, audit readiness, and model risk management.",
    "A weighted composite of data privacy compliance, model bias risk, and regulatory readiness, providing PE-level governance assurance and used as input to the governance benchmark rank.",
    "weighted_average",
    related_facts=["data_privacy_compliance","model_bias_risk_score","regulatory_readiness","governance_rank"],
    example_values=[72.1],
    possible_aliases=["ai governance score","governance risk composite score","ai compliance composite score"],
    extraction_patterns=["ai governance score","governance composite risk score"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    confidence_critical=True,
    missing_value_strategy="use_worst_case")

# ============================ INCIDENT / RELIABILITY FACTS ===================
add("incident_id","Incident ID","governance","string","string","raw",
    "Unique identifier for an AI incident record.",
    "A unique key identifying a recorded AI incident (failure, policy violation, or service degradation), used to join incident severity, status, and resolution data.",
    "latest_value",
    related_facts=["incident_severity","incident_status","incident_period","incident_resolution_time_minutes","incident_type"],
    example_values=["INC-7781","INC-9023"],
    possible_aliases=["incident id","incident identifier","ai incident record id"],
    extraction_patterns=["incident id","ai incident identifier"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("incident_severity","Incident Severity","governance","enum","string","raw",
    "Severity classification of an AI incident (e.g., critical, high, medium, low).",
    "A categorical severity rating assigned to an AI incident, used to filter incident counts for the AI Incident Rate and Critical Incident Count KPIs.",
    "latest_value",
    related_facts=["incident_id","ai_incident_rate","critical_incident_count"],
    example_values=["critical","high","medium","low"],
    possible_aliases=["incident severity","severity level","incident criticality"],
    extraction_patterns=["incident severity = critical","severity rated as high","critical severity incident"],
    validation_rules={"required_type":"enum","allowed_values":["critical","high","medium","low"]},
    normalization_rules={},
    confidence_critical=True,
    missing_value_strategy="use_worst_case")

add("incident_period","Incident Period","governance","date","date","raw",
    "Reporting period (typically month) in which an AI incident occurred.",
    "The calendar period (month) during which an AI incident occurred, used to filter incident counts to the current reporting month for the AI Incident Rate KPI.",
    "latest_value",
    related_facts=["incident_id","ai_incident_rate"],
    example_values=["2026-05","2026-06"],
    possible_aliases=["incident period","incident month","period of occurrence"],
    extraction_patterns=["incident occurred in period","incident period = current month"],
    validation_rules={"required_type":"date"},
    normalization_rules={"date_format":"YYYY-MM"},
    missing_value_strategy="use_worst_case")

add("incident_status","Incident Status","governance","enum","string","raw",
    "Resolution status of an AI incident (e.g., open, in_progress, resolved).",
    "A categorical status indicating whether an AI incident is open, in progress, or resolved, used to filter the Critical Incident Count to unresolved incidents.",
    "latest_value",
    related_facts=["incident_id","critical_incident_count"],
    example_values=["open","in_progress","resolved"],
    possible_aliases=["incident status","resolution status","incident state"],
    extraction_patterns=["incident status = resolved","incident still open","unresolved incident status"],
    validation_rules={"required_type":"enum","allowed_values":["open","in_progress","resolved"]},
    normalization_rules={},
    confidence_critical=True,
    missing_value_strategy="use_worst_case")

add("incident_resolution_time_minutes","Incident Resolution Time (Minutes)","reliability","float","minutes","raw",
    "Time in minutes taken to resolve an AI service degradation incident.",
    "The elapsed time, in minutes, between detection and resolution of an AI service degradation incident, used to compute the MTTR KPI.",
    "average",
    related_facts=["incident_id","incident_type","mttr"],
    example_values=[24, 12, 45],
    possible_aliases=["incident resolution time","time to resolve incident","mttr minutes"],
    extraction_patterns=["incident resolution time in minutes","time to recover from incident"],
    validation_rules={"min":0,"required_type":"float"},
    missing_value_strategy="use_worst_case")

add("incident_type","Incident Type","reliability","enum","string","raw",
    "Classification of an incident's type (e.g., ai_service_degradation, data_breach).",
    "A categorical label classifying the nature of an incident, used to filter incidents to AI service degradation events for the MTTR calculation.",
    "latest_value",
    related_facts=["incident_id","incident_resolution_time_minutes","mttr"],
    example_values=["ai_service_degradation","data_breach","model_drift"],
    possible_aliases=["incident type","incident category","incident classification"],
    extraction_patterns=["incident type = ai_service_degradation","classified as service degradation incident"],
    validation_rules={"required_type":"enum","allowed_values":["ai_service_degradation","data_breach","model_drift","policy_violation","other"]},
    normalization_rules={},
    missing_value_strategy="use_worst_case")

# ============================ TELEMETRY / RELIABILITY FACTS ==================
add("api_call_id","API Call ID","telemetry","string","string","raw",
    "Unique identifier for an individual AI API call record.",
    "A unique key identifying a single AI API request/response event, used to join status code data for reliability KPIs.",
    "latest_value",
    related_facts=["api_status_code","api_success_rate","error_rate"],
    example_values=["call-9981234"],
    possible_aliases=["api call id","api request identifier","api call record id"],
    extraction_patterns=["api call id","api request identifier"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("api_status_code","API Status Code","telemetry","integer","code","raw",
    "HTTP status code returned by an AI API call.",
    "The HTTP status code returned for an individual AI API call, used to compute API success rate and error rate KPIs (e.g., codes >=500 indicate errors).",
    "latest_value",
    related_facts=["api_call_id","api_success_rate","error_rate"],
    example_values=[200, 429, 500, 503],
    possible_aliases=["api status code","http response code","api response status"],
    extraction_patterns=["api status code","http status code for api call","response status code"],
    validation_rules={"min":100,"max":599,"required_type":"integer"},
    normalization_rules={},
    confidence_critical=True,
    missing_value_strategy="use_worst_case")

add("api_response_time_ms","API Response Time (ms)","telemetry","float","milliseconds","raw",
    "Response time in milliseconds for an individual AI API/LLM inference request.",
    "The end-to-end latency, in milliseconds, for a consumer-facing LLM inference request, used to compute the P95 Latency KPI.",
    "average",
    related_facts=["p95_latency"],
    example_values=[420, 1100, 2300],
    possible_aliases=["api response time","llm inference latency","response time milliseconds"],
    extraction_patterns=["api response time in milliseconds","llm inference latency ms"],
    validation_rules={"min":0,"required_type":"float"},
    missing_value_strategy="use_worst_case")

add("total_minutes_in_period","Total Minutes in Period","telemetry","integer","minutes","raw",
    "Total number of minutes in the reporting period, used as the denominator for uptime calculation.",
    "The total elapsed minutes within the reporting period (e.g., 43,800 minutes in a 30-day month), used as the denominator for the System Availability KPI.",
    "latest_value",
    related_facts=["total_downtime_minutes","availability_uptime"],
    example_values=[43800, 44640],
    possible_aliases=["total minutes in period","total period minutes","reporting period minutes"],
    extraction_patterns=["total minutes in the reporting period","period duration in minutes"],
    validation_rules={"min":0,"required_type":"integer"},
    normalization_rules={},
    missing_value_strategy="use_worst_case")

add("total_downtime_minutes","Total Downtime Minutes","telemetry","float","minutes","raw",
    "Total minutes of downtime across critical AI-driven infrastructure endpoints in the period.",
    "The total accumulated minutes of unplanned downtime across critical AI infrastructure endpoints during the reporting period, used to compute the System Availability (Uptime) KPI.",
    "sum",
    related_facts=["total_minutes_in_period","availability_uptime"],
    example_values=[4.3, 12.5],
    possible_aliases=["total downtime minutes","ai system downtime","unplanned downtime minutes"],
    extraction_patterns=["total downtime minutes","ai infrastructure downtime in minutes"],
    validation_rules={"min":0,"required_type":"float"},
    confidence_critical=True,
    missing_value_strategy="use_worst_case")

add("request_id","Request ID","telemetry","string","string","raw",
    "Unique identifier for an individual AI request record.",
    "A unique key identifying a single AI request, used to join fallback-triggered status for the Fallback Rate KPI.",
    "latest_value",
    related_facts=["fallback_triggered","fallback_rate"],
    example_values=["req-558821"],
    possible_aliases=["request id","ai request identifier"],
    extraction_patterns=["request id","ai request identifier"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("fallback_triggered","Fallback Triggered","telemetry","boolean","boolean","raw",
    "Boolean indicator of whether an AI request triggered a fallback to a secondary system or human escalation.",
    "A true/false flag indicating whether a given AI request fell back to a secondary model or human escalation due to primary model failure, used to compute the Fallback Rate KPI.",
    "average",
    related_facts=["request_id","fallback_rate"],
    example_values=[True, False],
    possible_aliases=["fallback triggered","fallback flag","secondary system fallback indicator"],
    extraction_patterns=["fallback triggered = true","request fell back to secondary system","human escalation triggered"],
    validation_rules={"required_type":"boolean"},
    normalization_rules={},
    missing_value_strategy="use_worst_case")

add("evaluated_output_id","Evaluated Output ID","telemetry","string","string","raw",
    "Unique identifier for an AI output record subjected to hallucination evaluation.",
    "A unique key identifying an individual AI model output that has been evaluated for hallucination, used to join hallucination flag data for the Hallucination Rate KPI.",
    "latest_value",
    related_facts=["hallucination_flag","hallucination_rate"],
    example_values=["out-44123"],
    possible_aliases=["evaluated output id","ai output identifier","hallucination evaluation record id"],
    extraction_patterns=["evaluated output id","ai output identifier for hallucination check"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("hallucination_flag","Hallucination Flag","governance","boolean","boolean","raw",
    "Boolean indicator of whether an AI output was flagged as factually incorrect or fabricated.",
    "A true/false flag indicating whether an AI model output was flagged by automated detection systems as factually incorrect or fabricated, used to compute the Hallucination Rate KPI.",
    "average",
    related_facts=["evaluated_output_id","hallucination_rate"],
    example_values=[True, False],
    possible_aliases=["hallucination flag","hallucination detected indicator","factual error flag"],
    extraction_patterns=["hallucination flag = true","output flagged as hallucination","factually incorrect output detected"],
    validation_rules={"required_type":"boolean"},
    normalization_rules={},
    confidence_critical=True,
    missing_value_strategy="use_worst_case")

add("output_id","Output ID","governance","string","string","raw",
    "Unique identifier for an AI output record subjected to human review evaluation.",
    "A unique key identifying an individual AI model output, used to join human review completion and criticality data for the Human Review Coverage KPI.",
    "latest_value",
    related_facts=["human_review_completed","output_criticality","human_review_coverage"],
    example_values=["out-77821"],
    possible_aliases=["output id","ai output identifier","review record id"],
    extraction_patterns=["output id","ai output identifier for review"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("human_review_completed","Human Review Completed","governance","boolean","boolean","raw",
    "Boolean indicator of whether a high-criticality AI output underwent human-in-the-loop review.",
    "A true/false flag indicating whether a given AI output was subjected to human-in-the-loop review, used to compute the Human Review Coverage KPI for high-criticality outputs.",
    "average",
    related_facts=["output_id","output_criticality","human_review_coverage"],
    example_values=[True, False],
    possible_aliases=["human review completed","reviewed by human flag","human-in-the-loop review status"],
    extraction_patterns=["human review completed = true","output reviewed by human","human-in-the-loop audit completed"],
    validation_rules={"required_type":"boolean"},
    normalization_rules={},
    missing_value_strategy="use_worst_case")

add("output_criticality","Output Criticality","governance","enum","string","raw",
    "Criticality classification of an AI output (e.g., high, medium, low).",
    "A categorical criticality rating assigned to an AI output indicating its potential business or legal impact, used to filter outputs for the Human Review Coverage KPI denominator.",
    "latest_value",
    related_facts=["output_id","human_review_completed","human_review_coverage"],
    example_values=["high","medium","low"],
    possible_aliases=["output criticality","decision criticality level","ai output impact rating"],
    extraction_patterns=["output criticality = high","high-criticality ai decision","criticality rating of output"],
    validation_rules={"required_type":"enum","allowed_values":["high","medium","low"]},
    normalization_rules={},
    missing_value_strategy="use_worst_case")

# ============================ COMPLIANCE / POLICY FACTS ======================
add("compliant_entities","Compliant Entities","governance","integer","count","raw",
    "Count of employees and vendors adhering to the Global AI Acceptable Use Policy.",
    "The number of employees and vendors confirmed compliant with the Global AI Acceptable Use Policy, used as the numerator for the Policy Compliance Rate KPI.",
    "sum",
    related_facts=["total_entities","policy_compliance_rate"],
    example_values=[9800],
    possible_aliases=["compliant entities","policy-compliant employees and vendors","ai policy compliant count"],
    extraction_patterns=["compliant entities count","employees and vendors compliant with ai policy"],
    missing_value_strategy="use_worst_case")

add("total_entities","Total Entities","governance","integer","count","raw",
    "Total count of employees and vendors subject to the Global AI Acceptable Use Policy.",
    "The total number of employees and vendors in scope of the Global AI Acceptable Use Policy, used as the denominator for the Policy Compliance Rate KPI.",
    "sum",
    related_facts=["compliant_entities","policy_compliance_rate"],
    example_values=[10000],
    possible_aliases=["total entities","total employees and vendors in scope","ai policy population count"],
    extraction_patterns=["total entities subject to ai policy","total employees and vendors in scope"],
    missing_value_strategy="use_worst_case")

add("vendor_id","Vendor ID","governance","string","string","raw",
    "Unique identifier for a third-party AI vendor record.",
    "A unique key identifying a third-party AI vendor, used to join compliance status data for the Vendor Compliance KPI.",
    "latest_value",
    related_facts=["compliance_status","vendor_compliance"],
    example_values=["VND-1029"],
    possible_aliases=["vendor id","third-party vendor identifier","ai vendor record id"],
    extraction_patterns=["vendor id","third-party ai vendor identifier"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="mark_unavailable")

add("compliance_status","Compliance Status","governance","enum","string","raw",
    "Compliance status of a third-party AI vendor (e.g., compliant, non_compliant, pending_review).",
    "A categorical status indicating whether a third-party AI vendor meets minimum PE-defined security and compliance requirements, used to compute the Vendor Compliance KPI.",
    "latest_value",
    related_facts=["vendor_id","vendor_compliance"],
    example_values=["compliant","non_compliant","pending_review"],
    possible_aliases=["compliance status","vendor compliance state","vendor security compliance status"],
    extraction_patterns=["compliance status = compliant","vendor marked non-compliant","vendor compliance review status"],
    validation_rules={"required_type":"enum","allowed_values":["compliant","non_compliant","pending_review"]},
    normalization_rules={},
    missing_value_strategy="use_worst_case")

# ============================ BENCHMARK FACTS =================================
add("portfolio_adoption_velocity","Portfolio Adoption Velocity","benchmark","float","score","raw",
    "Measure of the portfolio's speed of AI adoption, used for percentile ranking against industry peers.",
    "A composite measure of how quickly the portfolio is adopting AI relative to its own historical baseline, used as the numerator input for the Adoption Velocity Rank percentile calculation.",
    "latest_value",
    related_facts=["benchmark_adoption_velocity_distribution","adoption_velocity_rank"],
    example_values=[92],
    possible_aliases=["portfolio adoption velocity","ai adoption speed score","portfolio adoption pace"],
    extraction_patterns=["portfolio adoption velocity","ai adoption speed score for portfolio"],
    missing_value_strategy="use_last_known")

add("benchmark_adoption_velocity_distribution","Benchmark Adoption Velocity Distribution","benchmark","string","distribution","raw",
    "Distribution of AI adoption velocity scores across the PE benchmark panel.",
    "A statistical distribution of AI adoption velocity scores from the PE benchmark panel, used as the comparison set for computing the portfolio's adoption velocity percentile rank.",
    "latest_value",
    related_facts=["portfolio_adoption_velocity","adoption_velocity_rank"],
    example_values=["benchmark_panel_2026_q2"],
    possible_aliases=["benchmark adoption velocity distribution","peer adoption velocity distribution","industry adoption velocity benchmark"],
    extraction_patterns=["benchmark adoption velocity distribution","peer panel adoption velocity data"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="use_last_known")

add("portfolio_cost_per_outcome","Portfolio Cost Per Outcome","benchmark","currency","USD","raw",
    "Portfolio-wide cost per AI outcome, used for percentile ranking against industry peers.",
    "The portfolio-wide aggregate cost-per-outcome figure, used as the numerator input for the Cost Efficiency Rank percentile calculation against the PE benchmark panel.",
    "latest_value",
    related_facts=["benchmark_cost_efficiency_distribution","cost_efficiency_rank","cost_per_outcome"],
    example_values=[0.42],
    possible_aliases=["portfolio cost per outcome","portfolio unit economics ai cost","portfolio cost efficiency metric"],
    extraction_patterns=["portfolio cost per outcome","portfolio-wide ai unit cost"],
    missing_value_strategy="use_last_known")

add("benchmark_cost_efficiency_distribution","Benchmark Cost Efficiency Distribution","benchmark","string","distribution","raw",
    "Distribution of AI cost efficiency metrics across the PE benchmark panel.",
    "A statistical distribution of AI cost-per-outcome metrics from the PE benchmark panel, used as the comparison set for computing the portfolio's cost efficiency percentile rank.",
    "latest_value",
    related_facts=["portfolio_cost_per_outcome","cost_efficiency_rank"],
    example_values=["benchmark_panel_2026_q2"],
    possible_aliases=["benchmark cost efficiency distribution","peer cost efficiency distribution","industry cost efficiency benchmark"],
    extraction_patterns=["benchmark cost efficiency distribution","peer panel cost efficiency data"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="use_last_known")

add("ai_governance_score","AI Governance Score","governance","float","score_0_100","derived",
    "Composite risk metric evaluating policy adherence, audit readiness, and model risk management.",
    "A weighted composite of data privacy compliance, model bias risk, and regulatory readiness, providing PE-level governance assurance and used as input to the governance benchmark rank.",
    "weighted_average",
    related_facts=["data_privacy_compliance","model_bias_risk_score","regulatory_readiness","governance_rank"],
    example_values=[72.1],
    possible_aliases=["ai governance score","governance risk composite score","ai compliance composite score"],
    extraction_patterns=["ai governance score","governance composite risk score"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    confidence_critical=True,
    missing_value_strategy="use_worst_case")

add("benchmark_governance_distribution","Benchmark Governance Distribution","benchmark","string","distribution","raw",
    "Distribution of AI governance scores across the PE benchmark panel.",
    "A statistical distribution of AI governance scores from the PE benchmark panel, used as the comparison set for computing the portfolio's governance percentile rank.",
    "latest_value",
    related_facts=["ai_governance_score","governance_rank"],
    example_values=["benchmark_panel_2026_q2"],
    possible_aliases=["benchmark governance distribution","peer governance score distribution","industry governance benchmark"],
    extraction_patterns=["benchmark governance distribution","peer panel governance score data"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="use_last_known")

add("portfolio_avg_roi","Portfolio Average ROI","benchmark","float","ratio","raw",
    "Portfolio-wide average AI ROI, used for comparison against industry median ROI.",
    "The portfolio-wide average AI ROI ratio, used as the numerator for the Industry Benchmark Ratio KPI comparing portfolio performance to industry median.",
    "average",
    related_facts=["industry_median_roi","industry_benchmark_ratio","ai_roi"],
    example_values=[3.9],
    possible_aliases=["portfolio average roi","portfolio-wide ai roi","average ai roi across portfolio"],
    extraction_patterns=["portfolio average ai roi","portfolio-wide roi figure"],
    missing_value_strategy="use_last_known")

add("industry_median_roi","Industry Median ROI","benchmark","float","ratio","raw",
    "Median AI ROI across the PE benchmark panel of industry peers.",
    "The median AI ROI ratio observed across the PE benchmark panel of industry peers, used as the denominator for the Industry Benchmark Ratio KPI.",
    "latest_value",
    related_facts=["portfolio_avg_roi","industry_benchmark_ratio"],
    example_values=[2.4],
    possible_aliases=["industry median roi","peer median ai roi","benchmark panel median roi"],
    extraction_patterns=["industry median ai roi","peer panel median roi figure"],
    missing_value_strategy="use_last_known")

add("portfolio_avg_maturity_score","Portfolio Average Maturity Score","benchmark","float","score","raw",
    "Portfolio-wide average AI maturity score, used for percentile ranking against industry benchmark.",
    "The portfolio-wide average AI maturity score, used as the numerator input for the Portfolio Benchmark Score percentile calculation against the industry benchmark distribution.",
    "average",
    related_facts=["benchmark_distribution","portfolio_benchmark_score","ai_maturity_score"],
    example_values=[3.4],
    possible_aliases=["portfolio average maturity score","portfolio-wide ai maturity score","average ai maturity across portfolio"],
    extraction_patterns=["portfolio average ai maturity score","portfolio-wide maturity figure"],
    missing_value_strategy="use_last_known")

add("benchmark_distribution","Benchmark Distribution","benchmark","string","distribution","raw",
    "Distribution of AI maturity scores across industry-matched PE firm benchmarks.",
    "A statistical distribution of AI maturity scores from industry-matched PE firm benchmarks, used as the comparison set for computing the Portfolio Benchmark Score percentile.",
    "latest_value",
    related_facts=["portfolio_avg_maturity_score","portfolio_benchmark_score"],
    example_values=["benchmark_panel_2026_q2"],
    possible_aliases=["benchmark distribution","industry maturity benchmark distribution","peer maturity score distribution"],
    extraction_patterns=["benchmark distribution data","industry-matched pe firm benchmark distribution"],
    validation_rules={"required_type":"string"},
    normalization_rules={},
    missing_value_strategy="use_last_known")

add("portco_industry_maturity_percentile","PortCo Industry Maturity Percentile","benchmark","float","percentile","raw",
    "Individual PortCo's AI maturity percentile ranking within the industry benchmark.",
    "The percentile ranking (0-100) of an individual PortCo's AI maturity relative to the industry benchmark, used to compute the Top Quartile Position KPI.",
    "latest_value",
    related_facts=["top_quartile_position"],
    example_values=[82, 45, 91],
    possible_aliases=["portco industry maturity percentile","company maturity percentile rank","individual portco benchmark percentile"],
    extraction_patterns=["portco industry maturity percentile","company-level maturity percentile vs industry"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="exclude_portco")

# ============================ ADOPTION SCORE TIME SERIES FACTS ===============
add("adoption_score_current_year","Adoption Score (Current Year)","adoption","float","score_0_100","raw",
    "Portfolio-wide AI adoption score for the current year.",
    "The portfolio-wide composite AI adoption maturity score measured for the current year, used as the numerator input for the Adoption YoY Growth KPI.",
    "latest_value",
    related_facts=["adoption_score_prior_year","adoption_yoy_growth"],
    example_values=[71],
    possible_aliases=["adoption score current year","current year ai adoption score","this year's adoption score"],
    extraction_patterns=["adoption score for current year","current year ai adoption maturity score"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="insufficient_data")

add("adoption_score_prior_year","Adoption Score (Prior Year)","adoption","float","score_0_100","raw",
    "Portfolio-wide AI adoption score for the prior year.",
    "The portfolio-wide composite AI adoption maturity score measured for the prior year, used as the baseline denominator for the Adoption YoY Growth KPI.",
    "latest_value",
    related_facts=["adoption_score_current_year","adoption_yoy_growth"],
    example_values=[55],
    possible_aliases=["adoption score prior year","previous year ai adoption score","last year's adoption score"],
    extraction_patterns=["adoption score for prior year","previous year ai adoption maturity score"],
    validation_rules={"min":0,"max":100,"required_type":"float"},
    missing_value_strategy="insufficient_data")

# ============================ DERIVED COMPOSITE / OTHER ======================
add("monthly_ai_return","Monthly AI Return","financial","currency","USD","derived",
    "Average monthly financial return generated by AI initiatives.",
    "The combined AI revenue and cost savings divided by 12, representing the average monthly financial return used to compute the AI Payback Period KPI.",
    "average",
    related_facts=["ai_revenue","cost_savings","payback_period"],
    example_values=[1170000],
    possible_aliases=["monthly ai return","average monthly ai financial return","monthly return on ai investment"],
    extraction_patterns=["monthly ai return","average monthly return from ai initiatives"],
    missing_value_strategy="insufficient_data")

add("ai_roi","AI ROI","financial","float","ratio","derived",
    "Total financial return generated per dollar of AI investment across the portfolio.",
    "The ratio of combined AI revenue and cost savings to total AI spend, representing the portfolio-wide financial return on AI investment, used as an input to Industry Benchmark Ratio.",
    "portfolio_weighted_average",
    related_facts=["ai_revenue","cost_savings","total_ai_spend","portfolio_avg_roi","industry_benchmark_ratio","payback_period"],
    example_values=[7.0, 3.9],
    possible_aliases=["ai roi","return on ai investment","ai investment return ratio"],
    extraction_patterns=["ai roi","return on ai investment ratio"],
    validation_rules={"min":0,"required_type":"float"},
    missing_value_strategy="insufficient_data")

# Note: ai_maturity_score, ai_governance_score, etc are referenced as both
# "required_facts" of one KPI and the KPI itself of another -> add remaining
add("ai_maturity_score","AI Maturity Score","maturity","float","score_0_5","derived",
    "Cross-portfolio assessment of AI engineering culture, data readiness, and governance capability on a 5-point scale.",
    "The average of strategic alignment, technical maturity, talent readiness, and governance maturity scores, providing a composite view of long-term AI capability and risk, used to compute Company Maturity Rank.",
    "average",
    related_facts=["strategic_alignment_score","technical_maturity_score","talent_readiness_score","governance_maturity_score","portco_ai_maturity_score","portfolio_avg_maturity_score"],
    example_values=[3.4],
    possible_aliases=["ai maturity score","portfolio ai maturity score","cross-portfolio ai capability score"],
    extraction_patterns=["ai maturity score","cross-portfolio ai maturity assessment"],
    validation_rules={"min":0,"max":5,"required_type":"float"},
    missing_value_strategy="exclude_portco")

print(f"Total fact definitions: {len(F)}")

from pathlib import Path
import yaml
import os

OUTPUT_DIR = 'backend/data/fact_registry'
os.makedirs(OUTPUT_DIR, exist_ok=True)

for fact_id, fact_data in F.items():
    file_path = os.path.join(OUTPUT_DIR , f"{fact_id}.yaml") 

    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump(
            fact_data,
            f,
            sort_keys=False,
            default_flow_style=False,
            allow_unicode=True,
            width=120
        )

print(f"Generated {len(F)} fact YAML files in {OUTPUT_DIR}")


# # Validation: every fact referenced in facts_map must be in F
# missing = [f for f in facts_map if f not in F]
# print("Missing fact definitions:", missing)

# import pickle
# with open('/home/claude/F.pkl','wb') as fo:
#     pickle.dump(F, fo)
