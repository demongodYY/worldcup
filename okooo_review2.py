"""Review-2 Asian-handicap ROI/Kelly pricing helpers.

This module is intentionally independent from the dashboard and CLI layers so
both can use the same net-goal distribution and buy-rule implementation.
"""

from __future__ import annotations

import math
from typing import Any

from asian_handicap_validation import split_asian_line


SETTLED_OUTCOMES = ("full_win", "half_win", "push", "half_loss", "full_loss")


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def line_value(value: Any) -> float:
    text = str(value or "0").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def dewater_probabilities(odds: list[float]) -> list[float] | None:
    inverse = [1.0 / odd for odd in odds if odd and odd > 0]
    if len(inverse) != 3:
        return None
    total = sum(inverse)
    if total <= 0:
        return None
    return [value / total for value in inverse]


def kelly_light_probabilities(raw: dict[str, Any], base: list[float]) -> list[float]:
    kelly = [
        as_float(raw.get("KellyHome")),
        as_float(raw.get("KellyDraw")),
        as_float(raw.get("KellyAway")),
    ]
    if any(value <= 0 for value in kelly):
        return base
    corrected = [base[index] / kelly[index] for index in range(3)]
    corrected_total = sum(corrected)
    if corrected_total <= 0:
        return base
    corrected = [value / corrected_total for value in corrected]
    blended = [0.75 * base[index] + 0.25 * corrected[index] for index in range(3)]
    blended_total = sum(blended)
    return [value / blended_total for value in blended] if blended_total > 0 else base


def model_probabilities(raw: dict[str, Any], fallback: list[float]) -> list[float]:
    values = [
        raw.get("_okooo_probability_home", raw.get("_okooo_euro_prob_home")),
        raw.get("_okooo_probability_draw", raw.get("_okooo_euro_prob_draw")),
        raw.get("_okooo_probability_away", raw.get("_okooo_euro_prob_away")),
    ]
    probs = [as_float(value) / 100.0 for value in values]
    total = sum(probs)
    if total <= 0:
        return fallback
    return [value / total for value in probs]


def settlement_return(selected_handicap: float, decimal_odds: float, selected_margin: float) -> float:
    legs = split_asian_line(selected_handicap)
    total = 0.0
    for leg in legs:
        margin = selected_margin + leg
        if margin > 1e-9:
            outcome = decimal_odds - 1.0
        elif margin < -1e-9:
            outcome = -1.0
        else:
            outcome = 0.0
        total += outcome / len(legs)
    return total


def margin_distribution(probabilities: list[float], home_line: float) -> tuple[dict[int, float], float, float]:
    home_prob, draw_prob, away_prob = probabilities
    advantage = math.log((home_prob + 1e-6) / (away_prob + 1e-6))
    home_depth = max(0.0, -home_line)
    away_depth = max(0.0, home_line)
    home_mean = max(1.05, min(4.0, 1.08 + 0.42 * home_depth + 0.28 * max(0.0, advantage)))
    away_mean = max(1.05, min(4.0, 1.08 + 0.42 * away_depth + 0.28 * max(0.0, -advantage)))

    def truncated_geometric_weights(mean: float, size: int = 8) -> list[float]:
        q = max(0.05, min(0.78, 1.0 - 1.0 / mean))
        weights = [(1.0 - q) * (q ** (goal - 1)) for goal in range(1, size)]
        weights.append(q ** (size - 1))
        total = sum(weights)
        return [weight / total for weight in weights]

    distribution: dict[int, float] = {0: draw_prob}
    for goals, weight in enumerate(truncated_geometric_weights(home_mean), start=1):
        distribution[goals] = home_prob * weight
    for goals, weight in enumerate(truncated_geometric_weights(away_mean), start=1):
        distribution[-goals] = away_prob * weight
    return distribution, home_mean, away_mean


def side_roi(probabilities: list[float], home_line: float, decimal_odds: float, side: str) -> float:
    distribution, _, _ = margin_distribution(probabilities, home_line)
    selected_handicap = home_line if side == "home" else -home_line
    total = 0.0
    for home_margin, probability in distribution.items():
        selected_margin = float(home_margin if side == "home" else -home_margin)
        total += probability * settlement_return(selected_handicap, decimal_odds, selected_margin)
    return total


