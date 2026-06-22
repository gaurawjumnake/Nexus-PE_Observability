"""
Fact Aggregation Engine  (Stage 7.5)
=======================================
100% deterministic. No LLM. No agents.

Bridges two fact storage shapes into one "value for this fact, this
company, this period" lookup that KPICalculationEngine can consume
without caring where the data actually came from:

  1. fact_observations - raw dated rows (daily/weekly/monthly/whatever
     granularity the source gave us), e.g. four years of daily telemetry.
     Aggregated on demand into the requested period using the fact's own
     aggregation_strategy from the registry (sum / average / latest_value).

  2. facts - a single value already stated for one exact period (e.g. a
     narrative doc stating "Q2 2026 AI ROI: 7.0"). Used only as a
     fallback for facts that have no observations at all - this keeps
     the original point-in-time ingestion path (Novamind-style docs)
     working completely unchanged.

Never fabricates a value. If neither source has anything for the
requested period - and, for latest_value facts, there's no earlier
observation to carry forward - the fact is simply absent from the
result. KPICalculationEngine already treats a missing fact as reduced
coverage / insufficient_data, so no special-casing is needed downstream.
"""

import calendar
import re
from datetime import date
from typing import Optional

import backend.db.db_client as db

_QUARTER_RE = re.compile(r"^(\d{4})-Q([1-4])$")
_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR_RE = re.compile(r"^(\d{4})$")

# aggregation_strategy values that should be treated as a plain average -
# "weighted"/"portfolio_weighted" variants have no per-observation weight
# data at the single-fact level, so we degrade gracefully to an
# unweighted average rather than fabricate weights or crash.
_AVERAGE_LIKE = {"average", "weighted_average", "portfolio_weighted_average"}


# ------------------------------------------------------------------
# Period <-> date range
# ------------------------------------------------------------------
def resolve_period_range(period: str) -> tuple[str, str]:
    """
    Resolves a period string to an inclusive (start_date, end_date) ISO
    'YYYY-MM-DD' range.

    Supported formats:
        'YYYY'        e.g. '2026'      -> full calendar year
        'YYYY-Qn'     e.g. '2026-Q2'   -> calendar quarter (n = 1..4)
        'YYYY-MM'     e.g. '2026-06'   -> calendar month

    Raises ValueError for anything else - callers should treat an
    unparseable period as a usage error, not silently skip it.
    """
    m = _QUARTER_RE.match(period)
    if m:
        year, q = int(m.group(1)), int(m.group(2))
        start_month = (q - 1) * 3 + 1
        end_month = start_month + 2
        start = date(year, start_month, 1)
        end = date(year, end_month, calendar.monthrange(year, end_month)[1])
        return start.isoformat(), end.isoformat()

    m = _MONTH_RE.match(period)
    if m:
        year, month = int(m.group(1)), int(m.group(2))
        start = date(year, month, 1)
        end = date(year, month, calendar.monthrange(year, month)[1])
        return start.isoformat(), end.isoformat()

    m = _YEAR_RE.match(period)
    if m:
        year = int(m.group(1))
        return date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat()

    raise ValueError(
        f"Unrecognized period format: {period!r}. Expected 'YYYY', 'YYYY-MM', or 'YYYY-Qn'."
    )


def enumerate_periods(period_type: str, start_period: str, end_period: str) -> list[str]:
    """
    Ordered list of period strings between start_period and end_period
    inclusive, for building MoM / QoQ / YoY series.

    period_type: 'month' | 'quarter' | 'year'
    start_period / end_period must already be in that granularity's
    format (e.g. period_type='quarter' -> '2025-Q1').
    """
    if period_type == "year":
        start_year = int(_YEAR_RE.match(start_period).group(1)) #type:ignore
        end_year = int(_YEAR_RE.match(end_period).group(1)) #type:ignore
        return [str(y) for y in range(start_year, end_year + 1)]

    if period_type == "quarter":
        sy, sq = (int(g) for g in _QUARTER_RE.match(start_period).groups()) #type:ignore
        ey, eq = (int(g) for g in _QUARTER_RE.match(end_period).groups()) #type:ignore
        periods = []
        y, q = sy, sq
        while (y, q) <= (ey, eq):
            periods.append(f"{y}-Q{q}")
            q += 1
            if q > 4:
                q = 1
                y += 1
        return periods

    if period_type == "month":
        sy, sm = (int(g) for g in _MONTH_RE.match(start_period).groups()) #type:ignore
        ey, em = (int(g) for g in _MONTH_RE.match(end_period).groups()) #type:ignore
        periods = []
        y, m = sy, sm
        while (y, m) <= (ey, em):
            periods.append(f"{y}-{m:02d}")
            m += 1
            if m > 12:
                m = 1
                y += 1
        return periods

    raise ValueError(f"Unknown period_type: {period_type!r}. Expected 'month', 'quarter', or 'year'.")


