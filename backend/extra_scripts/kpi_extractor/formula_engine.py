"""
Formula Engine: Python coding based

Evaluates KPI formulas using fact values from the Fact Store.
Formulas reference fact_ids as variable names (e.g.
"(ai_revenue + cost_savings) / total_ai_spend").

Uses a restricted eval - only fact values + safe math operators allowed.
"""

import ast
import operator
from typing import Dict, Optional
from backend.utilites.registry_loader import RegistryCache


_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.Pow: operator.pow,
}


def calculate_kpi(kpi_id: str, fact_values: Dict[str, float], registry: RegistryCache) -> Optional[Dict]:
    """
    fact_values: {fact_id: numeric_value} for facts required by this KPI
                  (already resolved by the Dependency Resolver / Fact Store)

    Returns: {"kpi_id", "value", "status"} or None if formula not evaluable
             with the current registry definition (e.g. complex SQL-style
             formula not expressible as arithmetic - handled separately).
    """
    kpi = registry.kpis[kpi_id]
    formula = kpi.formula

    expr = _formula_to_expression(formula)
    if expr is None:
        return {"kpi_id": kpi_id, "value": None, "status": "unsupported_formula"}

    try:
        tree = ast.parse(expr, mode="eval")
        value = _eval_node(tree.body, fact_values)
    except (KeyError, ZeroDivisionError, SyntaxError, TypeError):
        return {"kpi_id": kpi_id, "value": None, "status": "calculation_error"}

    return {"kpi_id": kpi_id, "value": value, "status": "calculated"}


def _formula_to_expression(formula: str) -> Optional[str]:
    """
    Many registry formulas are simple arithmetic over fact_ids, e.g.:
        "(ai_revenue + cost_savings) / total_ai_spend"
    These are evaluable directly.

    Formulas using SQL-style constructs (count(...), sum(...) WHERE ...,
    percentile_rank(...), rank(...) OVER (...)) are aggregation formulas
    that must be pre-computed at the fact level (the aggregated result IS
    the fact, e.g. `total_ai_spend`). Such formulas are flagged as
    unsupported for the arithmetic engine and resolved earlier in the
    pipeline (fact_type=derived facts already hold the aggregated value).
    """
    unsupported_markers = ["count(", "sum(", "average(", "percentile_rank(",
                            "rank(", "WHERE", "OVER", "percentile("]
    if any(marker in formula for marker in unsupported_markers):
        return None
    return formula


def _eval_node(node, fact_values: Dict[str, float]):
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise TypeError(f"Unsupported operator: {type(node.op)}")
        return op(_eval_node(node.left, fact_values), _eval_node(node.right, fact_values))

    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise TypeError(f"Unsupported operator: {type(node.op)}")
        return op(_eval_node(node.operand, fact_values))

    if isinstance(node, ast.Name):
        if node.id not in fact_values:
            raise KeyError(node.id)
        return fact_values[node.id]

    if isinstance(node, ast.Constant):
        return node.value

    raise TypeError(f"Unsupported expression node: {type(node)}")