def side_kelly(probabilities: list[float], home_line: float, decimal_odds: float, side: str) -> float:
    distribution, _, _ = margin_distribution(probabilities, home_line)
    selected_handicap = home_line if side == "home" else -home_line
    outcomes: list[tuple[float, float]] = []
    for home_margin, probability in distribution.items():
        selected_margin = float(home_margin if side == "home" else -home_margin)
        outcomes.append((probability, settlement_return(selected_handicap, decimal_odds, selected_margin)))
    expected = sum(probability * ret for probability, ret in outcomes)
    if expected <= 0:
        return 0.0
    high = 1.0
    for _, ret in outcomes:
        if ret < 0:
            high = min(high, -1.0 / ret)
    high = min(0.999999, high * 0.999999)

    def derivative(fraction: float) -> float:
        return sum(probability * ret / (1.0 + fraction * ret) for probability, ret in outcomes)

    if derivative(high) > 0:
        return high
    low = 0.0
    for _ in range(100):
        mid = (low + high) / 2.0
        if derivative(mid) > 0:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def team_side(team: Any, home: str, away: str) -> str | None:
    if str(team or "") == home:
        return "home"
    if str(team or "") == away:
        return "away"
    return None


def side_label(side: str, home: str, away: str, home_line: float) -> str:
    team = home if side == "home" else away
    handicap = home_line if side == "home" else -home_line
    return f"{team} {handicap:+.2f}"


def validate_record_is_bet(record: dict[str, Any]) -> bool:
    return (
        record.get("status") == "ok"
        and record.get("profit") is not None
        and record.get("outcome") not in ("missing", "excluded", "error", "na")
    )


