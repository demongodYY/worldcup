#!/usr/bin/env python3
"""Local dashboard for Okooo prediction snapshots and validation replay."""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asian_handicap_validation import settle_asian_handicap, summarize_settlements  # noqa: E402
from okooo_review2 import (  # noqa: E402
    enrich_records_with_review2 as enrich_review2_records,
    review2_prediction_pricing,
    review2_summary,
)
from okooo_ah_cli import (  # noqa: E402
    DEFAULT_OKOOO_VALIDATE_SCORES_PATH,
    OKOOO_DEFAULT_ISSUE,
    OkoooClient,
    attach_review2_pricing,
    attach_snapshot_replay_fields,
    build_validate_snapshot_records,
    cookie_from_env,
    load_okooo_validate_scores,
    median_snapshot_prediction_dict,
    replay_snapshot_result_at,
    replay_snapshot_results,
    snapshot_dir as resolve_okooo_snapshot_dir,
)
from worldcup_ah_cli import (  # noqa: E402
    AnalysisResult,
    DataError,
    Match,
    MODEL_VERSION,
    Predictor,
    SnapshotStore,
    line_value,
    load_dotenv_file,
    match_from_dict,
    score_snapshot_signal_history,
    score_strength_label,
    snapshot_metrics,
    snapshot_trend_summary,
)

DEFAULT_HTML_PATH = ROOT / "tools" / "okooo_dashboard.html"
MAX_PENDING_PREDICTIONS = 16
PENDING_PREDICTIONS: dict[str, AnalysisResult] = {}


@dataclass(frozen=True)
class DashboardConfig:
    snapshot_root: Path
    scores_json: Path | None
    html_path: Path
    issue: str
    timeout: float
    cookie_file: str | None
    trade_trend: bool
    detail_max_pages: int


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return sorted(records, key=lambda item: str(item.get("fetched_at", "")))


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


