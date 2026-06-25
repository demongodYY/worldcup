"""Shared Asian-handicap settlement and snapshot-validation helpers."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


SETTLED_OUTCOMES = ("full_win", "half_win", "push", "half_loss", "full_loss")
WIN_OUTCOMES = ("full_win", "half_win")
LOSS_OUTCOMES = ("half_loss", "full_loss")


@dataclass(frozen=True)
class AsianHandicapSettlement:
    outcome: str
    unit_result: float
    profit: float | None
    decimal_odds: float | None
    margin: float
    leg_margins: tuple[float, ...]


@dataclass(frozen=True)
class CutoffSelection:
    index: int
    record: dict[str, Any]
    minutes_before_kickoff: float
    distance_minutes: float


def parse_utc_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_asian_decimal_odds(value: Any) -> float | None:
    """Return decimal odds from common decimal or Hong Kong-style Asian prices."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    if price < 1.5:
        price += 1.0
    if not 1.5 <= price <= 5.0:
        return None
    return price


def split_asian_line(line: float) -> tuple[float, ...]:
    quarter_units = round(float(line) * 4)
    normalized = quarter_units / 4.0
    if quarter_units % 2 == 0:
        return (normalized,)
    lower = math.floor(normalized * 2) / 2.0
    upper = math.ceil(normalized * 2) / 2.0
    return (lower, upper)


def margin_for_upper(
    home_goals: int,
    away_goals: int,
    line: float,
    upper_team: str,
    home: str,
    away: str,
) -> float:
    if upper_team == home:
        return float(home_goals - away_goals) + line
    if upper_team == away:
        return float(away_goals - home_goals) - line
    raise ValueError(f"upper_team {upper_team!r} not in {home!r} / {away!r}")


def settle_asian_handicap(
    recommendation: str,
    upper_team: str,
    home: str,
    away: str,
    line: float,
    home_goals: int,
    away_goals: int,
    *,
    decimal_odds: float | None = None,
) -> AsianHandicapSettlement:
    if recommendation not in ("上盘", "下盘"):
        return AsianHandicapSettlement("na", 0.0, None, decimal_odds, 0.0, ())

    leg_margins = tuple(
        margin_for_upper(home_goals, away_goals, leg, upper_team, home, away)
        for leg in split_asian_line(line)
    )
    leg_results: list[float] = []
    for margin in leg_margins:
        upper_result = 1.0 if margin > 1e-9 else -1.0 if margin < -1e-9 else 0.0
        leg_results.append(upper_result if recommendation == "上盘" else -upper_result)
    unit_result = sum(leg_results) / len(leg_results)
    outcome = {
        1.0: "full_win",
        0.5: "half_win",
        0.0: "push",
        -0.5: "half_loss",
        -1.0: "full_loss",
    }[unit_result]
    normalized_odds = normalize_asian_decimal_odds(decimal_odds)
    profit: float | None = None
    if normalized_odds is not None:
        profit = unit_result * (normalized_odds - 1.0) if unit_result > 0 else unit_result
    return AsianHandicapSettlement(
        outcome=outcome,
        unit_result=unit_result,
        profit=profit,
        decimal_odds=normalized_odds,
        margin=margin_for_upper(home_goals, away_goals, line, upper_team, home, away),
        leg_margins=leg_margins,
    )


def legacy_placeholder_snapshot(record: dict[str, Any]) -> bool:
    match = record.get("match") if isinstance(record.get("match"), dict) else {}
    raw = match.get("raw") if isinstance(match.get("raw"), dict) else {}
    signature = (
        raw.get("_okooo_popularity_home"),
        raw.get("_okooo_popularity_draw"),
        raw.get("_okooo_popularity_away"),
        raw.get("_okooo_diff_home"),
        raw.get("_okooo_diff_draw"),
        raw.get("_okooo_diff_away"),
        raw.get("_okooo_zhishu_tips"),
    )
    return signature == (50.0, 28.0, 22.0, 10.0, 5.0, 10.0, "胜 平")


