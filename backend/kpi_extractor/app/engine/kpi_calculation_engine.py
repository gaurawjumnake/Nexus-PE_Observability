"""
KPI Calculation Engine  (Stage 8 + 9 + 10)
=============================================
100% deterministic. No LLM. No agents.

Workflow per KPI:
    1. retrieve_formula_context(kpi_id) via MCP
       → get formula string + required fact_ids
    2. Check fact availability from Postgres (coverage check)
    3. If coverage meets threshold → evaluate formula via safe AST engine
    4. Save result to KPI store

Formula engine:
    Supports arithmetic expressions over fact_id variable names, e.g.:
        (ai_revenue + cost_savings) / total_ai_spend
    Aggregation-style formulas (count, percentile_rank, sum WHERE ...) are
    pre-resolved at the fact level (derived facts) and appear as simple
    variables here.
"""

import ast
import operator
from typing import Optional
from backend.kpi_extractor.app.registry.registry_service import RegistryService

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
}

_AGGREGATION_MARKERS = [
    "count(", "sum(", "average(", "where ", "over(",
    "percentile_rank(", "rank(", "filter(", "select ",
]


class KPICalculationEngine:
    """
    Deterministic KPI calculation engine.

    Usage:
        engine = KPICalculationEngine(registry)
        results = engine.calculate_all(company_id, period, fact_store_fn, save_fn)
    """

    def __init__(self, registry: RegistryService):
        self.registry = registry

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def calculate_all(
        self,
        company_id: str,
        period: str,
        get_facts_fn,   # callable(company_id, period) -> {fact_id: value}
        save_kpi_fn,    # callable(kpi_id, company_id, value, coverage, status, period)
        kpi_ids: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Calculate all (or a subset of) KPIs for a company/period.

        get_facts_fn  : returns all validated fact values for this company+period
        save_kpi_fn   : persists each KPI result to Postgres
        kpi_ids       : if provided, calculate only these KPIs (default: all)
        """
        fact_values = get_facts_fn(company_id, period)
        results = []

        ids_to_run = kpi_ids or self._all_kpi_ids()
        for kpi_id in ids_to_run:
            result = self._calculate_one(kpi_id, company_id, period, fact_values)
            save_kpi_fn(
                result["kpi_id"], company_id,
                result["value"], result["coverage"],
                result["status"], period,
            )
            results.append(result)

        return results

    def calculate_one(
        self,
        kpi_id: str,
        company_id: str,
        period: str,
        get_facts_fn,
        save_kpi_fn,
    ) -> dict:
        fact_values = get_facts_fn(company_id, period)
        result = self._calculate_one(kpi_id, company_id, period, fact_values)
        save_kpi_fn(
            result["kpi_id"], company_id,
            result["value"], result["coverage"],
            result["status"], period,
        )
        return result

    def calculate_trend(
        self,
        kpi_id: str,
        company_id: str,
        periods: list[str],
        get_facts_fn,
        save_kpi_fn=None,
    ) -> list[dict]:
        """
        Calculates one KPI across a list of periods - this is what
        powers MoM / QoQ / YoY: the same formula, evaluated once per
        period bucket, each pulling its facts independently (so a
        period with no underlying data just comes back insufficient_data
        rather than reusing a neighboring period's value).

        periods: e.g. ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4"] for
        QoQ, or ["2025-01", ..., "2025-12"] for MoM. Build this list with
        app.engine.fact_aggregation.enumerate_periods().

        get_facts_fn: same contract as calculate_one - callable(company_id,
        period) -> {fact_id: value}. Pass
        fact_aggregation.get_facts_for_company_period (bound to a
        registry) to make this aggregation-aware.

        save_kpi_fn is optional here (unlike calculate_one) since a trend
        is often requested read-only for a dashboard chart without
        wanting to persist every bucket - pass it if you do want each
        period's result saved.

        Returns one result dict per period, in the same order as
        `periods`, each carrying its own "period" key.
        """
        results = []
        for period in periods:
            fact_values = get_facts_fn(company_id, period)
            result = self._calculate_one(kpi_id, company_id, period, fact_values)
            result["period"] = period
            if save_kpi_fn is not None:
                save_kpi_fn(
                    result["kpi_id"], company_id,
                    result["value"], result["coverage"],
                    result["status"], period,
                )
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _all_kpi_ids(self) -> list[str]:
        hits = self.registry.search_registry("kpi", limit=500)
        return [h["entity_id"] for h in hits if h["entity_type"] == "kpi"]

    def _calculate_one(
        self, kpi_id: str, company_id: str, period: str, fact_values: dict
    ) -> dict:
        # Step 1: fetch formula context
        ctx = self.registry.retrieve_formula_context(kpi_id)
        if ctx is None:
            return _result(kpi_id, None, 0.0, "kpi_not_found")

        required_facts = list(ctx["facts"].keys())
        formula = ctx.get("formula", "")

        # Step 2: coverage check
        available = {fid: fact_values[fid] for fid in required_facts if fid in fact_values}
        coverage = len(available) / len(required_facts) if required_facts else 1.0

        if not required_facts or coverage < 1.0:
            # Short-circuit: if the KPI ID itself is a directly-observed fact, use it.
            # This handles cases where e.g. total_ai_spend is both a KPI (computed from
            # sub-components) and a directly-measured column in the uploaded data.
            if kpi_id in fact_values:
                try:
                    direct_val = float(fact_values[kpi_id])
                    return _result(kpi_id, direct_val, 1.0, "calculated_direct")
                except (ValueError, TypeError):
                    pass
            return _result(kpi_id, None, coverage, "insufficient_data",
                           missing=[f for f in required_facts if f not in available])

        # Step 3: formula evaluation
        if _is_aggregation_formula(formula):
            # Aggregation formulas may be pre-computed as derived facts keyed by kpi_id.
            # First look in full fact_values (catches pre-computed row aggregates like
            # api_success_rate). Fall back to first numeric value in the required-fact
            # subset, but skip non-numeric strings (e.g. call IDs) to prevent garbage.
            derived_id = kpi_id.replace("_kpi", "")
            value = fact_values.get(derived_id) or fact_values.get(kpi_id)
            if value is None:
                for v in available.values():
                    try:
                        value = float(v)
                        break
                    except (ValueError, TypeError):
                        pass
            if value is None:
                return _result(kpi_id, None, coverage, "insufficient_data")
            try:
                final_val = float(value)
            except (ValueError, TypeError):
                final_val = value
            return _result(kpi_id, final_val, coverage, "calculated_derived")

        try:
            value = _eval_formula(formula, available)
            return _result(kpi_id, value, coverage, "calculated")
        except ZeroDivisionError:
            return _result(kpi_id, None, coverage, "division_by_zero")
        except Exception as e:
            return _result(kpi_id, None, coverage, f"error: {e}")


# ------------------------------------------------------------------
# Formula evaluation (pure Python AST)
# ------------------------------------------------------------------
def _is_aggregation_formula(formula: str) -> bool:
    f = formula.lower()
    return any(m in f for m in _AGGREGATION_MARKERS)


def _eval_formula(formula: str, variables: dict[str, float]) -> float:
    tree = ast.parse(formula.strip(), mode="eval")
    return float(_eval_node(tree.body, variables))


def _eval_node(node, variables: dict):
    if isinstance(node, ast.BinOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise TypeError(f"Unsupported operator: {type(node.op).__name__}")
        return op(_eval_node(node.left, variables), _eval_node(node.right, variables))

    if isinstance(node, ast.UnaryOp):
        op = _OPS.get(type(node.op))
        if op is None:
            raise TypeError(f"Unsupported unary operator")
        return op(_eval_node(node.operand, variables))

    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in ("abs", "round", "min", "max"):
            args = [_eval_node(a, variables) for a in node.args]
            return {"abs": abs, "round": round, "min": min, "max": max}[node.func.id](*args)
        raise TypeError(f"Unsupported function call: {getattr(node.func, 'id', '?')}")

    if isinstance(node, ast.Name):
        if node.id not in variables:
            raise KeyError(f"Fact not available: {node.id}")
        v = variables[node.id]
        # unwrap JSONB dict from Postgres if needed
        if isinstance(v, dict):
            v = next(iter(v.values()))
        try:
            return float(v)
        except (ValueError, TypeError):
            return v

    if isinstance(node, ast.Constant):
        return float(node.value)

    raise TypeError(f"Unsupported AST node: {type(node).__name__}")


def _result(
    kpi_id: str, value, coverage: float, status: str, missing: list = None
) -> dict:
    r = {"kpi_id": kpi_id, "kpi": kpi_id, "value": value, "coverage": coverage, "status": status}
    if missing:
        r["missing_facts"] = missing
    return r