_ROW_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def derive_periods_from_tables(time_series_tables: list[dict], granularity: str = "year") -> list[str]:
    """
    Scans every row's date across all `time_series_tables` (the dated
    tables DoclingDocumentParser produces, the same shape consumed by
    tabular_extraction.extract_observations_from_time_series_table) and
    returns the sorted list of distinct period strings the data
    actually covers, at the requested granularity.

    This exists so the upload router can run KPI calculation against
    periods the data really contains, instead of trusting a single
    typed/Form `period` value that may not match - e.g. a daily-dated
    table spanning all of 2025 should be recalculated for period
    '2025', regardless of what string was typed into the upload form.

    granularity: 'year' | 'quarter' | 'month' - same granularities
    resolve_period_range() understands, so the returned strings are
    valid `period` arguments for calculate_kpis()/calculate_all().

    Returns [] if no table has any parseable row date - callers should
    fall back to the caller-supplied period in that case (e.g. a flat
    structured_table-only upload with no date column at all).
    """
    periods = set()
    for table in time_series_tables or []:
        date_col = table.get("date_column", "date")
        for row in table.get("rows", []):
            raw_date = row.get("date") or row.get(date_col)
            if not raw_date:
                continue
            m = _ROW_DATE_RE.match(str(raw_date))
            if not m:
                continue
            year, month = m.group(1), int(m.group(2))
            if granularity == "year":
                periods.add(year)
            elif granularity == "month":
                periods.add(f"{year}-{month:02d}")
            elif granularity == "quarter":
                periods.add(f"{year}-Q{(month - 1) // 3 + 1}")
            else:
                raise ValueError(f"Unknown granularity: {granularity!r}. Expected 'year', 'quarter', or 'month'.")

    return sorted(periods)


