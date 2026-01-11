# win.py
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple, Optional


@dataclass
class ConditionResult:
    label: str
    ok: bool
    current: Any
    target: Any
    op: str


def _get_value(metric_key: str, country_metrics: Dict[str, Any], eu_state: Dict[str, Any]) -> Any:
    """
    Supported metric keys:
    - country metrics: military, stability, economy, diplomatic_influence, public_approval
    - eu: eu_cohesion
    """
    if metric_key == "eu_cohesion":
        return int(eu_state.get("cohesion", 0))

    if metric_key in country_metrics:
        return country_metrics[metric_key]

    raise KeyError(f"Unknown metric_key: {metric_key}")


def _compare(current: Any, op: str, target: Any) -> bool:
    if op == ">=":
        return current >= target
    if op == "<=":
        return current <= target
    if op == ">":
        return current > target
    if op == "<":
        return current < target
    if op == "==":
        return current == target
    raise ValueError(f"Unsupported operator: {op}")


def evaluate_country_win_conditions(
    country_key: str,
    *,
    country_metrics: Dict[str, Any],
    eu_state: Dict[str, Any],
    country_defs: Dict[str, Dict[str, Any]],
) -> Tuple[bool, List[ConditionResult]]:
    """
    Returns: (is_winner, condition_results)

    Expects country_defs[country_key]["win_conditions"] like:
    [
      {"metric": "economy", "op": ">=", "value": 90, "label": "Wirtschaft hoch"},
      {"metric": "eu_cohesion", "op": ">=", "value": 75, "label": "EU-Kohäsion stabil"},
    ]
    label is optional; we auto-generate a readable label if missing.
    """
    defs = country_defs.get(country_key, {})
    conds = defs.get("win_conditions") or []

    results: List[ConditionResult] = []
    for c in conds:
        metric = c["metric"]
        op = c.get("op", ">=")
        target = c["value"]
        label = c.get("label") or f"{metric} {op} {target}"

        current = _get_value(metric, country_metrics, eu_state)
        ok = _compare(current, op, target)

        results.append(
            ConditionResult(
                label=label,
                ok=ok,
                current=current,
                target=target,
                op=op,
            )
        )

    # Wenn keine Bedingungen definiert sind, nie automatisch gewinnen (explizit definieren!)
    is_winner = bool(results) and all(r.ok for r in results)
    return is_winner, results


def evaluate_all_countries(
    *,
    all_country_metrics: Dict[str, Dict[str, Any]],
    eu_state: Dict[str, Any],
    country_defs: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    """
    Returns dict:
    {
      "Germany": {"is_winner": bool, "results": [ConditionResult,...]},
      ...
    }
    """
    out: Dict[str, Dict[str, Any]] = {}
    for country_key, metrics in all_country_metrics.items():
        is_winner, results = evaluate_country_win_conditions(
            country_key,
            country_metrics=metrics,
            eu_state=eu_state,
            country_defs=country_defs,
        )
        out[country_key] = {"is_winner": is_winner, "results": results}
    return out