def review2_pricing_from_context(
    record: dict[str, Any],
    *,
    match: dict[str, Any],
    raw: dict[str, Any],
    market: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    market = market or {}
    home = str(record.get("home") or match.get("home") or "")
    away = str(record.get("away") or match.get("away") or "")
    if not home or not away:
        return None
    home_line = line_value(record.get("asian_line") or match.get("asian_line") or "0")
    home_odds = as_float(market.get("home_decimal_odds") or raw.get("AsianAvrHome"))
    away_odds = as_float(market.get("away_decimal_odds") or raw.get("AsianAvrAway"))
    if home_odds <= 1 or away_odds <= 1:
        return None
    euro_base = dewater_probabilities(
        [
            as_float(raw.get("EuroAvrHome")),
            as_float(raw.get("EuroAvrDraw")),
            as_float(raw.get("EuroAvrAway")),
        ]
    )
    if euro_base is None:
        return None
    euro_kelly_prob = kelly_light_probabilities(raw, euro_base)
    model_prob = model_probabilities(raw, euro_kelly_prob)
    odds_by_side = {"home": home_odds, "away": away_odds}
    sides: dict[str, dict[str, Any]] = {}
    for side in ("home", "away"):
        euro_roi = side_roi(euro_kelly_prob, home_line, odds_by_side[side], side)
        model_roi = side_roi(model_prob, home_line, odds_by_side[side], side)
        sides[side] = {
            "label": side_label(side, home, away, home_line),
            "euro_roi": round(euro_roi, 4),
            "model_roi": round(model_roi, 4),
            "euro_kelly": round(side_kelly(euro_kelly_prob, home_line, odds_by_side[side], side), 4),
            "model_kelly": round(side_kelly(model_prob, home_line, odds_by_side[side], side), 4),
            "decimal_odds": round(odds_by_side[side], 4),
        }
    rec_side = team_side(record.get("purchase_team"), home, away)
    if rec_side is None:
        upper_side = team_side(record.get("upper_team"), home, away)
        lower_side = team_side(record.get("lower_team"), home, away)
        rec_side = upper_side if str(record.get("recommendation") or "") == "上盘" else lower_side
    if rec_side not in sides:
        rec_side = "home" if as_float(record.get("score")) >= 0 else "away"
    rec = sides[rec_side]
    shallow = abs(home_line) <= 0.5
    strict_buy = rec["model_roi"] > 0 and rec["euro_roi"] > -0.04
    balanced_buy = (not shallow) or rec["model_roi"] > 0 or rec["euro_roi"] > 0
    risk_veto_buy = (
        (not shallow and not (rec["model_roi"] < -0.08 and rec["euro_roi"] < -0.08))
        or (shallow and (rec["model_roi"] > 0 or rec["euro_roi"] > 0))
    )
    loose_buy = (not shallow) or not (rec["model_roi"] < -0.04 and rec["euro_roi"] < -0.04)
    if not shallow and rec["model_roi"] < -0.08 and rec["euro_roi"] < -0.08:
        risk = "深盘高风险"
    elif rec["model_roi"] > 0 and rec["euro_roi"] > 0:
        risk = "双正ROI"
    elif rec["model_roi"] > 0 or rec["euro_roi"] > 0:
        risk = "单侧正ROI"
    elif shallow:
        risk = "浅盘价格差"
    else:
        risk = "方向保留"
    return {
        "version": "review-2",
        "method": "net_goal_distribution",
        "home_line": round(home_line, 4),
        "shallow": shallow,
        "probabilities": {
            "euro_kelly": [round(value, 4) for value in euro_kelly_prob],
            "model": [round(value, 4) for value in model_prob],
        },
        "sides": sides,
        "recommendation_side": rec_side,
        "recommendation_label": rec["label"],
        "decision": "买" if balanced_buy else "放弃",
        "risk": risk,
        "strategies": {
            "strict": strict_buy,
            "balanced": balanced_buy,
            "risk_veto": risk_veto_buy,
            "loose": loose_buy,
        },
    }


def review2_record_pricing(record: dict[str, Any], snapshot_record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record.get("status") != "ok" or not snapshot_record:
        return None
    match = snapshot_record.get("match") if isinstance(snapshot_record.get("match"), dict) else {}
    raw = match.get("raw") if isinstance(match.get("raw"), dict) else {}
    market = snapshot_record.get("market") if isinstance(snapshot_record.get("market"), dict) else {}
    return review2_pricing_from_context(record, match=match, raw=raw, market=market)


def review2_prediction_pricing(result: dict[str, Any], match: dict[str, Any]) -> dict[str, Any] | None:
    """Price a live prediction result with the review-2 AH ROI/Kelly framework."""
    raw = match.get("raw") if isinstance(match.get("raw"), dict) else {}
    record = {
        "status": "ok",
        "home": match.get("home"),
        "away": match.get("away"),
        "asian_line": result.get("asian_line") or match.get("asian_line"),
        "recommendation": result.get("recommendation") or result.get("purchase_side"),
        "purchase_side": result.get("purchase_side") or result.get("recommendation"),
        "purchase_team": result.get("purchase_team"),
        "upper_team": result.get("upper_team"),
        "lower_team": result.get("lower_team"),
        "score": result.get("score"),
    }
    return review2_pricing_from_context(record, match=match, raw=raw, market={})


def enrich_records_with_review2(
    records: list[dict[str, Any]],
    snapshot_lookup: Any,
) -> None:
    """Attach ``review2`` to validation records.

    ``snapshot_lookup`` may be a callable taking event_id or a mapping whose
    values are either a latest snapshot dict or a list of snapshots.
    """

    def lookup(event_id: int) -> dict[str, Any] | None:
        if callable(snapshot_lookup):
            return snapshot_lookup(event_id)
        if isinstance(snapshot_lookup, dict):
            value = snapshot_lookup.get(event_id) or snapshot_lookup.get(str(event_id))
            if isinstance(value, list):
                return value[-1] if value else None
            return value if isinstance(value, dict) else None
        return None

    for record in records:
        event_id = as_int(record.get("event_id"))
        pricing = review2_record_pricing(record, lookup(event_id) if event_id else None)
        if pricing is not None:
            record["review2"] = pricing


def roi_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [record for record in records if validate_record_is_bet(record)]
    net_profit = sum(as_float(record.get("profit")) for record in usable)
    outcomes = {outcome: 0 for outcome in SETTLED_OUTCOMES}
    for record in usable:
        outcome = str(record.get("outcome") or "")
        if outcome in outcomes:
            outcomes[outcome] += 1
    bets = len(usable)
    return {
        "bets": bets,
        "net_profit": round(net_profit, 4),
        "roi": round(net_profit / bets, 4) if bets else None,
        **outcomes,
    }


def summarize_strategy(records: list[dict[str, Any]], strategy: str) -> dict[str, Any]:
    usable = [
        record
        for record in records
        if validate_record_is_bet(record)
        and isinstance(record.get("review2"), dict)
        and bool(((record.get("review2") or {}).get("strategies") or {}).get(strategy))
    ]
    summary = roi_summary(usable)
    summary["total"] = len(usable)
    return summary


def review2_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "algorithm": "review-2",
        "description": "浅盘价格过滤；深盘净胜球分布保留模型方向并标风险",
        "strategies": {
            "strict": summarize_strategy(records, "strict"),
            "balanced": summarize_strategy(records, "balanced"),
            "risk_veto": summarize_strategy(records, "risk_veto"),
            "loose": summarize_strategy(records, "loose"),
        },
    }