# ------------------------------------------------------------------
# Per-fact aggregation
# ------------------------------------------------------------------
def _as_number(value):
    """Best-effort numeric coercion for sum/average. Returns None (not 0)
    for values that genuinely aren't numeric, so callers can skip them
    rather than silently corrupting a sum with a zero that was never
    really there."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ok(value, as_of_date: Optional[str] = None, carried_forward: bool = False) -> dict:
    return {
        "status": "carried_forward" if carried_forward else "observed",
        "value": value,
        "as_of_date": as_of_date,
        "carried_forward": carried_forward,
    }


def _insufficient() -> dict:
    return {"status": "insufficient_data", "value": None, "as_of_date": None, "carried_forward": False}


def aggregate_fact_for_period(
    registry, fact_id: str, company_id: str, period: str,
    start_date: Optional[str] = None, end_date: Optional[str] = None,
) -> dict:
    """
    Aggregates one fact's raw observations into a single value for the
    given period, using that fact's aggregation_strategy from the
    registry. Returns:

        {"status": "observed" | "carried_forward" | "insufficient_data",
         "value": <number or None>,
         "as_of_date": <ISO date the value is actually from, or None>,
         "carried_forward": bool}

    start_date/end_date can be passed pre-resolved to avoid re-parsing
    `period` repeatedly when aggregating many facts for the same period.
    """
    if start_date is None or end_date is None:
        start_date, end_date = resolve_period_range(period)

    fact_ctx = registry.get_fact_context(fact_id)
    strategy = (fact_ctx or {}).get("aggregation_strategy") or "latest_value"

    observations = db.get_observations_in_range(fact_id, company_id, start_date, end_date)

    if strategy == "sum":
        nums = [n for n in (_as_number(o["value"]) for o in observations) if n is not None]
        if not nums:
            return _insufficient()
        return _ok(sum(nums))

    if strategy in _AVERAGE_LIKE:
        nums = [n for n in (_as_number(o["value"]) for o in observations) if n is not None]
        if not nums:
            return _insufficient()
        return _ok(sum(nums) / len(nums))

    # default + explicit "latest_value": most recent observation within
    # the period; if none, carry forward the most recent observation
    # from before the period (flagged), rather than insufficient_data.
    if observations:
        latest = observations[-1]
        value = _as_number(latest["value"])
        if value is None:
            value = latest["value"]  # non-numeric latest_value facts (e.g. enums) pass through as-is
        return _ok(value, as_of_date=latest["observation_date"])

    carried = db.get_latest_observation_on_or_before(fact_id, company_id, end_date)
    if carried is not None:
        value = _as_number(carried["value"])
        if value is None:
            value = carried["value"]
        return _ok(value, as_of_date=carried["observation_date"], carried_forward=True)

    return _insufficient()


# ------------------------------------------------------------------
# Public entry point: drop-in replacement for db.get_facts_for_company
# ------------------------------------------------------------------
def _compute_row_aggregates(company_id: str, start_date: str, end_date: str) -> dict:
    """
    Computes count-where KPIs that require cross-row logic over raw
    fact_observations: api_success_rate, error_rate, fallback_rate,
    hallucination_rate. Returns a partial {kpi_id: value} dict that
    get_facts_for_company_period merges into the main values dict.
    """
    result = {}

    status_obs = db.get_observations_in_range("api_status_code", company_id, start_date, end_date)
    if status_obs:
        total = len(status_obs)
        success = sum(1 for o in status_obs if (_as_number(o["value"]) or 0) < 500)
        result["api_success_rate"] = round(success / total * 100, 2)
        result["error_rate"] = round((total - success) / total * 100, 2)

    fallback_obs = db.get_observations_in_range("fallback_triggered", company_id, start_date, end_date)
    if fallback_obs:
        total = len(fallback_obs)
        triggered = sum(1 for o in fallback_obs if str(o["value"]).lower() == "true")
        result["fallback_rate"] = round(triggered / total * 100, 2)

    halluc_obs = db.get_observations_in_range("hallucination_flag", company_id, start_date, end_date)
    if halluc_obs:
        total = len(halluc_obs)
        flagged = sum(1 for o in halluc_obs if str(o["value"]).lower() == "true")
        result["hallucination_rate"] = round(flagged / total * 100, 2)

    return result


def get_facts_for_company_period(registry, company_id: str, period: str) -> dict:
    """
    Returns {fact_id: value} for a company+period, the same shape
    KPICalculationEngine already expects from get_facts_fn - pass this
    (bound to a registry instance) instead of db.get_facts_for_company
    to make KPI calculation aggregation-aware.

    Combines both sources:
      - point-in-time facts (`facts` table) - unchanged, exact period match
      - time-series facts (`fact_observations`) - aggregated to this period
        per-fact using aggregation_strategy; overrides the point-in-time
        value if a fact has data in both places, since observations are
        the more granular source.

    A fact with no data anywhere for this period (in either source, and
    no carry-forward candidate for latest_value facts) is simply absent
    from the returned dict - never filled in with a guess.
    """
    values = dict(db.get_facts_for_company(company_id, period))

    start_date, end_date = resolve_period_range(period)
    for fact_id in db.get_distinct_observed_facts(company_id):
        result = aggregate_fact_for_period(registry, fact_id, company_id, period, start_date, end_date)
        if result["status"] != "insufficient_data":
            values[fact_id] = result["value"]

    values.update(_compute_row_aggregates(company_id, start_date, end_date))

    return values


def get_facts_for_company_period_detailed(registry, company_id: str, period: str) -> tuple[dict, dict]:
    """
    Like get_facts_for_company_period, but also returns a parallel
    metadata dict {fact_id: {"status", "as_of_date", "carried_forward"}}
    for every time-series fact considered - useful for surfacing
    "this number is carried forward from March" in the UI instead of
    presenting it as a fresh observation.
    """
    values = dict(db.get_facts_for_company(company_id, period))
    meta: dict = {}

    start_date, end_date = resolve_period_range(period)
    for fact_id in db.get_distinct_observed_facts(company_id):
        result = aggregate_fact_for_period(registry, fact_id, company_id, period, start_date, end_date)
        meta[fact_id] = {
            "status": result["status"],
            "as_of_date": result["as_of_date"],
            "carried_forward": result["carried_forward"],
        }
        if result["status"] != "insufficient_data":
            values[fact_id] = result["value"]

    return values, meta