def timestamp_sort_value(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return float("-inf")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def snapshot_event_sort_key(event: dict[str, Any]) -> tuple[float, int]:
    return timestamp_sort_value(event.get("match_time")), as_int(event.get("event_id"))


def infer_strength(score: Any, explicit: Any = None) -> str:
    if explicit:
        return str(explicit)
    return score_strength_label(as_float(score))


def normalize_result_dict(result: dict[str, Any]) -> dict[str, Any]:
    score = as_float(result.get("score"))
    recommendation = str(result.get("recommendation") or result.get("purchase_side") or "")
    if recommendation not in ("上盘", "下盘"):
        recommendation = "上盘" if score >= 0 else "下盘"
    purchase_side = str(result.get("purchase_side") or recommendation)
    if purchase_side not in ("上盘", "下盘"):
        purchase_side = recommendation
    purchase_team = str(result.get("purchase_team") or "")
    if not purchase_team:
        if purchase_side == "上盘":
            purchase_team = str(result.get("upper_team") or "")
        elif purchase_side == "下盘":
            purchase_team = str(result.get("lower_team") or "")
    return {
        "event_id": result.get("event_id"),
        "match": result.get("match"),
        "recommendation": recommendation,
        "purchase_side": purchase_side,
        "purchase_team": purchase_team,
        "model_recommendation": result.get("model_recommendation") or recommendation,
        "score": round(score, 4),
        "purchase_score": round(as_float(result.get("purchase_score"), score), 4),
        "strength": infer_strength(score, result.get("strength")),
        "confidence": as_int(result.get("confidence")),
        "completeness": as_int(result.get("completeness")),
        "upper_team": result.get("upper_team") or "",
        "lower_team": result.get("lower_team") or "",
        "decision_reason": result.get("decision_reason") or "",
        "warnings": result.get("warnings") if isinstance(result.get("warnings"), list) else [],
        "signals": result.get("signals") if isinstance(result.get("signals"), list) else [],
        "review2": result.get("review2") if isinstance(result.get("review2"), dict) else None,
        "snapshot_median_count": result.get("snapshot_median_count"),
        "snapshot_median_total_count": result.get("snapshot_median_total_count"),
        "last_replay_score": result.get("last_replay_score"),
    }


def record_point(record: dict[str, Any]) -> dict[str, Any]:
    metrics = snapshot_metrics(record)
    match = record.get("match") if isinstance(record.get("match"), dict) else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    normalized = normalize_result_dict(result)
    signals = result_signal_map(record)
    return {
        "fetched_at": record.get("fetched_at") or "",
        "match_time": match.get("match_time") or result.get("match_time") or "",
        "asian_line": match.get("asian_line") or result.get("asian_line") or "",
        "recommendation": normalized["recommendation"],
        "purchase_side": normalized["purchase_side"],
        "purchase_team": normalized["purchase_team"],
        "model_recommendation": normalized["model_recommendation"],
        "strength": normalized["strength"],
        "score": round(metrics["score"], 4),
        "confidence": normalized["confidence"],
        "completeness": normalized["completeness"],
        "heat_edge": round(metrics["heat_edge"], 4),
        "amount_edge": round(metrics["amount_edge"], 4),
        "payout_edge": round(metrics["payout_edge"], 4),
        "upper_water": round(metrics["upper_water"], 4),
        "lower_water": round(metrics["lower_water"], 4),
        "line_depth": round(metrics["line_depth"], 4),
        "euro_edge": round(metrics["euro_edge"], 4),
        "signal_scores": {
            name: round(as_float(data.get("score")), 4)
            for name, data in signals.items()
            if data.get("score") is not None
        },
    }


def important_signals(result: dict[str, Any], limit: int = 7) -> list[dict[str, Any]]:
    signals = result.get("signals")
    if not isinstance(signals, list):
        return []
    usable: list[dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict) or not signal.get("available", False):
            continue
        usable.append(
            {
                "name": signal.get("name") or "",
                "score": round(as_float(signal.get("score")), 4),
                "weight": as_float(signal.get("weight")),
                "summary": signal.get("summary") or signal.get("reason") or "",
            }
        )
    usable.sort(key=lambda item: abs(item["score"]) * max(item["weight"], 0.01), reverse=True)
    return usable[:limit]


def result_signal_map(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    signals = result.get("signals") if isinstance(result.get("signals"), list) else []
    mapped: dict[str, dict[str, Any]] = {}
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        name = str(signal.get("name") or "").strip()
        if not name:
            continue
        available = bool(signal.get("available", False))
        score = as_float(signal.get("score")) if available else None
        mapped[name] = {
            "name": name,
            "score": score,
            "weight": as_float(signal.get("weight")),
            "available": available,
            "summary": signal.get("summary") or signal.get("reason") or "",
            "reason": signal.get("reason") or "",
        }
    return mapped


def _round_optional(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _delta_optional(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return left - right


def signal_delta_rows(records: list[dict[str, Any]], *, limit: int = 12) -> list[dict[str, Any]]:
    if not records:
        return []
    first_map = result_signal_map(records[0])
    previous_map = result_signal_map(records[-2] if len(records) >= 2 else records[-1])
    last_map = result_signal_map(records[-1])
    names = set(first_map) | set(previous_map) | set(last_map)
    rows: list[dict[str, Any]] = []
    for name in names:
        first = first_map.get(name, {})
        previous = previous_map.get(name, {})
        last = last_map.get(name, {})
        first_score = first.get("score")
        previous_score = previous.get("score")
        last_score = last.get("score")
        total_delta = _delta_optional(last_score, first_score)
        recent_delta = _delta_optional(last_score, previous_score)
        weight = as_float(last.get("weight"), as_float(previous.get("weight"), as_float(first.get("weight"))))
        weighted_total_delta = total_delta * weight if total_delta is not None else None
        weighted_recent_delta = recent_delta * weight if recent_delta is not None else None
        summary = str(last.get("summary") or previous.get("summary") or first.get("summary") or "")
        reason = str(last.get("reason") or previous.get("reason") or first.get("reason") or "")
        rows.append(
            {
                "name": name,
                "first_score": _round_optional(first_score),
                "previous_score": _round_optional(previous_score),
                "last_score": _round_optional(last_score),
                "delta": _round_optional(total_delta),
                "recent_delta": _round_optional(recent_delta),
                "weight": round(weight, 4),
                "weighted_delta": _round_optional(weighted_total_delta),
                "weighted_recent_delta": _round_optional(weighted_recent_delta),
                "summary": summary,
                "reason": reason,
            }
        )

    def sort_key(row: dict[str, Any]) -> tuple[float, float, str]:
        recent = abs(as_float(row.get("weighted_recent_delta")))
        total = abs(as_float(row.get("weighted_delta")))
        current = abs(as_float(row.get("last_score")))
        priority = recent + total * 0.55 + current * 0.03
        if row.get("name") == "数据质量":
            priority *= 0.05
        return priority, total, str(row.get("name") or "")

    rows.sort(key=sort_key, reverse=True)
    return rows[:limit]


def _direction_from_delta(delta: float | None) -> str:
    if delta is None or abs(delta) < 0.01:
        return "基本没动"
    return "拉向上盘" if delta > 0 else "拉向下盘"


def _side_from_score(score: float | None) -> str:
    if score is None or abs(score) < 0.05:
        return "中性"
    return "偏上盘" if score > 0 else "偏下盘"


PLAIN_SIGNAL_NAMES = {
    "必发指数": "必发热度",
    "必发成交走势": "成交走势",
    "亚盘水位": "盘口和水位",
    "欧赔/Kelly": "欧赔/Kelly",
    "市场平衡/背离": "市场是否同向",
    "平局风险": "平局风险",
    "盘口合理性": "盘口深浅",
    "公司一致性": "公司一致性",
    "盘口深度/打穿能力": "赢盘难度",
    "赢盘门槛风险": "赢盘门槛",
    "快照趋势": "快照趋势",
    "资金/盘口弹性": "资金后盘口反应",
    "外部赔率/实力校验": "外部赔率校验",
    "高低水价值": "水位性价比",
    "临场score变化": "临场动能",
    "数据质量": "数据质量",
}


def _plain_side_label(side: Any, result: dict[str, Any]) -> str:
    side_text = str(side or "")
    if side_text not in ("上盘", "下盘"):
        side_text = "上盘" if as_float(result.get("score")) >= 0 else "下盘"
    if side_text == "上盘":
        team = result.get("upper_team") or result.get("purchase_team") or ""
    elif side_text == "下盘":
        team = result.get("lower_team") or result.get("purchase_team") or ""
    else:
        team = ""
    return f"{side_text}（{team}）" if team else side_text


def _plain_direction_word(value: float | None) -> str:
    if value is None or abs(value) < 0.015:
        return "基本没变"
    return "往上盘靠" if value > 0 else "往下盘靠"


def _plain_movement_with_context(delta: float | None, current: float | None, result: dict[str, Any]) -> str:
    base = _plain_direction_word(delta)
    if delta is None or current is None or abs(delta) < 0.015 or abs(current) < 0.05:
        return base
    if current < 0 and delta > 0:
        return f"{base}，但当前仍偏下盘（只是下盘力度变弱）"
    if current > 0 and delta < 0:
        return f"{base}，但当前仍偏上盘（只是上盘力度变弱）"
    return base


def _plain_signal_side(value: float | None, result: dict[str, Any]) -> str:
    if value is None or abs(value) < 0.05:
        return "比较中性"
    return f"更支持{_plain_side_label('上盘' if value > 0 else '下盘', result)}"


def _reason_clauses(text: Any) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    clauses = [part.strip() for part in re.split(r"[；;]", raw) if part.strip()]
    return [part for part in clauses if "澳客同源" not in part and "澳客亚盘主导" not in part]


def _extract_float(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text)
    if not match:
        return None
    return as_float(match.group(1))


def _plain_delta_direction(value: float | None, *, context: str = "信号") -> str:
    if value is None or abs(value) < 0.01:
        return "变化很小"
    if context == "fair_line":
        return "上盘更受支持" if value > 0 else "下盘更受支持"
    return "往上盘靠" if value > 0 else "往下盘靠"


def _plain_handicap_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    reason = str(row.get("reason") or "")
    clauses = _reason_clauses(reason)
    parts: list[str] = []
    fair_change = _extract_float(r"等价公平盘口中位变化\s*([+-]?\d+(?:\.\d+)?)\s*球", reason)
    if fair_change is not None:
        parts.append(
            f"模型先把盘口深度和两边水位折成一条“公平盘口轴”，中位变化 {fair_change:+.3f} 球，"
            f"含义是{_plain_delta_direction(fair_change, context='fair_line')}"
        )
    company = re.search(
        r"公司同向\s*上(\d+)/下(\d+)/中性(\d+).*?一致性可信度\s*([0-9.]+)",
        reason,
    )
    if company:
        up, down, neutral, credibility = company.groups()
        parts.append(
            f"公司方向是上盘 {up} 家、下盘 {down} 家、中性 {neutral} 家，一致性可信度 {credibility}"
        )
    path = next((clause for clause in clauses if "等价公平盘口路径" in clause or "近180分钟公平盘口" in clause), "")
    if path:
        parts.append(path)
    examples = [clause for clause in clauses if "盘口" in clause and "水位" in clause and "->" in clause]
    if examples:
        parts.append("代表公司例子：" + "；".join(examples[:2]))
    if not parts:
        parts = clauses[:3]
    return "为什么：" + "；".join(parts[:4]) + "。"


def _plain_trade_response_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    reason = str(row.get("reason") or "")
    clauses = _reason_clauses(reason)
    parts: list[str] = []
    heat = next((clause for clause in clauses if "热度" in clause), "")
    response = next((clause for clause in clauses if "资金后等价公平盘口变化" in clause), "")
    live = next((clause for clause in clauses if "临场资金后盘口响应" in clause), "")
    if heat:
        parts.append(heat)
    if response:
        parts.append("看资金进来后盘口有没有跟，" + response)
    if live:
        parts.append(live)
    if not parts:
        parts = clauses[:3]
    return "为什么：" + "；".join(parts[:3]) + "。"


def _plain_market_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    clauses = _reason_clauses(row.get("reason"))
    useful = [clause for clause in clauses if "多信号" not in clause][:4]
    if not useful:
        useful = clauses[:4]
    return "为什么：它把热度、亚盘、成交、欧赔放在一起看；" + "；".join(useful) + "。"


def _plain_bifa_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    clauses = _reason_clauses(row.get("reason"))
    main = clauses[0] if clauses else ""
    caution = next((clause for clause in clauses[1:] if "扣分" in clause or "浅盘" in clause or "大热" in clause), "")
    parts = [part for part in (main, caution) if part]
    return "为什么：必发看的是谁更热、成交额集中在哪边、庄家盈亏压力在哪边；" + "；".join(parts[:2]) + "。"


def _plain_trade_flow_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    clauses = _reason_clauses(row.get("reason"))
    key = [
        clause
        for clause in clauses
        if "异常成交份额" in clause or "价格冲击" in clause or "成交加速度" in clause or "价格" in clause
    ]
    if not key:
        key = clauses[:3]
    return "为什么：成交走势看的是钱有没有继续追热门，以及价格有没有被资金压低/抬高；" + "；".join(key[:4]) + "。"


def _plain_euro_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    clauses = _reason_clauses(row.get("reason"))
    key = clauses[:3]
    return "为什么：欧赔/Kelly用来确认胜负价格有没有跟盘口同向；" + "；".join(key) + "。"


def _plain_cover_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    clauses = _reason_clauses(row.get("reason"))
    key = clauses[:4]
    return "为什么：赢盘门槛看的是热门方不只是赢球，还要能打穿盘口；" + "；".join(key) + "。"


def _plain_generic_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    clauses = _reason_clauses(row.get("reason"))
    if clauses:
        return "为什么：" + "；".join(clauses[:3]) + "。"
    summary = str(row.get("summary") or "").strip()
    return f"为什么：{summary}" if summary else ""


def _plain_signal_reason(row: dict[str, Any], result: dict[str, Any]) -> str:
    name = str(row.get("name") or "")
    if name == "亚盘水位":
        return _plain_handicap_reason(row, result)
    if name == "资金/盘口弹性":
        return _plain_trade_response_reason(row, result)
    if name == "市场平衡/背离":
        return _plain_market_reason(row, result)
    if name == "必发指数":
        return _plain_bifa_reason(row, result)
    if name == "必发成交走势":
        return _plain_trade_flow_reason(row, result)
    if name == "欧赔/Kelly":
        return _plain_euro_reason(row, result)
    if name == "赢盘门槛风险":
        return _plain_cover_reason(row, result)
    return _plain_generic_reason(row, result)


def _plain_signal_sentence(row: dict[str, Any], result: dict[str, Any]) -> str:
    name = PLAIN_SIGNAL_NAMES.get(str(row.get("name") or ""), str(row.get("name") or "信号"))
    current = row.get("last_score")
    recent = row.get("recent_delta")
    total = row.get("delta")
    if row.get("name") == "必发指数":
        if current is None or abs(as_float(current)) < 0.05:
            side_text = "资金热度比较分散"
        else:
            hot_side = "上盘" if as_float(current) > 0 else "下盘"
            side_text = f"显示{_plain_side_label(hot_side, result)}是热门，不单独代表下注价值"
    else:
        side_text = _plain_signal_side(current, result)
    recent_text = _plain_movement_with_context(recent, current, result)
    total_text = _plain_movement_with_context(total, current, result)
    reason = _plain_signal_reason(row, result)
    trend = f"节奏：最近一条{recent_text}"
    if total is None:
        return f"{name}：当前{side_text}。{reason}{trend}。"
    return f"{name}：当前{side_text}。{reason}{trend}；从第一条到现在{total_text}。"


def plain_trend_explanation(
    *,
    result: dict[str, Any],
    records: list[dict[str, Any]],
    score_delta: float,
    signal_deltas: list[dict[str, Any]],
    heat_delta: float,
    amount_delta: float,
    payout_delta: float,
    upper_water_delta: float,
    line_depth_delta: float,
) -> dict[str, Any]:
    """A deliberately non-technical explanation for dashboard readers."""
    if not records:
        return {"headline": "暂无足够快照，先等下一次采样。", "bullets": [], "warning": ""}
    first_score = snapshot_metrics(records[0])["score"]
    previous_score = snapshot_metrics(records[-2])["score"] if len(records) >= 2 else first_score
    last_score = snapshot_metrics(records[-1])["score"]
    recent_score_delta = last_score - previous_score
    side = result.get("purchase_side") or result.get("recommendation") or ("上盘" if last_score >= 0 else "下盘")
    side_label = _plain_side_label(side, result)
    score = as_float(result.get("score"), last_score)
    strength = result.get("strength") or infer_strength(score)
    if abs(score) < 0.015:
        headline = f"这场现在只是轻微偏{side_label}，分差很小；重点看盘口/水位、欧赔和成交后面有没有一起确认。"
    else:
        headline = f"这场现在偏{side_label}，强度是{strength}；重点看盘口/水位、欧赔和成交有没有一起确认。"

    bullets = [
        (
            f"整体方向：score 从 {first_score:+.3f} 走到 {last_score:+.3f}，"
            f"最后一跳 {recent_score_delta:+.3f}。简单说，就是模型最近{_plain_direction_word(recent_score_delta)}。"
        )
    ]

    directional_rows = [
        row
        for row in signal_deltas
        if row.get("name") != "数据质量"
        and (abs(as_float(row.get("recent_delta"))) >= 0.015 or abs(as_float(row.get("delta"))) >= 0.035)
    ]
    for row in directional_rows[:3]:
        bullets.append(_plain_signal_sentence(row, result))

    visible_metric_moves = [
        ("热度", heat_delta),
        ("成交", amount_delta),
        ("盈亏压力", payout_delta),
        ("上盘水位", upper_water_delta),
        ("盘口深度", line_depth_delta),
    ]
    obvious = [(name, value) for name, value in visible_metric_moves if abs(value) >= 0.04]
    if obvious:
        readable = "、".join(f"{name}{'上升' if value > 0 else '下降'}" for name, value in obvious[:3])
        bullets.append(f"表格里能直接看到的变化主要是：{readable}。")
    else:
        bullets.append("如果你看到热度、成交、盈亏、深度几列差不多，但 score 还在动，通常是水位、欧赔或历史趋势这些隐藏项在变，不是模型随机跳。")

    line_depth = abs(line_value(str((records[-1].get("match") or {}).get("asian_line", "0"))))
    warning = ""
    if side == "上盘" and line_depth >= 1.25:
        warning = "提醒：这是深盘思路，赢球不等于赢盘，要确认强队有打穿能力。"
    elif side == "上盘" and abs(score) < 0.12:
        warning = "提醒：虽然偏上盘，但分数还在边缘区，别把轻微优势当成强信号。"
    elif side == "下盘" and line_depth <= 0.5:
        warning = "提醒：浅盘下盘更多是在防热门打不穿，临场如果盘口/欧赔突然回到上盘，要重新看。"
    elif abs(recent_score_delta) >= 0.08:
        warning = "提醒：最后一跳变化较大，说明临场信息在重新定价，最好结合最新盘口再确认。"
    else:
        warning = "提醒：这只是市场信号解释，不等于保证赛果；重点看临场是否继续同向。"

    return {
        "headline": headline,
        "bullets": bullets[:5],
        "warning": warning,
        "score_delta": round(score_delta, 4),
        "recent_score_delta": round(recent_score_delta, 4),
    }


def trend_reason_lines(
    *,
    result: dict[str, Any],
    records: list[dict[str, Any]],
    score_delta: float,
    signal_deltas: list[dict[str, Any]],
    heat_delta: float,
    amount_delta: float,
    payout_delta: float,
    upper_water_delta: float,
    line_depth_delta: float,
) -> list[str]:
    if not records:
        return []
    first_score = snapshot_metrics(records[0])["score"]
    previous_score = snapshot_metrics(records[-2])["score"] if len(records) >= 2 else first_score
    last_score = snapshot_metrics(records[-1])["score"]
    recent_score_delta = last_score - previous_score
    side = result.get("purchase_side") or result.get("recommendation") or ("上盘" if last_score >= 0 else "下盘")
    team = result.get("purchase_team") or ""
    lines = [
        (
            f"综合分从首条 {first_score:+.3f} 到末条 {last_score:+.3f}，全程变化 {score_delta:+.3f}；"
            f"最近一跳 {recent_score_delta:+.3f}，当前中位数口径推荐{side}{f'（{team}）' if team else ''}。"
        )
    ]
    if len(records) >= 2 and abs(recent_score_delta) >= 0.04:
        lines.append("最近一条快照变化比较明显，建议优先看“近期变化”列，而不是只看首末累计。")
    elif len(records) >= 2:
        lines.append("最近一条快照变化不大，当前方向主要来自前面已经累积的信号。")

    moved = [
        row
        for row in signal_deltas
        if abs(as_float(row.get("recent_delta"))) >= 0.015 or abs(as_float(row.get("delta"))) >= 0.035
    ][:4]
    if moved:
        fragments: list[str] = []
        for row in moved[:3]:
            recent = row.get("recent_delta")
            total = row.get("delta")
            current = row.get("last_score")
            fragments.append(
                f"{row.get('name')}当前{_side_from_score(current)}，近期{_direction_from_delta(recent)} {as_float(recent):+.3f}，全程 {as_float(total):+.3f}"
            )
        lines.append("主要变化来自：" + "；".join(fragments) + "。")
    else:
        lines.append("各核心信号最近没有明显跳变，score 小幅波动多半来自水位/欧赔等连续变量的细小变化和中位数重放口径。")

    visible_metric_moves = [
        ("热度", heat_delta),
        ("成交", amount_delta),
        ("盈亏", payout_delta),
        ("上盘水位", upper_water_delta),
        ("盘口深度", line_depth_delta),
    ]
    obvious = [(name, value) for name, value in visible_metric_moves if abs(value) >= 0.04]
    if obvious:
        lines.append(
            "可见指标变化：" + "；".join(f"{name} {value:+.3f}" for name, value in obvious[:4]) + "。"
        )
    else:
        lines.append("列表里的热度、成交、盈亏、深度看起来可能接近不变；若 score 仍变化，通常是亚盘均水、欧赔/Kelly、市场平衡和快照趋势这些隐藏信号在动。")

    return lines


def replay_snapshot_result(snapshot_root: Path, event_id: int, records: list[dict[str, Any]], index: int) -> AnalysisResult:
    """Replay one snapshot point with the current Predictor and only prior history.

    ``records[index]`` is the current point. The replay client sees records up to
    that point, while SnapshotStore history excludes the current point so trend
    signals never look ahead.
    """
    return replay_snapshot_result_at(snapshot_root, event_id, records, index)


def replayed_snapshot_records(snapshot_root: Path, event_id: int, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, record in enumerate(records):
        try:
            result = replay_snapshot_result(snapshot_root, event_id, records, idx)
            replayed = dict(record)
            replayed["result"] = result.to_dict()
            out.append(replayed)
        except Exception:
            # Keep the row visible if one old snapshot cannot be replayed.
            out.append(record)
    return out


def build_snapshot_events(snapshot_root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not snapshot_root.is_dir():
        return events
    for path in sorted(snapshot_root.glob("*.jsonl")):
        try:
            event_id = int(path.stem)
        except ValueError:
            continue
        records = [record for record in read_jsonl(path) if isinstance(record.get("match"), dict)]
        if not records:
            continue
        replayed_records = replayed_snapshot_records(snapshot_root, event_id, records)
        first = replayed_records[0]
        last = replayed_records[-1]
        first_metrics = snapshot_metrics(first)
        last_metrics = snapshot_metrics(last)
        match_dict = last.get("match") if isinstance(last.get("match"), dict) else {}
        replay_results = [
            record["result"] for record in replayed_records if isinstance(record.get("result"), dict)
        ]
        result = median_snapshot_prediction_dict(
            replay_results,
            match=match_from_dict(match_dict) if match_dict else None,
        )
        normalized = normalize_result_dict(result)
        if normalized.get("review2") is None and match_dict:
            normalized["review2"] = review2_prediction_pricing(result, match_dict)
        score_delta = last_metrics["score"] - first_metrics["score"]
        heat_delta = last_metrics["heat_edge"] - first_metrics["heat_edge"]
        amount_delta = last_metrics["amount_edge"] - first_metrics["amount_edge"]
        payout_delta = last_metrics["payout_edge"] - first_metrics["payout_edge"]
        upper_water_delta = last_metrics["upper_water"] - first_metrics["upper_water"]
        line_depth_delta = last_metrics["line_depth"] - first_metrics["line_depth"]
        if len(records) >= 2:
            trend_note = snapshot_trend_summary(score_delta, heat_delta, upper_water_delta, line_depth_delta)
            signal_history_score, signal_history_reason = score_snapshot_signal_history(records)
        else:
            trend_note = "本地只有 1 条快照，趋势等待下一次采样"
            signal_history_score, signal_history_reason = 0.0, "本地快照不足 2 条"
        deltas = signal_delta_rows(replayed_records)
        trend_reasons = trend_reason_lines(
            result=normalized,
            records=replayed_records,
            score_delta=score_delta,
            signal_deltas=deltas,
            heat_delta=heat_delta,
            amount_delta=amount_delta,
            payout_delta=payout_delta,
            upper_water_delta=upper_water_delta,
            line_depth_delta=line_depth_delta,
        )
        plain_explanation = plain_trend_explanation(
            result=normalized,
            records=replayed_records,
            score_delta=score_delta,
            signal_deltas=deltas,
            heat_delta=heat_delta,
            amount_delta=amount_delta,
            payout_delta=payout_delta,
            upper_water_delta=upper_water_delta,
            line_depth_delta=line_depth_delta,
        )
        event = {
            "event_id": event_id,
            "home": match_dict.get("home") or "",
            "away": match_dict.get("away") or "",
            "match": f"{match_dict.get('home') or ''} vs {match_dict.get('away') or ''}",
            "league_name": match_dict.get("league_name") or "",
            "match_time": match_dict.get("match_time") or result.get("match_time") or "",
            "asian_line": match_dict.get("asian_line") or result.get("asian_line") or "",
            "snapshot_count": len(records),
            "first_fetched_at": first.get("fetched_at") or "",
            "last_fetched_at": last.get("fetched_at") or "",
            "last_result": normalized,
            "score_delta": round(score_delta, 4),
            "heat_delta": round(heat_delta, 4),
            "amount_delta": round(amount_delta, 4),
            "payout_delta": round(payout_delta, 4),
            "upper_water_delta": round(upper_water_delta, 4),
            "line_depth_delta": round(line_depth_delta, 4),
            "trend_note": trend_note,
            "trend_reasons": trend_reasons,
            "plain_explanation": plain_explanation,
            "signal_history_score": round(signal_history_score, 4),
            "signal_history_reason": signal_history_reason,
            "important_signals": important_signals(result),
            "signal_deltas": deltas,
            "series": [record_point(record) for record in replayed_records],
        }
        events.append(event)
    return sorted(events, key=snapshot_event_sort_key, reverse=True)


def margin_for_upper(home_goals: int, away_goals: int, line: float, upper: str, home: str, away: str) -> float:
    settlement = settle_asian_handicap("上盘", upper, home, away, line, home_goals, away_goals)
    return settlement.margin


def recommendation_outcome(
    recommendation: str,
    upper: str,
    lower: str,
    home: str,
    away: str,
    line_text: str,
    home_goals: int,
    away_goals: int,
) -> tuple[str, float]:
    settlement = settle_asian_handicap(
        recommendation,
        upper,
        home,
        away,
        line_value(line_text),
        home_goals,
        away_goals,
    )
    return settlement.outcome, settlement.margin


def replay_validate_result(snapshot_root: Path, event_id: int, home_goals: int, away_goals: int) -> dict[str, Any]:
    rows = build_validate_snapshot_records(
        snapshot_root,
        {event_id: (home_goals, away_goals)},
        mode="replay",
    )
    if not rows:
        return {
            "event_id": event_id,
            "status": "missing",
            "outcome": "missing",
            "scoreline": f"{home_goals}-{away_goals}",
        }
    row = dict(rows[0])
    row["last_fetched_at"] = row.get("fetched_at") or ""
    return row


def summarize_validate_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    stats = summarize_settlements(records)
    stats["hit"] = stats["full_win"] + stats["half_win"]
    stats["miss"] = stats["half_loss"] + stats["full_loss"]
    stats["accuracy"] = stats["positive_rate"]
    stats["total"] = len(records)
    stats["rolling"] = validate_rolling_summaries(records)
    stats["alerts"] = validate_rolling_alerts(stats["rolling"])
    return stats


def validate_record_is_bet(record: dict[str, Any]) -> bool:
    return (
        record.get("status") == "ok"
        and record.get("profit") is not None
        and record.get("outcome") not in ("missing", "excluded", "error", "na")
    )


def validate_roi_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [record for record in records if validate_record_is_bet(record)]
    net_profit = sum(as_float(record.get("profit")) for record in usable)
    outcomes: dict[str, int] = {
        "full_win": 0,
        "half_win": 0,
        "push": 0,
        "half_loss": 0,
        "full_loss": 0,
    }
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


def validate_depth_bucket(record: dict[str, Any]) -> str:
    depth = abs(line_value(str(record.get("asian_line") or "0")))
    if depth <= 0.5:
        return "shallow"
    if depth < 1.25:
        return "mid"
    return "deep"


def validate_rolling_summaries(records: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        [record for record in records if validate_record_is_bet(record)],
        key=validate_record_sort_key,
    )
    last_7 = ordered[-7:]
    last_13 = ordered[-13:]
    shallow_last_13 = [record for record in last_13 if validate_depth_bucket(record) == "shallow"]
    shallow_lower_last_13 = [
        record
        for record in shallow_last_13
        if str(record.get("recommendation") or record.get("purchase_side") or "") == "下盘"
    ]
    deep_upper_last_13 = [
        record
        for record in last_13
        if validate_depth_bucket(record) == "deep"
        and str(record.get("recommendation") or record.get("purchase_side") or "") == "上盘"
    ]
    return {
        "last_7": validate_roi_summary(last_7),
        "last_13": validate_roi_summary(last_13),
        "shallow_last_13": validate_roi_summary(shallow_last_13),
        "shallow_lower_last_13": validate_roi_summary(shallow_lower_last_13),
        "deep_upper_last_13": validate_roi_summary(deep_upper_last_13),
    }


def validate_rolling_alerts(rolling: dict[str, Any]) -> list[str]:
    alerts: list[str] = []
    last_13 = rolling.get("last_13") if isinstance(rolling.get("last_13"), dict) else {}
    shallow_lower = (
        rolling.get("shallow_lower_last_13")
        if isinstance(rolling.get("shallow_lower_last_13"), dict)
        else {}
    )
    deep_upper = (
        rolling.get("deep_upper_last_13")
        if isinstance(rolling.get("deep_upper_last_13"), dict)
        else {}
    )
    if as_int(last_13.get("bets")) >= 8 and as_float(last_13.get("roi")) < 0:
        alerts.append("近13注 ROI 为负，近期样本可能失效，先看分盘口表现")
    if as_int(shallow_lower.get("bets")) >= 3 and as_float(shallow_lower.get("roi")) < 0:
        alerts.append("浅盘下盘近期为负，需确认不是把热门风险误当下盘价值")
    if as_int(deep_upper.get("bets")) >= 3 and as_float(deep_upper.get("roi")) < 0:
        alerts.append("深盘上盘近期为负，需区分胜负确认和打穿确认")
    return alerts


def validate_record_sort_key(record: dict[str, Any]) -> tuple[str, int]:
    try:
        event_id = int(record.get("event_id") or 0)
    except (TypeError, ValueError):
        event_id = 0
    return str(record.get("last_fetched_at") or record.get("match_time") or ""), event_id


def build_validate_records(snapshot_root: Path, scores_json: Path | None) -> dict[str, Any]:
    effective_scores = scores_json or DEFAULT_OKOOO_VALIDATE_SCORES_PATH
    scores = load_okooo_validate_scores(effective_scores)
    records = build_validate_snapshot_records(
        snapshot_root,
        scores,
        mode="replay",
    )
    for record in records:
        record["last_fetched_at"] = record.get("fetched_at") or ""
    enrich_review2_records(
        records,
        lambda event_id: (items[-1] if (items := read_jsonl(snapshot_root / f"{event_id}.jsonl")) else None),
    )
    records = sorted(records, key=validate_record_sort_key, reverse=True)
    stats = summarize_validate_stats(records)
    stats["review2"] = review2_summary(records)
    return {
        "scores_json": str(effective_scores),
        "aggregation": "last_two_median",
        "records": records,
        "stats": stats,
    }


def build_dashboard_payload(snapshot_root: Path, scores_json: Path | None) -> dict[str, Any]:
    snapshots = build_snapshot_events(snapshot_root)
    validation = build_validate_records(snapshot_root, scores_json)
    validation_by_id = {record.get("event_id"): record for record in validation["records"]}
    for event in snapshots:
        validation_record = validation_by_id.get(event["event_id"])
        is_finished = bool(validation_record and validation_record.get("scoreline"))
        event["is_finished"] = is_finished
        event["match_status"] = "finished" if is_finished else "unfinished"
        if validation_record:
            event["validation"] = {
                "outcome": validation_record.get("outcome"),
                "scoreline": validation_record.get("scoreline"),
                "margin": validation_record.get("margin"),
                "recommendation": validation_record.get("recommendation"),
                "purchase_side": validation_record.get("purchase_side"),
                "purchase_team": validation_record.get("purchase_team"),
                "model_recommendation": validation_record.get("model_recommendation"),
                "score": validation_record.get("score"),
                "confidence": validation_record.get("confidence"),
                "completeness": validation_record.get("completeness"),
                "snapshot_median_count": validation_record.get("snapshot_median_count"),
                "snapshot_median_total_count": validation_record.get("snapshot_median_total_count"),
                "last_replay_score": validation_record.get("last_replay_score"),
            }
        else:
            event["validation"] = None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": {
            "version": MODEL_VERSION,
            "asian_direction_weight": 0.50,
            "trade_flow_weight": 0.135,
            "post_fund_response_weight": 0.065,
            "depth_risk_weight": 0.095,
        },
        "snapshot_root": str(snapshot_root),
        "snapshots": snapshots,
        "validation": validation,
    }


def analysis_result_payload(result: AnalysisResult) -> dict[str, Any]:
    data = normalize_result_dict(result.to_dict())
    data["signals"] = important_signals(result.to_dict(), limit=12)
    return data


def prune_pending_predictions() -> None:
    while len(PENDING_PREDICTIONS) > MAX_PENDING_PREDICTIONS:
        oldest = next(iter(PENDING_PREDICTIONS))
        PENDING_PREDICTIONS.pop(oldest, None)


def predict_latest(config: DashboardConfig, match_id: int, *, save_snapshot: bool) -> dict[str, Any]:
    client = OkoooClient(
        issue=config.issue,
        timeout=config.timeout,
        cookie=cookie_from_env(config.cookie_file),
        trade_trend=config.trade_trend,
        detail_max_pages=config.detail_max_pages,
    )
    store = SnapshotStore(config.snapshot_root)
    client.refresh()
    match: Match = client.build_match(match_id)
    result = Predictor(client, store).analyze(match)
    attach_review2_pricing(result)
    attach_snapshot_replay_fields(client, result.match)
    token = str(uuid.uuid4())
    PENDING_PREDICTIONS[token] = result
    prune_pending_predictions()
    path: Path | None = None
    if save_snapshot:
        path = store.save(result)
        PENDING_PREDICTIONS.pop(token, None)
    return {
        "saved": path is not None,
        "snapshot_path": str(path) if path else None,
        "save_token": None if path else token,
        "result": analysis_result_payload(result),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def save_pending_prediction(config: DashboardConfig, token: str) -> dict[str, Any]:
    result = PENDING_PREDICTIONS.pop(token, None)
    if result is None:
        raise DataError("prediction token not found or already saved")
    path = SnapshotStore(config.snapshot_root).save(result)
    return {
        "saved": True,
        "snapshot_path": str(path),
        "result": analysis_result_payload(result),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "OkoooDashboard/1.0"
    client_disconnect_errors = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)

    @property
    def config(self) -> DashboardConfig:
        return self.server.config  # type: ignore[attr-defined, no-any-return]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[dashboard] {self.address_string()} - {fmt % args}\n")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except self.client_disconnect_errors:
            return

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            raw = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "file not found")
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
        except self.client_disconnect_errors:
            return

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DataError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise DataError("JSON body must be an object")
        return payload

    def handle_error(self, exc: Exception) -> None:
        if isinstance(exc, self.client_disconnect_errors):
            return
        status = HTTPStatus.BAD_REQUEST if isinstance(exc, DataError) else HTTPStatus.INTERNAL_SERVER_ERROR
        if not isinstance(exc, DataError):
            traceback.print_exc()
        self.send_json({"ok": False, "error": str(exc)}, status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/dashboard", "/index.html"):
                self.send_file(self.config.html_path, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/dashboard":
                query = parse_qs(parsed.query)
                scores_path = self.config.scores_json
                if query.get("scores_json"):
                    scores_path = Path(query["scores_json"][0]).expanduser()
                self.send_json({"ok": True, "data": build_dashboard_payload(self.config.snapshot_root, scores_path)})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self.read_json_body()
            if parsed.path == "/api/predict":
                match_id = payload.get("match_id", payload.get("event_id"))
                if match_id is None:
                    raise DataError("match_id is required")
                data = predict_latest(self.config, int(match_id), save_snapshot=bool(payload.get("save_snapshot")))
                self.send_json({"ok": True, "data": data})
                return
            if parsed.path == "/api/save-prediction":
                token = str(payload.get("token") or "").strip()
                if not token:
                    raise DataError("token is required")
                self.send_json({"ok": True, "data": save_pending_prediction(self.config, token)})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self.handle_error(exc)


class DashboardServer(ThreadingHTTPServer):
    config: DashboardConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve an Okooo snapshot/validate dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--snapshot-dir", default=None, help="默认仓库根 .okooo_snapshots 或 OKOOO_SNAPSHOT_DIR")
    parser.add_argument("--scores-json", type=Path, default=None, help="默认 tools/okooo_validate_scores.json")
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML_PATH)
    parser.add_argument("--issue", default=OKOOO_DEFAULT_ISSUE)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--no-dotenv", action="store_true")
    parser.add_argument("--cookie-file", default=None)
    parser.add_argument("--detail-max-pages", type=int, default=5)
    parser.add_argument("--no-trade-trend", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.no_dotenv:
        load_dotenv_file(args.env_file.expanduser() if args.env_file else ROOT / ".env")
    snapshot_root = Path(resolve_okooo_snapshot_dir(args.snapshot_dir)).expanduser()
    config = DashboardConfig(
        snapshot_root=snapshot_root,
        scores_json=args.scores_json.expanduser() if args.scores_json else None,
        html_path=args.html.expanduser(),
        issue=args.issue,
        timeout=args.timeout,
        cookie_file=args.cookie_file,
        trade_trend=not args.no_trade_trend,
        detail_max_pages=args.detail_max_pages,
    )
    server = DashboardServer((args.host, args.port), DashboardHandler)
    server.config = config
    print(f"Okooo dashboard: http://{args.host}:{server.server_port}/")
    print(f"snapshot_dir: {config.snapshot_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