def snapshot_validation_issues(record: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    match = record.get("match") if isinstance(record.get("match"), dict) else {}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    raw = match.get("raw") if isinstance(match.get("raw"), dict) else {}
    fetched_at = parse_utc_datetime(record.get("fetched_at"))
    match_time = parse_utc_datetime(match.get("match_time"))

    if not match:
        issues.append("missing_match")
    if fetched_at is None:
        issues.append("missing_fetched_at")
    if match_time is None:
        issues.append("missing_match_time")
    if fetched_at is not None and match_time is not None and fetched_at >= match_time:
        issues.append("post_kickoff")
    if provenance.get("validation_eligible") is False:
        issues.append(str(provenance.get("reason") or "provenance_excluded"))
    if raw.get("_validation_eligible") is False:
        issues.append(str(raw.get("_validation_exclusion_reason") or "raw_excluded"))
    if raw.get("_okooo_import_placeholders") or legacy_placeholder_snapshot(record):
        issues.append("placeholder_features")
    return list(dict.fromkeys(issues))


def minutes_before_kickoff(record: dict[str, Any]) -> float | None:
    match = record.get("match") if isinstance(record.get("match"), dict) else {}
    fetched_at = parse_utc_datetime(record.get("fetched_at"))
    match_time = parse_utc_datetime(match.get("match_time"))
    if fetched_at is None or match_time is None:
        return None
    return (match_time - fetched_at).total_seconds() / 60.0


def select_snapshot_at_cutoff(
    records: list[dict[str, Any]],
    cutoff_minutes: float,
    *,
    tolerance_minutes: float = 15.0,
    allowed_indices: set[int] | None = None,
) -> CutoffSelection | None:
    candidates: list[CutoffSelection] = []
    for index, record in enumerate(records):
        if allowed_indices is not None and index not in allowed_indices:
            continue
        if snapshot_validation_issues(record):
            continue
        before = minutes_before_kickoff(record)
        if before is None or before < 0:
            continue
        distance = abs(before - cutoff_minutes)
        if distance <= tolerance_minutes:
            candidates.append(CutoffSelection(index, record, before, distance))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item.distance_minutes, -item.index))


def select_last_eligible_snapshots(
    records: list[dict[str, Any]],
    *,
    count: int = 2,
    allowed_indices: set[int] | None = None,
) -> list[CutoffSelection]:
    candidates: list[CutoffSelection] = []
    for index, record in enumerate(records):
        if allowed_indices is not None and index not in allowed_indices:
            continue
        if snapshot_validation_issues(record):
            continue
        before = minutes_before_kickoff(record)
        if before is None or before < 0:
            continue
        candidates.append(CutoffSelection(index, record, before, 0.0))
    candidates.sort(key=lambda item: item.index)
    return candidates[-count:] if count > 0 else candidates


def selected_asian_price(
    record: dict[str, Any],
    result: dict[str, Any],
    recommendation: str,
) -> float | None:
    direct = normalize_asian_decimal_odds(result.get("purchase_decimal_odds"))
    if direct is not None:
        return direct
    match = record.get("match") if isinstance(record.get("match"), dict) else {}
    raw = match.get("raw") if isinstance(match.get("raw"), dict) else {}
    home = str(match.get("home") or "")
    away = str(match.get("away") or "")
    upper_team = str(result.get("upper_team") or "")
    lower_team = str(result.get("lower_team") or "")
    team = upper_team if recommendation == "上盘" else lower_team
    key = "Home" if team == home else "Away" if team == away else ""
    if not key:
        return None
    return normalize_asian_decimal_odds(raw.get(f"AsianAvr{key}"))


def summarize_settlements(records: list[dict[str, Any]]) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "full_win": 0,
        "half_win": 0,
        "push": 0,
        "half_loss": 0,
        "full_loss": 0,
        "na": 0,
        "missing": 0,
        "excluded": 0,
        "error": 0,
    }
    net_profit = 0.0
    roi_bets = 0
    for record in records:
        outcome = str(record.get("outcome") or "")
        if outcome in stats:
            stats[outcome] += 1
        elif record.get("status") in ("missing", "empty", "no_cutoff_snapshot", "insufficient_snapshots"):
            stats["missing"] += 1
        elif record.get("status") == "excluded":
            stats["excluded"] += 1
        elif record.get("status") == "error":
            stats["error"] += 1
        profit = record.get("profit")
        if outcome in SETTLED_OUTCOMES and isinstance(profit, (int, float)) and math.isfinite(float(profit)):
            net_profit += float(profit)
            roi_bets += 1
    positive = stats["full_win"] + stats["half_win"]
    negative = stats["half_loss"] + stats["full_loss"]
    stats["directional"] = positive + negative
    stats["positive_rate"] = round(positive / (positive + negative), 4) if positive + negative else None
    stats["net_profit"] = round(net_profit, 4)
    stats["roi_bets"] = roi_bets
    stats["roi"] = round(net_profit / roi_bets, 4) if roi_bets else None
    stats["total"] = len(records)
    return stats
