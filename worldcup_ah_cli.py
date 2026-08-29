#!/usr/bin/env python3
"""World Cup Asian handicap helper based on public SPDEX data.

This tool is a decision aid, not a betting guarantee. It uses public endpoints
observed from SPDEX's web app and intentionally keeps the data layer isolated so
commercial or official data providers can be added later.
"""

from __future__ import annotations

import argparse
from difflib import SequenceMatcher
import hashlib
import html as html_lib
import http.client
import json
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from asian_handicap_validation import normalize_asian_decimal_odds


SPDEX_BASE_URL = "https://app.spdex.com/spdexapi"
SPDEX_WEB_BASE_URL = "https://app.spdex.com"
CHUQI_BIFA_LIST_URL = "https://www.chuqi.com/data_channel/bifa/"
CHUQI_BIFA_DETAIL_URL = "https://live.chuqi.com/football/live-bifa/{match_id}/"
CHINA_TZ = timezone(timedelta(hours=8))
WORLD_CUP_LEAGUE_ID = 911
_ENV_VAR_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_COOKIE_HEADER_RE = re.compile(r"cookie\s*:\s*([^'\r\n]+)", re.IGNORECASE)

WEIGHTS = {
    "bifa": 0.16,
    "bifa_trade": 0.08,
    "asian_handicap": 0.13,
    "euro_kelly": 0.07,
    "market_balance": 0.07,
    "draw_risk": 0.07,
    "fair_line": 0.05,
    "bookmaker_consensus": 0.05,
    "depth_profile": 0.03,
    "cover_risk": 0.05,
    "snapshot_trend": 0.06,
    "market_elasticity": 0.05,
    "external_consensus": 0.04,
    "water_value": 0.032,
    "data_quality": 0.05,
}

OKOOO_INTERNAL_SIGNAL_NAMES = {
    "盘口合理性",
    "公司一致性",
    "盘口深度/打穿能力",
    "外部赔率/实力校验",
    "高低水价值",
}
OKOOO_PURCHASE_INTERNAL_SIGNAL_NAMES = {
    "高低水价值",
}

OKOOO_SCORING_WEIGHT_OVERRIDES = {
    "必发指数": 0.0,
    "必发成交走势": 0.135,
    "亚盘水位": 0.50,
    "欧赔/Kelly": 0.085,
    "市场平衡/背离": 0.065,
    "平局风险": 0.010,
    "盘口合理性": 0.075,
    "公司一致性": 0.0,
    "盘口深度/打穿能力": 0.020,
    "赢盘门槛风险": 0.095,
    "快照趋势": 0.040,
    "资金/盘口弹性": 0.065,
    "高低水价值": 0.016,
    "外部赔率/实力校验": 0.020,
}

UPPER_THRESHOLD = 0.12
LOWER_THRESHOLD = -0.12
LEAN_THRESHOLD = 0.05
PURCHASE_LAYER_MOMENTUM_COEFF = 0.14
MODEL_DIRECTION_EPSILON = 0.015
STRONG_THRESHOLD = 0.25
MODEL_VERSION = "okooo-ah-fair-line-v9-2026-07-01"

TOP_BOOKMAKERS = ("PinnacleSports", "Bet365", "Singbet", "IBC", "Ysb88")
MATCH_LIST_HOT_MODES = (1, None)
SNAPSHOT_DIR_NAME = ".spdex_snapshots"
SCHEDULE_WINDOWS = (
    ("T-24h 建立基线", timedelta(hours=24), True),
    ("T-8h 观察盘口", timedelta(hours=8), True),
    ("T-4h 追踪热度/盘口修正", timedelta(hours=4), True),
    ("T-2h 追踪临场资金变化", timedelta(hours=2), True),
    ("T-60m 首次正式推荐", timedelta(minutes=60), True),
    ("T-30m 复核", timedelta(minutes=30), True),
)


def model_source_fingerprint() -> str:
    """Fingerprint the frozen scoring implementation written into new snapshots."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def default_env_file_path() -> Path:
    return Path(__file__).resolve().parent / ".env"


def load_dotenv_file(path: Path) -> int:
    """Parse KEY=value lines into os.environ. Does not override existing variables.

    Returns the number of variables newly set from non-empty values.
    """
    if not path.is_file():
        return 0
    set_count = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not _ENV_VAR_KEY_RE.match(key):
            continue
        if key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value == "":
            continue
        os.environ[key] = value
        set_count += 1
    return set_count


def normalize_cookie_value(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    header_match = _COOKIE_HEADER_RE.search(text)
    if header_match:
        text = header_match.group(1).strip()
    elif text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return " ".join(part.strip() for part in text.splitlines() if part.strip())


def mask_secret(value: str, visible: int = 6) -> str:
    value = value.strip()
    if len(value) <= visible * 2:
        return "*" * len(value)
    return f"{value[:visible]}...{value[-visible:]}"


def set_env_file_value(path: Path, key: str, value: str) -> None:
    line = f"{key}={value}\n"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    updated = False
    for index, existing in enumerate(lines):
        stripped = existing.strip()
        if stripped.startswith("#"):
            continue
        candidate = stripped[7:].strip() if stripped.lower().startswith("export ") else stripped
        if candidate.startswith(f"{key}="):
            lines[index] = line
            updated = True
            break
    if not updated:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = f"{lines[-1]}\n"
        lines.append(line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


def warn_if_credentials_without_cookie() -> None:
    """new.spdex login API requires captcha; USERNAME/PASSWORD alone cannot populate session."""
    user = os.environ.get("USERNAME", "").strip()
    password = os.environ.get("PASSWORD", "").strip()
    if not user or not password:
        return
    if os.environ.get("SPDEX_COOKIE", "").strip() or os.environ.get("SPDEX_AUTHORIZATION", "").strip():
        return
    print(
        "提示: .env 中已设置 USERNAME/PASSWORD，但 new.spdex 登录接口需要验证码，"
        "本脚本不会自动完成登录。请在浏览器登录后把对 *.spdex.com 生效的 Cookie 写入 SPDEX_COOKIE=...，"
        "或设置 SPDEX_AUTHORIZATION（若接口返回 Bearer）。",
        file=sys.stderr,
    )


def apply_spdex_auth_headers(target: dict[str, str]) -> None:
    """Merge SPDEX_COOKIE / SPDEX_AUTHORIZATION from the environment into request headers."""
    cookie = os.environ.get("SPDEX_COOKIE", "").strip()
    if cookie:
        target["Cookie"] = cookie
    auth = os.environ.get("SPDEX_AUTHORIZATION", "").strip()
    if auth:
        if auth.lower().startswith("bearer "):
            target["Authorization"] = auth
        else:
            target["Authorization"] = f"Bearer {auth}"


class DataError(RuntimeError):
    """Raised when a data source cannot return usable data."""


def looks_like_spdex_login_page(body: str) -> bool:
    sample = body[:5000].lower()
    return any(
        marker in sample or marker in body
        for marker in (
            "会员登录",
            "login-page",
            'path":"/login"',
            "/login",
            "login-submit",
        )
    )


def spdex_login_data_error(
    url: str,
    auth_configured: bool,
    *,
    cached: bool = False,
    first_url: str | None = None,
) -> DataError:
    if cached:
        source = f" 首次登录页来源: {first_url}." if first_url else ""
        prefix = f"SPDEX login required from prior request: {url}.{source}"
    else:
        prefix = f"SPDEX returned login page instead of JSON: {url}."
    if auth_configured:
        return DataError(
            f"{prefix} 已配置 SPDEX_COOKIE / SPDEX_AUTHORIZATION，但会话可能过期或不适用于 app.spdex.com；"
            "请在浏览器重新登录后更新 .env 中的 SPDEX_COOKIE。"
        )
    return DataError(
        f"{prefix} app.spdex.com 现在要求登录会话；请在浏览器登录后把 Cookie 写入 .env 的 "
        "SPDEX_COOKIE=...，然后重新运行 auth-probe / predict。"
    )


def non_json_data_error(url: str, body: str, auth_configured: bool) -> DataError:
    if looks_like_spdex_login_page(body):
        return spdex_login_data_error(url, auth_configured)
    compact = " ".join(body[:220].split())
    return DataError(f"SPDEX returned non-JSON data: {url}; preview={compact!r}")


def raise_if_spdex_error_json_payload(url: str, payload: Any) -> None:
    """Raise DataError when the server returns JSON error objects instead of API data."""
    if not isinstance(payload, dict):
        return
    if payload.get("error") is True and (
        "statusCode" in payload or "statusMessage" in payload or "message" in payload
    ):
        msg = payload.get("statusMessage") or payload.get("message") or "unknown"
        raise DataError(
            f"SPDEX 返回错误 JSON（常见于 app.spdex.com 302 至 new.spdex.com 后旧 /spdexapi 路径未再提供数据）"
            f" {url}: {msg}"
        )
    if payload.get("code") == 403 and payload.get("message") == "该接口暂未开放":
        raise DataError(f"SPDEX 接口拒绝访问 {url}: {payload.get('message')}")
    code = payload.get("code")
    if code not in (None, 0, "0") and ("data" in payload or "message" in payload):
        message = payload.get("message") or payload.get("msg") or payload.get("statusMessage") or "unknown"
        raise DataError(f"SPDEX API 返回错误 {url}: code={code}, message={message}")


_LIST_PAYLOAD_KEYS = (
    "data",
    "Data",
    "rows",
    "Rows",
    "list",
    "List",
    "items",
    "Items",
    "matches",
    "Matches",
    "matchList",
    "MatchList",
    "result",
    "Result",
)


def find_list_payload(data: Any, depth: int = 3) -> list[Any] | None:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict) or depth <= 0:
        return None
    for key in _LIST_PAYLOAD_KEYS:
        value = data.get(key)
        if isinstance(value, list):
            return value
    for key in _LIST_PAYLOAD_KEYS:
        nested = find_list_payload(data.get(key), depth - 1)
        if nested is not None:
            return nested
    return None


def require_list_payload(data: Any, source: str) -> list[Any]:
    payload = find_list_payload(data)
    if payload is not None:
        return payload
    if isinstance(data, dict):
        keys = ", ".join(str(key) for key in data.keys())
        raise DataError(f"SPDEX {source} returned an unexpected shape: dict keys=[{keys}]")
    raise DataError(f"SPDEX {source} returned an unexpected shape: {type(data).__name__}")


@dataclass(frozen=True)
class Match:
    event_id: int
    match_time: datetime
    home: str
    away: str
    league_id: int | None
    league_name: str
    asian_line: str
    is_stop_update: bool
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HandicapRow:
    bookmaker_id: int
    name: str
    sec_a: float
    sec_b: float
    init_sec_a: float
    init_sec_b: float
    payout: float
    update_time: datetime | None
    source: str = "live"
    init_line: float = 0.0
    latest_line: float = 0.0
    priority_hint: int | None = None
    init_line_known: bool = False
    latest_line_known: bool = False


def handicap_row_to_dict(row: HandicapRow) -> dict[str, Any]:
    return {
        "bookmaker_id": row.bookmaker_id,
        "name": row.name,
        "sec_a": row.sec_a,
        "sec_b": row.sec_b,
        "init_sec_a": row.init_sec_a,
        "init_sec_b": row.init_sec_b,
        "payout": row.payout,
        "update_time": row.update_time.isoformat() if row.update_time else None,
        "source": row.source,
        "init_line": row.init_line,
        "latest_line": row.latest_line,
        "priority_hint": row.priority_hint,
        "init_line_known": row.init_line_known,
        "latest_line_known": row.latest_line_known,
    }


def handicap_row_from_dict(item: dict[str, Any]) -> HandicapRow:
    source = str(item.get("source", "live"))
    legacy_okooo_row = source == "okooo"
    return HandicapRow(
        bookmaker_id=int(float_or_zero(item.get("bookmaker_id"))),
        name=str(item.get("name", "")),
        sec_a=float_or_zero(item.get("sec_a")),
        sec_b=float_or_zero(item.get("sec_b")),
        init_sec_a=float_or_zero(item.get("init_sec_a")),
        init_sec_b=float_or_zero(item.get("init_sec_b")),
        payout=float_or_zero(item.get("payout")),
        update_time=parse_datetime_or_none(item.get("update_time")),
        source=source,
        init_line=float_or_zero(item.get("init_line")),
        latest_line=float_or_zero(item.get("latest_line")),
        priority_hint=to_int_or_none(item.get("priority_hint")),
        init_line_known=bool(item.get("init_line_known", legacy_okooo_row and "init_line" in item)),
        latest_line_known=bool(item.get("latest_line_known", legacy_okooo_row and "latest_line" in item)),
    )


def handicap_init_line_known(row: HandicapRow) -> bool:
    return row.init_line_known or abs(row.init_line) > 1e-9


def handicap_latest_line_known(row: HandicapRow) -> bool:
    return row.latest_line_known or abs(row.latest_line) > 1e-9


def handicap_line_pair_known(row: HandicapRow) -> bool:
    return handicap_init_line_known(row) and handicap_latest_line_known(row)


@dataclass(frozen=True)
class PriceVolumePoint:
    price: float
    volume: float
    update_time: datetime | None
    attr: str | None


@dataclass(frozen=True)
class ChuqiMatchRef:
    match_id: int
    match_time: datetime
    home: str
    away: str
    league_name: str
    status: str = ""
    is_finished: bool = False


@dataclass(frozen=True)
class EuroTrendPoint:
    refresh_time: datetime | None
    home_price: float
    draw_price: float
    away_price: float
    home_kelly: float
    draw_kelly: float
    away_kelly: float


def price_volume_point_to_dict(point: PriceVolumePoint) -> dict[str, Any]:
    return {
        "price": point.price,
        "volume": point.volume,
        "update_time": point.update_time.isoformat() if point.update_time else None,
        "attr": point.attr,
    }


def price_volume_point_from_dict(item: dict[str, Any]) -> PriceVolumePoint:
    return PriceVolumePoint(
        price=float_or_zero(item.get("price")),
        volume=float_or_zero(item.get("volume")),
        update_time=parse_datetime_or_none(item.get("update_time")),
        attr=item.get("attr"),
    )


def euro_trend_point_to_dict(point: EuroTrendPoint) -> dict[str, Any]:
    return {
        "refresh_time": point.refresh_time.isoformat() if point.refresh_time else None,
        "home_price": point.home_price,
        "draw_price": point.draw_price,
        "away_price": point.away_price,
        "home_kelly": point.home_kelly,
        "draw_kelly": point.draw_kelly,
        "away_kelly": point.away_kelly,
    }


def euro_trend_point_from_dict(item: dict[str, Any]) -> EuroTrendPoint:
    return EuroTrendPoint(
        refresh_time=parse_datetime_or_none(item.get("refresh_time")),
        home_price=float_or_zero(item.get("home_price")),
        draw_price=float_or_zero(item.get("draw_price")),
        away_price=float_or_zero(item.get("away_price")),
        home_kelly=float_or_zero(item.get("home_kelly")),
        draw_kelly=float_or_zero(item.get("draw_kelly")),
        away_kelly=float_or_zero(item.get("away_kelly")),
    )


@dataclass
class Signal:
    name: str
    score: float
    weight: float
    available: bool
    reason: str


SIGNAL_SUMMARY_FOCUS = {
    "必发指数": "必发静态热度、成交额、盈亏和价格是否互相确认",
    "必发成交走势": "临场成交速率、价量变化和两边资金节奏",
    "亚盘水位": "去水后的等价公平盘口变化、公司一致性和路径持续性",
    "欧赔/Kelly": "欧赔变化和 Kelly 风险是否确认同一方向",
    "市场平衡/背离": "必发热度、盘口、欧赔、成交等信号是否同向",
    "平局风险": "平局或小胜对上盘赢盘的拖累",
    "盘口合理性": "内部计算：实际盘口相对赔率估盘口是否过深或偏浅",
    "公司一致性": "主流公司等价公平盘口变化是否形成可信共识",
    "盘口深度/打穿能力": "当前盘口深度下，上盘是否具备打穿条件",
    "赢盘门槛风险": "上盘打穿盘口所需确认是否不足",
    "快照趋势": "本地滚动快照里的 score、热度、水位和盘口变化",
    "资金/盘口弹性": "资金热度出现后，等价公平盘口是否有对应反应",
    "外部赔率/实力校验": "外部赔率、合理盘口或实力模型是否提供校验",
    "高低水价值": "当前水位相对模型概率是否有赔率补偿",
    "临场score变化": "本轮综合分相对历史快照的临场动能",
    "数据质量": "可用信号覆盖度对置信度的支撑程度",
}


def signal_strength_label(score: float) -> str:
    abs_score = abs(score)
    if abs_score >= 0.55:
        return "强烈"
    if abs_score >= 0.30:
        return "中等"
    if abs_score >= 0.12:
        return "轻微"
    return "非常轻微"


def signal_summary_text(signal: Signal, upper_team: str, lower_team: str) -> str:
    focus = SIGNAL_SUMMARY_FOCUS.get(signal.name, "综合评分方向")
    if not signal.available:
        return f"总结：数据不可用，本项不参与加权；看点：{focus}。"

    if signal.name == "数据质量":
        coverage = abs(signal.score)
        if coverage >= 0.85:
            quality = "覆盖度较高"
        elif coverage >= 0.65:
            quality = "覆盖度基本可用"
        else:
            quality = "覆盖度偏低"
        return f"总结：{quality}，主要影响置信度稳定性，不单独代表上下盘价值；看点：{focus}。"

    if signal.name == "平局风险":
        if abs(signal.score) < 0.05:
            return f"总结：平局拖累不明显，对上下盘没有明显推动；看点：{focus}。"
        strength = signal_strength_label(signal.score)
        if signal.score < 0:
            return f"总结：{strength}压制上盘({upper_team})，平局或小胜会削弱赢盘确定性；看点：{focus}。"
        return f"总结：{strength}缓解上盘({upper_team})风险，胜率缓冲相对足；看点：{focus}。"

    if signal.name == "赢盘门槛风险":
        if abs(signal.score) < 0.05:
            return f"总结：赢盘门槛风险不明显，对上下盘没有明显推动；看点：{focus}。"
        strength = signal_strength_label(signal.score)
        if signal.score < 0:
            return f"总结：{strength}压制上盘({upper_team})，说明打穿盘口所需确认不足或风险堆积；看点：{focus}。"
        return f"总结：{strength}支持上盘({upper_team})，打穿门槛风险暂不突出；看点：{focus}。"

    if abs(signal.score) < 0.05:
        return f"总结：基本中性，对上下盘没有明显推动；看点：{focus}。"

    strength = signal_strength_label(signal.score)
    direction = "上盘" if signal.score > 0 else "下盘"
    team = upper_team if signal.score > 0 else lower_team
    return f"总结：{strength}偏{direction}({team})，该指标正在向{direction}提供证据；看点：{focus}。"


@dataclass(frozen=True)
class ScheduledTask:
    key: str
    label: str
    run_at: datetime
    match: Match
    do_predict: bool
    is_catch_up: bool = False


@dataclass(frozen=True)
class SnapshotContext:
    records: list[dict[str, Any]]
    first_metrics: dict[str, float]
    last_metrics: dict[str, float]
    signal_history_score: float
    signal_history_reason: str

    @property
    def available(self) -> bool:
        return len(self.records) >= 2

    @property
    def heat_delta(self) -> float:
        return self.last_metrics.get("heat_edge", 0.0) - self.first_metrics.get("heat_edge", 0.0)

    @property
    def upper_water_delta(self) -> float:
        return self.last_metrics.get("upper_water", 0.0) - self.first_metrics.get("upper_water", 0.0)

    @property
    def lower_water_delta(self) -> float:
        return self.last_metrics.get("lower_water", 0.0) - self.first_metrics.get("lower_water", 0.0)

    @property
    def line_depth_delta(self) -> float:
        return self.last_metrics.get("line_depth", 0.0) - self.first_metrics.get("line_depth", 0.0)


@dataclass
class AnalysisResult:
    match: Match
    recommendation: str
    score: float
    confidence: int
    completeness: int
    upper_team: str
    lower_team: str
    signals: list[Signal]
    warnings: list[str]
    model_recommendation: str = ""
    model_confidence: int = 0
    purchase_score: float = 0.0
    decision_reason: str = ""
    is_reversed: bool = False
    review2: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.model_recommendation:
            self.model_recommendation = self.recommendation
        if self.purchase_score == 0.0:
            self.purchase_score = self.score
        if not self.decision_reason:
            self.decision_reason = "按综合分数直接推荐"

    @property
    def lean(self) -> str:
        if self.score < 0:
            return "下盘"
        return "上盘"

    @property
    def lean_team(self) -> str:
        if self.lean == "下盘":
            return self.lower_team
        return self.upper_team

    @property
    def strength(self) -> str:
        return score_strength_label(self.score)

    @property
    def purchase_side(self) -> str:
        if self.recommendation in ("上盘", "下盘"):
            return self.recommendation
        return recommendation_from_score(self.score)

    @property
    def purchase_team(self) -> str:
        if self.purchase_side == "上盘":
            return self.upper_team
        if self.purchase_side == "下盘":
            return self.lower_team
        return self.lean_team

    @property
    def purchase_raw_price(self) -> float | None:
        key = side_key(self.match, self.purchase_team)
        if key not in ("Home", "Away"):
            return None
        value = to_float_or_none(self.match.raw.get(f"AsianAvr{key}"))
        return value if value is not None and value > 0 else None

    @property
    def purchase_decimal_odds(self) -> float | None:
        return normalize_asian_decimal_odds(self.purchase_raw_price)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_version": MODEL_VERSION,
            "model_fingerprint": model_source_fingerprint(),
            "event_id": self.match.event_id,
            "match": f"{self.match.home} vs {self.match.away}",
            "match_time": self.match.match_time.isoformat(),
            "asian_line": self.match.asian_line,
            "upper_team": self.upper_team,
            "lower_team": self.lower_team,
            "recommendation": self.recommendation,
            "purchase_side": self.purchase_side,
            "purchase_team": self.purchase_team,
            "purchase_raw_price": self.purchase_raw_price,
            "purchase_decimal_odds": self.purchase_decimal_odds,
            "purchase_score": round(self.purchase_score, 4),
            "model_recommendation": self.model_recommendation,
            "model_confidence": self.model_confidence,
            "decision_reason": self.decision_reason,
            "is_reversed": self.is_reversed,
            "lean": self.lean,
            "lean_team": self.lean_team,
            "strength": self.strength,
            "score": round(self.score, 4),
            "confidence": self.confidence,
            "completeness": self.completeness,
            "signals": [
                {
                    "name": signal.name,
                    "score": round(signal.score, 4),
                    "weight": signal.weight,
                    "available": signal.available,
                    "reason": signal.reason,
                    "summary": signal_summary_text(signal, self.upper_team, self.lower_team),
                }
                for signal in self.signals
            ],
            "warnings": self.warnings,
            "review2": self.review2,
        }


@dataclass(frozen=True)
class PurchaseDecision:
    side: str
    score: float
    confidence: int
    reason: str
    is_reversed: bool


def strip_html_tags(value: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def normalize_match_name(value: str) -> str:
    text = strip_html_tags(value)
    text = re.sub(r"\([^)]*\)|（[^）]*）", "", text)
    text = re.sub(r"[\s·\-._/]+", "", text)
    return text.strip().lower()


def team_similarity(left: str, right: str) -> float:
    a = normalize_match_name(left)
    b = normalize_match_name(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def parse_percent_value(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    try:
        return float(text)
    except ValueError:
        return 0.0


def extract_js_array_after(text: str, marker: str) -> str | None:
    marker_pos = text.find(marker)
    if marker_pos < 0:
        return None
    start = text.find("[", marker_pos)
    if start < 0:
        return None
    depth = 0
    in_string: str | None = None
    escaped = False
    for index in range(start, len(text)):
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == in_string:
                in_string = None
            continue
        if ch in ("'", '"'):
            in_string = ch
            continue
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def js_literal_to_json_text(value: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", value, flags=re.S)
    text = re.sub(r"(^|[\s:\[,])(-?)\.(\d+)", r"\1\g<2>0.\3", text)
    text = re.sub(r"(?<=[{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", r'"\1":', text)
    text = re.sub(r"\b(undefined|NaN)\b", "null", text)

    def single_quote_repl(match: re.Match[str]) -> str:
        inner = match.group(1).replace("\\'", "'")
        return json.dumps(inner, ensure_ascii=False)

    text = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", single_quote_repl, text)
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return text


def loads_js_literal(value: str) -> Any:
    return json.loads(js_literal_to_json_text(value))


def parse_chuqi_timestamp(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    else:
        text = str(value).strip()
        try:
            timestamp = float(text)
        except ValueError:
            return parse_datetime_or_none(text)
    if timestamp > 10_000_000_000:
        timestamp /= 1000.0
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, ValueError):
        return None


def parse_chuqi_match_time_from_md(day: datetime, hhmm: str) -> datetime | None:
    match = re.match(r"^(\d{2}):(\d{2})$", hhmm.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=CHINA_TZ).astimezone(timezone.utc)


def parse_chuqi_match_refs(page_html: str, day: datetime) -> list[ChuqiMatchRef]:
    blocks = [
        match.group(0)
        for match in re.finditer(
            r"<li\b[^>]*class=[\"'][^\"']*channel-item[^\"']*[\"'][^>]*>.*?</li>",
            page_html,
            flags=re.S | re.I,
        )
    ]
    if not blocks:
        seen_windows: set[int] = set()
        for match in re.finditer(r"/football/live-bifa/(\d+)/?", page_html):
            start = max(0, match.start() - 1800)
            end = min(len(page_html), match.end() + 2400)
            if start in seen_windows:
                continue
            seen_windows.add(start)
            blocks.append(page_html[start:end])

    refs: list[ChuqiMatchRef] = []
    seen_ids: set[int] = set()
    for block in blocks:
        id_match = re.search(r"/football/live-bifa/(\d+)/?", block)
        if not id_match:
            continue
        match_id = int(id_match.group(1))
        if match_id in seen_ids:
            continue
        names = [
            strip_html_tags(item)
            for item in re.findall(
                r"<p\b[^>]*class=[\"'][^\"']*name\s+ellipsis[^\"']*[\"'][^>]*>(.*?)</p>",
                block,
                flags=re.S | re.I,
            )
        ]
        if len(names) < 2:
            names = [
                strip_html_tags(item)
                for item in re.findall(
                    r"<(?:p|span|div)\b[^>]*class=[\"'][^\"']*(?:team|name)[^\"']*[\"'][^>]*>(.*?)</(?:p|span|div)>",
                    block,
                    flags=re.S | re.I,
                )
                if strip_html_tags(item)
            ]
        if len(names) < 2:
            continue
        time_match = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*date[^\"']*[\"'][^>]*>(\d{2}:\d{2})</span>",
            block,
            flags=re.S | re.I,
        )
        if not time_match:
            time_match = re.search(r"(\d{2}:\d{2})", strip_html_tags(block))
        match_time = parse_chuqi_match_time_from_md(day, time_match.group(1)) if time_match else None
        if match_time is None:
            continue
        league_match = re.search(
            r"<span\b[^>]*class=[\"'][^\"']*type[^\"']*[\"'][^>]*>(.*?)</span>",
            block,
            flags=re.S | re.I,
        )
        league_name = strip_html_tags(league_match.group(1)) if league_match else ""
        status_match = re.search(
            r"<p\b[^>]*class=[\"'][^\"']*status[^\"']*[\"'][^>]*>(.*?)</p>",
            block,
            flags=re.S | re.I,
        )
        status = strip_html_tags(status_match.group(1)) if status_match else ""
        data_status_match = re.search(r"data-status=[\"']?([^\"'\s>]+)", block, flags=re.I)
        data_status = data_status_match.group(1) if data_status_match else ""
        is_finished = data_status in {"30", "finished", "end"} or any(
            marker in status for marker in ("完", "结束", "赛果")
        )
        refs.append(
            ChuqiMatchRef(
                match_id=match_id,
                match_time=match_time,
                home=names[0],
                away=names[1],
                league_name=league_name,
                status=status,
                is_finished=is_finished,
            )
        )
        seen_ids.add(match_id)
    return refs


def parse_chuqi_all_data(page_html: str) -> list[dict[str, Any]]:
    array_text = extract_js_array_after(page_html, "allData")
    if not array_text:
        raise DataError("Chuqi live-bifa page does not contain allData")
    data = loads_js_literal(array_text)
    if not isinstance(data, list):
        raise DataError("Chuqi allData is not a list")
    return [item for item in data if isinstance(item, dict)]


def _chuqi_detail_teams(page_html: str, fallback: ChuqiMatchRef | None) -> tuple[str, str]:
    if fallback:
        return fallback.home, fallback.away
    title_tag = re.search(r"<title>(.*?)</title>", page_html, flags=re.S | re.I)
    if title_tag:
        title = strip_html_tags(title_tag.group(1))
        title_match = re.search(
            r"(?:^|[-_|])\s*([^_\-|]+?)\s+(?:VS|vs|v)\s+([^_\-|]+?)(?:\s*[-_|]|$)",
            title,
        )
        if title_match:
            return strip_html_tags(title_match.group(1)), strip_html_tags(title_match.group(2))
        title_match = re.search(r"^(.+?)\s+(?:VS|vs|v)\s+(.+?)(?:\s*[-_|]|$)", title)
        if title_match:
            return strip_html_tags(title_match.group(1)), strip_html_tags(title_match.group(2))
    names = [
        strip_html_tags(item)
        for item in re.findall(
            r"<p\b[^>]*class=[\"'][^\"']*team-name[^\"']*[\"'][^>]*>.*?<a\b[^>]*>(.*?)</a>",
            page_html,
            flags=re.S | re.I,
        )
        if strip_html_tags(item) and not re.match(r"^\[[^\]]+\]$", strip_html_tags(item))
    ]
    if len(names) >= 2:
        return names[0], names[1]
    return "", ""


def chuqi_series_rows(row: dict[str, Any], summary_price: float) -> list[dict[str, Any]]:
    source = row.get("echart")
    if not isinstance(source, list) or len(source) < 2:
        source = row.get("detail")
    if not isinstance(source, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in source:
        if not isinstance(item, dict):
            continue
        update_time = parse_chuqi_timestamp(item.get("time"))
        amount = amount_to_float(item.get("amount"))
        price = to_float_or_none(item.get("odds")) or summary_price
        if amount <= 0:
            continue
        rows.append(
            {
                "price": price,
                "volume": amount,
                "time": update_time.isoformat() if update_time else None,
            }
        )
    return rows


def parse_chuqi_bifa_detail(match_id: int, page_html: str, ref: ChuqiMatchRef | None = None) -> Match:
    rows = parse_chuqi_all_data(page_html)
    home, away = _chuqi_detail_teams(page_html, ref)
    match_time = ref.match_time if ref else datetime.now(timezone.utc)
    league_name = ref.league_name if ref else "世界杯"
    is_finished = ref.is_finished if ref else False
    raw: dict[str, Any] = {
        "_source": "chuqi",
        "_bifa_source": "chuqi",
        "_chuqi_id": match_id,
        "EventId": match_id,
        "MatchTime": match_time.isoformat(),
        "HomeTeam": home,
        "AwayTeam": away,
        "SortName": league_name,
        "LeagueName": league_name,
        "MatchPath": league_name,
        "AsianAvrLet": "0",
    }
    trade_series: dict[str, list[dict[str, Any]]] = {}
    label_map = {
        "主": ("Home", "home"),
        "主胜": ("Home", "home"),
        "和": ("Draw", "draw"),
        "平": ("Draw", "draw"),
        "平局": ("Draw", "draw"),
        "客": ("Away", "away"),
        "客胜": ("Away", "away"),
    }
    for row in rows:
        label = str(row.get("name", "")).strip()
        mapped = label_map.get(label)
        if not mapped:
            continue
        legacy_key, selection = mapped
        summary = row.get("summary")
        if not isinstance(summary, dict):
            summary = {}
        odds = to_float_or_none(summary.get("odds")) or 0.0
        raw[f"BfOdds{legacy_key}"] = odds
        raw[f"BfAmount{legacy_key}"] = amount_to_float(summary.get("amount"))
        raw[f"BfIndex{legacy_key}"] = parse_percent_value(summary.get("per"))
        raw[f"BfPayout{legacy_key}"] = to_float_or_none(summary.get("payout")) or 0.0
        raw[f"BfProfit{legacy_key}"] = amount_to_float(summary.get("profit"))
        raw[f"BfHot{legacy_key}"] = to_float_or_none(summary.get("hot")) or 0.0
        series = chuqi_series_rows(row, odds)
        if series:
            trade_series[selection] = series
    if "BfIndexHome" not in raw or "BfIndexAway" not in raw:
        raise DataError("Chuqi live-bifa allData does not include home/away summary")
    raw["_chuqi_trade_series"] = trade_series
    raw["_chuqi_trade_point_count"] = sum(len(value) for value in trade_series.values())
    return Match(
        event_id=match_id,
        match_time=match_time,
        home=home,
        away=away,
        league_id=None,
        league_name=league_name,
        asian_line="0",
        is_stop_update=is_finished,
        raw=raw,
    )


def merge_chuqi_bifa_detail(base: Match, detail: Match) -> Match:
    raw = dict(base.raw)
    for key, value in detail.raw.items():
        if key == "_source" and raw.get("_source"):
            continue
        if key.startswith("Bf") or key.startswith("_chuqi") or key == "_bifa_source":
            if value not in (None, ""):
                raw[key] = value
    raw["_bifa_source"] = "chuqi"
    return replace(base, raw=raw)


def attach_chuqi_id(match: Match, chuqi_id: int) -> Match:
    raw = dict(match.raw)
    raw["_chuqi_id"] = int(chuqi_id)
    return replace(match, raw=raw)


def chuqi_trade_points_from_raw(raw: dict[str, Any], selection: str) -> list[PriceVolumePoint]:
    series = raw.get("_chuqi_trade_series")
    if not isinstance(series, dict):
        return []
    rows = series.get(selection)
    if not isinstance(rows, list):
        return []
    points: list[PriceVolumePoint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        points.append(
            PriceVolumePoint(
                price=to_float_or_none(row.get("price")) or 0.0,
                volume=amount_to_float(row.get("volume")),
                update_time=parse_datetime_or_none(row.get("time")),
                attr="chuqi",
            )
        )
    return points


class ChuqiBifaClient:
    """No-login Chuqi live-bifa reader used as a Betfair/必发 fallback source."""

    def __init__(self, timeout: float = 8.0, curl_fallback: bool = True):
        self.timeout = timeout
        self.curl_fallback = curl_fallback
        self.curl_fallback_used = False

    def _request_headers(self) -> dict[str, str]:
        return {
            "User-Agent": "worldcup-ah-cli/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
        }

    def _get_text(self, url: str) -> str:
        request = urllib.request.Request(url, headers=self._request_headers())
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            if not self.curl_fallback:
                raise DataError(f"Chuqi request failed: {url}: {exc}") from exc
            raw = self._curl_bytes(url)
        for encoding in ("utf-8", "gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    def _curl_bytes(self, url: str) -> bytes:
        curl = shutil.which("curl")
        if not curl:
            raise DataError("curl is not installed")
        command = [
            curl,
            "-L",
            "--compressed",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, int(self.timeout))),
            "-A",
            self._request_headers()["User-Agent"],
            url,
        ]
        try:
            result = subprocess.run(command, capture_output=True, check=True)
        except (subprocess.CalledProcessError, OSError) as exc:
            stderr = getattr(exc, "stderr", b"") or str(exc).encode()
            raise DataError(stderr.decode("utf-8", errors="replace").strip()) from exc
        self.curl_fallback_used = True
        return result.stdout

    def matches_for_date(self, day: datetime) -> list[ChuqiMatchRef]:
        local = day.astimezone(CHINA_TZ)
        url = f"{CHUQI_BIFA_LIST_URL}?type=0&kind=1&isHome&datetime={local.strftime('%m-%d')}"
        return parse_chuqi_match_refs(self._get_text(url), local)

    def world_cup_matches(self, now: datetime | None = None) -> list[Match]:
        now = now or datetime.now(timezone.utc)
        seen: set[int] = set()
        matches: list[Match] = []
        for offset in range(-1, 4):
            try:
                refs = self.matches_for_date(now + timedelta(days=offset))
            except DataError:
                continue
            for ref in refs:
                if ref.match_id in seen or "世界杯" not in ref.league_name:
                    continue
                seen.add(ref.match_id)
                matches.append(
                    Match(
                        event_id=ref.match_id,
                        match_time=ref.match_time,
                        home=ref.home,
                        away=ref.away,
                        league_id=None,
                        league_name=ref.league_name,
                        asian_line="0",
                        is_stop_update=ref.is_finished,
                        raw={
                            "_source": "chuqi",
                            "_bifa_source": "chuqi",
                            "_chuqi_id": ref.match_id,
                            "EventId": ref.match_id,
                            "MatchTime": ref.match_time.isoformat(),
                            "HomeTeam": ref.home,
                            "AwayTeam": ref.away,
                            "SortName": ref.league_name,
                            "LeagueName": ref.league_name,
                            "MatchPath": ref.league_name,
                            "AsianAvrLet": "0",
                        },
                    )
                )
        return sorted(matches, key=lambda item: item.match_time)

    def find_best_ref(self, match: Match) -> ChuqiMatchRef | None:
        local_day = match.match_time.astimezone(CHINA_TZ)
        best: tuple[float, ChuqiMatchRef] | None = None
        for offset in range(-1, 2):
            try:
                refs = self.matches_for_date(local_day + timedelta(days=offset))
            except DataError:
                continue
            for ref in refs:
                if "世界杯" not in ref.league_name:
                    continue
                direct = min(team_similarity(match.home, ref.home), team_similarity(match.away, ref.away))
                swapped = min(team_similarity(match.home, ref.away), team_similarity(match.away, ref.home))
                name_score = max(direct, swapped)
                if name_score < 0.58:
                    continue
                hours_delta = abs((match.match_time - ref.match_time).total_seconds()) / 3600
                if hours_delta > 18:
                    continue
                score = 0.82 * name_score + 0.18 * max(0.0, 1 - hours_delta / 18)
                if best is None or score > best[0]:
                    best = (score, ref)
        return best[1] if best else None

    def match_detail(self, match_id: int, ref: ChuqiMatchRef | None = None) -> Match:
        url = CHUQI_BIFA_DETAIL_URL.format(match_id=match_id)
        return parse_chuqi_bifa_detail(match_id, self._get_text(url), ref)

    def enrich_match(self, match: Match) -> Match | None:
        ref: ChuqiMatchRef | None = None
        chuqi_id = to_int_or_none(match.raw.get("_chuqi_id"))
        if chuqi_id is None:
            ref = self.find_best_ref(match)
            if ref is None:
                return None
            chuqi_id = ref.match_id
        detail = self.match_detail(chuqi_id, ref)
        if match.home and match.away and detail.home and detail.away:
            direct = min(team_similarity(match.home, detail.home), team_similarity(match.away, detail.away))
            if direct < 0.58:
                raise DataError(
                    f"楚旗必发详情与 SPDEX 队名不匹配: "
                    f"SPDEX={match.home} vs {match.away}; Chuqi={detail.home} vs {detail.away}"
                )
        return merge_chuqi_bifa_detail(match, detail)


class SpdexClient:
    """Small public SPDEX client using only the Python standard library."""

    def __init__(
        self,
        base_url: str = SPDEX_BASE_URL,
        timeout: float = 8.0,
        ssl_fallback: bool = True,
        retries: int = 1,
        curl_fallback: bool = True,
        *,
        use_env_auth: bool = True,
        extra_headers: dict[str, str] | None = None,
        chuqi_bifa: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ssl_fallback = ssl_fallback
        self.retries = max(0, retries)
        self.curl_fallback = curl_fallback
        self.ssl_fallback_used = False
        self.curl_fallback_used = False
        self.login_required_detected = False
        self.login_required_url: str | None = None
        self.chuqi = ChuqiBifaClient(timeout=timeout, curl_fallback=curl_fallback) if chuqi_bifa else None
        self._chuqi_trade_cache: dict[int, dict[str, list[PriceVolumePoint]]] = {}
        self._chuqi_enriched_cache: dict[int, Match] = {}
        self._extra_headers: dict[str, str] = {}
        if use_env_auth:
            apply_spdex_auth_headers(self._extra_headers)
        if extra_headers:
            self._extra_headers.update(extra_headers)

    @property
    def auth_configured(self) -> bool:
        return bool(self._extra_headers.get("Cookie") or self._extra_headers.get("Authorization"))

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": "worldcup-ah-cli/1.0",
            "Accept": "application/json,text/plain,*/*",
        }
        headers.update(self._extra_headers)
        return headers

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        query.setdefault("app", "a")
        query.setdefault("version", "1.01")
        query.setdefault("dateformat", "iso8601")
        return self._get_json_url(f"{self.base_url}{path}", query)

    def _get_web_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._get_json_url(f"{SPDEX_WEB_BASE_URL}{path}", dict(params or {}))

    def _get_json_url(self, base_url: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        url = f"{base_url}?{urllib.parse.urlencode(query)}" if query else base_url
        if self.login_required_detected:
            raise spdex_login_data_error(
                url,
                self.auth_configured,
                cached=True,
                first_url=self.login_required_url,
            )
        request = urllib.request.Request(url, headers=self._request_headers())
        body: str | None = None
        body_from_curl = False
        last_exc: BaseException | None = None
        network_errors = (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            http.client.HTTPException,
            OSError,
        )
        for attempt in range(self.retries + 1):
            try:
                body = self._open_text(request)
                break
            except network_errors as exc:
                if self.ssl_fallback and is_ssl_verify_error(exc):
                    self.ssl_fallback_used = True
                    try:
                        body = self._open_text(request, context=ssl._create_unverified_context())
                        break
                    except network_errors as fallback_exc:
                        last_exc = fallback_exc
                else:
                    last_exc = exc
                if isinstance(last_exc, urllib.error.HTTPError) and 400 <= last_exc.code < 500:
                    try:
                        raw = last_exc.read()
                        if raw:
                            body = raw.decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    break
                if attempt < self.retries:
                    time.sleep(0.35 * (attempt + 1))
        if body is None:
            if self.curl_fallback:
                try:
                    body = self._curl_text(url)
                    body_from_curl = True
                    self.curl_fallback_used = True
                except DataError as curl_exc:
                    raise DataError(f"SPDEX request failed: {url}: {last_exc}; curl fallback: {curl_exc}") from last_exc
            else:
                raise DataError(f"SPDEX request failed: {url}: {last_exc}") from last_exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            if self.curl_fallback and not body_from_curl:
                try:
                    curl_body = self._curl_text(url)
                    body_from_curl = True
                    self.curl_fallback_used = True
                    data = json.loads(curl_body)
                except json.JSONDecodeError as curl_json_exc:
                    body = curl_body
                    exc = curl_json_exc
                except DataError:
                    pass
                else:
                    raise_if_spdex_error_json_payload(url, data)
                    return data
            if looks_like_spdex_login_page(body):
                self.login_required_detected = True
                self.login_required_url = self.login_required_url or url
            raise non_json_data_error(url, body, self.auth_configured) from exc
        raise_if_spdex_error_json_payload(url, data)
        return data

    def _open_text(
        self,
        request: urllib.request.Request,
        context: ssl.SSLContext | None = None,
    ) -> str:
        with urllib.request.urlopen(request, timeout=self.timeout, context=context) as resp:
            return resp.read().decode("utf-8")

    def _curl_text(self, url: str) -> str:
        curl = shutil.which("curl")
        if not curl:
            raise DataError("curl is not installed")
        command = [
            curl,
            "-L",
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            str(max(1, int(self.timeout))),
            "--connect-timeout",
            str(max(1, min(5, int(self.timeout)))),
        ]
        cookie_header = self._extra_headers.get("Cookie")
        if cookie_header:
            # curl 跟随跨子域 302 时不会复用 -H Cookie:；用 -b 才能在 app→new 重定向链上持续携带会话
            command.extend(["-b", cookie_header])
        for hk, hv in self._extra_headers.items():
            if hk.lower() == "cookie":
                continue
            command.extend(["-H", f"{hk}: {hv}"])
        command.append(url)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, OSError) as exc:
            stderr = getattr(exc, "stderr", "") or str(exc)
            raise DataError(stderr.strip()) from exc
        return result.stdout

    def match_list(self, hot: int | None = None) -> list[Match]:
        params: dict[str, Any] = {"class": -1}
        if hot is not None:
            params["hot"] = hot
        data = self._get_json("/spdex/match_list", params)
        rows = require_list_payload(data, "match_list")
        return [parse_match(item) for item in rows if isinstance(item, dict)]

    def newspdex_matches(
        self,
        *,
        date: str = "today-window",
        status: str = "upcoming",
        page: int = 1,
        page_size: int = 200,
    ) -> list[Match]:
        params: dict[str, Any] = {
            "date": date,
            "status": status,
            "page": page,
            "pageSize": page_size,
        }
        data = self._get_web_json("/api/newspdex/matches", params)
        rows = require_list_payload(data, "newspdex matches")
        return [parse_newspdex_match(item) for item in rows if isinstance(item, dict)]

    def newspdex_match_detail(self, event_id: int) -> Match | None:
        data = self._get_web_json(f"/api/newspdex/match-detail/{event_id}")
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise DataError("SPDEX newspdex match-detail returned an unexpected shape")
        match_info = payload.get("match")
        if not isinstance(match_info, dict):
            return None
        match = parse_newspdex_match(match_info)
        raw = dict(match.raw)
        enrich_newspdex_detail_raw(raw, payload)
        return replace(match, raw=raw)

    def _match_detail_candidates(self, keyword: str) -> list[dict[str, Any]]:
        data = self._get_json(
            "/spdex/match_detail",
            {"keyword": keyword, "product_id": 0, "tutorial": 0},
        )
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            nested = data.get("data")
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
            if isinstance(nested, dict):
                return [nested]
            return [data]
        return []

    def match_detail(self, keyword: str) -> Match | None:
        for item in self._match_detail_candidates(keyword):
            try:
                return parse_match(item)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def find_match(self, event_id: int) -> Match:
        errors: list[DataError] = []
        base_match: Match | None = None
        try:
            for match in self.world_cup_matches():
                if match.event_id == event_id:
                    base_match = match
                    break
        except DataError as exc:
            errors.append(exc)
        try:
            detail = self.newspdex_match_detail(event_id)
            if detail:
                return merge_match_detail(base_match, detail) if base_match else detail
        except DataError as exc:
            errors.append(exc)
        if base_match:
            return base_match
        try:
            detail = self.match_detail(str(event_id))
            if detail:
                return detail
        except DataError as exc:
            errors.append(exc)
        if self.chuqi is not None:
            try:
                return self.chuqi.match_detail(event_id)
            except DataError as exc:
                errors.append(exc)
        if errors:
            raise errors[-1]
        raise DataError(f"cannot find event_id={event_id} in SPDEX")

    def world_cup_matches(self) -> list[Match]:
        seen: set[int] = set()
        matches: list[Match] = []
        successful_requests = 0
        last_error: DataError | None = None
        for date in newspdex_upcoming_dates():
            try:
                batch = self.newspdex_matches(date=date, status="upcoming")
            except DataError as exc:
                last_error = exc
                continue
            successful_requests += 1
            for match in batch:
                if match.event_id in seen or not is_world_cup(match):
                    continue
                seen.add(match.event_id)
                matches.append(match)
        if matches:
            return sorted(matches, key=lambda item: item.match_time)
        for hot in MATCH_LIST_HOT_MODES:
            try:
                batch = self.match_list(hot=hot)
            except DataError as exc:
                last_error = exc
                continue
            successful_requests += 1
            for match in batch:
                if match.event_id in seen or not is_world_cup(match):
                    continue
                seen.add(match.event_id)
                matches.append(match)
        if matches:
            return sorted(matches, key=lambda item: item.match_time)
        if self.chuqi is not None:
            try:
                chuqi_matches = self.chuqi.world_cup_matches()
                if chuqi_matches:
                    return chuqi_matches
            except DataError as exc:
                last_error = exc
        if successful_requests == 0:
            if last_error:
                raise last_error
            raise DataError("SPDEX match list is unavailable")
        return sorted(matches, key=lambda item: item.match_time)

    def enrich_with_chuqi_bifa(self, match: Match) -> Match:
        if self.chuqi is None:
            return match
        cached = self._chuqi_enriched_cache.get(match.event_id)
        if cached is not None:
            return merge_chuqi_bifa_detail(match, cached)
        enriched = self.chuqi.enrich_match(match)
        if enriched is None:
            return match
        self._chuqi_enriched_cache[match.event_id] = enriched
        selection_points: dict[str, list[PriceVolumePoint]] = {}
        for selection in ("home", "draw", "away"):
            points = chuqi_trade_points_from_raw(enriched.raw, selection)
            if points:
                selection_points[selection] = points
        if selection_points:
            self._chuqi_trade_cache[match.event_id] = selection_points
        return enriched

    def handicap_list(self, event_id: int, asian_line: str) -> list[HandicapRow]:
        data = self._get_json(
            "/spdex/odds/view/list",
            {
                "eid": event_id,
                "outcome": 3,
                "line": normalize_line_for_spdex(asian_line),
            },
        )
        rows = require_list_payload(data, "handicap list")
        return [parse_handicap(item) for item in rows if isinstance(item, dict)]

    def handicap_detail(
        self, event_id: int, asian_line: str, bookmaker_id: int
    ) -> list[HandicapRow]:
        data = self._get_json(
            "/spdex/odds/view/detail",
            {
                "eid": event_id,
                "outcome": 3,
                "line": normalize_line_for_spdex(asian_line),
                "bookmaker": bookmaker_id,
            },
        )
        rows = require_list_payload(data, "handicap detail")
        return [parse_handicap(item, bookmaker_id=bookmaker_id) for item in rows if isinstance(item, dict)]

    def price_volume(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        cached = self._chuqi_trade_cache.get(event_id, {}).get(selection)
        if cached:
            return cached
        try:
            return self.newspdex_tradeflow(event_id, selection)
        except DataError:
            pass
        data = self._get_json(
            "/spdex/price/volumn",
            {"eventid": event_id, "hour": -1, "selection": selection},
        )
        rows = require_list_payload(data, "price/volumn")
        return [parse_price_volume(item) for item in rows if isinstance(item, dict)]

    def newspdex_tradeflow(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        data = self._get_web_json(
            f"/api/newspdex/charts/{event_id}/tradeflow",
            {"market": "standard", "selection": selection, "granularity": "15m"},
        )
        payload = data.get("data") if isinstance(data, dict) else None
        if not isinstance(payload, dict):
            raise DataError("SPDEX newspdex tradeflow returned an unexpected shape")
        if payload.get("status") == "no-access" or payload.get("accessLocked"):
            raise DataError("SPDEX newspdex tradeflow is not available for current account")
        buckets = payload.get("buckets")
        if not isinstance(buckets, list):
            raise DataError("SPDEX newspdex tradeflow returned no buckets")
        points: list[PriceVolumePoint] = []
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            items = bucket.get("items")
            if isinstance(items, dict):
                volume = sum(amount_to_float(value) for value in items.values())
            else:
                volume = amount_to_float(bucket.get("volume"))
            points.append(
                PriceVolumePoint(
                    price=to_float_or_none(bucket.get("price")) or 0.0,
                    volume=volume,
                    update_time=parse_datetime_or_none(bucket.get("time")),
                    attr=None,
                )
            )
        return points

    def euro_trend(self, event_id: int) -> list[EuroTrendPoint]:
        data = self._get_json(
            "/spdex/odds/1x2/trend",
            {"hour": -1, "eid": event_id},
        )
        rows = require_list_payload(data, "euro trend")
        return [parse_euro_trend(item) for item in rows if isinstance(item, dict)]


class SnapshotStore:
    """Append-only local JSONL cache for pre-match trend tracking."""

    def __init__(self, root: str | Path = SNAPSHOT_DIR_NAME):
        self.root = Path(root)

    def event_path(self, event_id: int) -> Path:
        return self.root / f"{event_id}.jsonl"

    def scheduler_state_path(self) -> Path:
        return self.root / "scheduler_state.json"

    def save(self, result: "AnalysisResult", fetched_at: datetime | None = None) -> Path:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        self.root.mkdir(parents=True, exist_ok=True)
        before_minutes = (result.match.match_time - fetched_at).total_seconds() / 60.0
        home_price = to_float_or_none(result.match.raw.get("AsianAvrHome"))
        away_price = to_float_or_none(result.match.raw.get("AsianAvrAway"))
        fingerprint = model_source_fingerprint()
        record = {
            "schema": 2,
            "fetched_at": fetched_at.isoformat(),
            "model_version": MODEL_VERSION,
            "model_fingerprint": fingerprint,
            "minutes_before_kickoff": round(before_minutes, 3),
            "provenance": {
                "kind": "live_snapshot",
                "validation_eligible": before_minutes > 0,
                "reason": "" if before_minutes > 0 else "post_kickoff",
            },
            "market": {
                "asian_line": result.match.asian_line,
                "home_raw_price": home_price,
                "away_raw_price": away_price,
                "home_decimal_odds": normalize_asian_decimal_odds(home_price),
                "away_decimal_odds": normalize_asian_decimal_odds(away_price),
            },
            "match": match_to_dict(result.match),
            "result": result.to_dict(),
        }
        path = self.event_path(result.match.event_id)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        return path

    def load_event(self, event_id: int) -> list[dict[str, Any]]:
        path = self.event_path(event_id)
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    records.append(record)
        return sorted(records, key=lambda item: str(item.get("fetched_at", "")))

    def load_scheduler_state(self) -> dict[str, Any]:
        path = self.scheduler_state_path()
        if not path.exists():
            return {"completed": {}}
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {"completed": {}}
        if not isinstance(data, dict):
            return {"completed": {}}
        completed = data.get("completed")
        if not isinstance(completed, dict):
            data["completed"] = {}
        return data

    def save_scheduler_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.scheduler_state_path().open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)

    def mark_task_completed(self, task: ScheduledTask, completed_at: datetime | None = None) -> None:
        completed_at = completed_at or datetime.now(timezone.utc)
        state = self.load_scheduler_state()
        completed = state.setdefault("completed", {})
        completed[task.key] = {
            "completed_at": completed_at.isoformat(),
            "event_id": task.match.event_id,
            "label": task.label,
            "match_time": task.match.match_time.isoformat(),
        }
        self.save_scheduler_state(state)


class Predictor:
    def __init__(self, client: SpdexClient, snapshot_store: SnapshotStore | None = None):
        self.client = client
        self.snapshot_store = snapshot_store

    def analyze(self, match: Match) -> AnalysisResult:
        warnings: list[str] = []
        match = self._refresh_newspdex_detail(match, warnings)
        match = self._refresh_chuqi_bifa_detail(match, warnings)
        upper_team, lower_team = upper_lower_teams(match)

        snapshot_context = self._snapshot_context(match)
        handicap_rows = self._handicap_rows(match, warnings)
        bifa_signal = self._bifa_signal(match, upper_team, lower_team)
        trade_signal = self._trade_signal(match, upper_team, lower_team, warnings, snapshot_context)
        fair_line_signal = self._fair_line_signal(match, upper_team, lower_team, handicap_rows)
        handicap_signal = self._handicap_signal(
            match,
            upper_team,
            lower_team,
            handicap_rows,
            snapshot_context,
            fair_line_signal,
        )
        euro_kelly_signal = self._euro_kelly_signal(match, upper_team, lower_team, warnings)
        draw_risk_signal = self._draw_risk_signal(match, upper_team, lower_team)
        bookmaker_consensus_signal = self._bookmaker_consensus_signal(match, upper_team, handicap_rows)
        depth_profile_signal = self._depth_profile_signal(
            match,
            upper_team,
            lower_team,
            handicap_rows,
            bifa_signal,
            trade_signal,
            handicap_signal,
            euro_kelly_signal,
            draw_risk_signal,
        )
        snapshot_trend_signal = self._snapshot_trend_signal(match, snapshot_context)
        market_elasticity_signal = self._market_elasticity_signal(
            match,
            upper_team,
            lower_team,
            handicap_rows,
            snapshot_context,
        )
        handicap_signal, market_elasticity_signal = deep_favorite_retracement_guard(
            match,
            upper_team,
            handicap_signal,
            market_elasticity_signal,
            bifa_signal,
            trade_signal,
            euro_kelly_signal,
        )
        external_consensus_signal = self._external_consensus_signal(match, upper_team, lower_team)
        water_value_signal = self._water_value_signal(
            match,
            upper_team,
            lower_team,
            handicap_rows,
            bifa_signal,
            trade_signal,
            euro_kelly_signal,
            fair_line_signal,
            handicap_signal,
            bookmaker_consensus_signal,
            depth_profile_signal,
            snapshot_trend_signal,
            market_elasticity_signal,
            external_consensus_signal,
        )
        market_balance_signal = self._market_balance_signal(
            match,
            upper_team,
            lower_team,
            bifa_signal,
            trade_signal,
            handicap_signal,
            euro_kelly_signal,
            draw_risk_signal,
            fair_line_signal,
            depth_profile_signal,
            snapshot_trend_signal,
            market_elasticity_signal,
            external_consensus_signal,
            water_value_signal,
            snapshot_context,
        )
        cover_risk_signal = self._cover_risk_signal(
            match,
            bifa_signal,
            trade_signal,
            euro_kelly_signal,
            draw_risk_signal,
            handicap_signal,
            bookmaker_consensus_signal,
            depth_profile_signal,
            snapshot_trend_signal,
            market_balance_signal,
            snapshot_context,
        )

        all_signals: list[Signal] = [
            bifa_signal,
            trade_signal,
            handicap_signal,
            euro_kelly_signal,
            market_balance_signal,
            draw_risk_signal,
            fair_line_signal,
            bookmaker_consensus_signal,
            depth_profile_signal,
            cover_risk_signal,
            snapshot_trend_signal,
            market_elasticity_signal,
            external_consensus_signal,
            water_value_signal,
        ]
        adjusted_signals = source_adjusted_signals(match, all_signals)
        signals = scoring_signals_for_source(match, adjusted_signals)

        available_weight = sum(s.weight for s in signals if s.available)
        weighted_score = 0.0
        if available_weight > 0:
            weighted_score = sum(s.score * s.weight for s in signals if s.available)
            weighted_score = weighted_score / available_weight
            if match.raw.get("_source") != "okooo":
                weighted_score, okooo_note = okooo_static_snapshot_score_adjustment(
                    match,
                    weighted_score,
                    trade_signal,
                    euro_kelly_signal,
                    snapshot_context,
                )
                if okooo_note:
                    warnings.append(okooo_note)
                weighted_score, lift_note = marginal_model_score_lift_for_mid_deep_upper(
                    match,
                    weighted_score,
                    available_weight,
                    euro_kelly_signal,
                    market_balance_signal,
                    draw_risk_signal,
                    trade_signal,
                    snapshot_context,
                )
                if lift_note:
                    warnings.append(lift_note)
                weighted_score, deep_lift_note = marginal_deep_upper_cover_score_lift(
                    match,
                    weighted_score,
                    available_weight,
                    euro_kelly_signal,
                    external_consensus_signal,
                    market_balance_signal,
                    handicap_signal,
                    cover_risk_signal,
                    trade_signal,
                    snapshot_context,
                )
                if deep_lift_note:
                    warnings.append(deep_lift_note)
                weighted_score, trap_note = model_upper_trap_score_adjustment(
                    match,
                    weighted_score,
                    decision_signals_for_source(match, adjusted_signals),
                )
                if trap_note:
                    warnings.append(trap_note)
                weighted_score, missing_asian_note = okooo_missing_live_asian_model_cap(
                    match,
                    weighted_score,
                    decision_signals_for_source(match, adjusted_signals),
                )
                if missing_asian_note:
                    warnings.append(missing_asian_note)
            weighted_score, retreat_guard_note = deep_favorite_retracement_score_floor(
                match,
                weighted_score,
                signals,
            )
            if retreat_guard_note:
                warnings.append(retreat_guard_note)
            weighted_score, shallow_trap_note = shallow_hot_favorite_trap_score_adjustment(
                match,
                weighted_score,
                decision_signals_for_source(match, adjusted_signals),
            )
            if shallow_trap_note:
                warnings.append(shallow_trap_note)
            weighted_score, shallow_value_note = shallow_antihot_value_confirmation_guard(
                match,
                weighted_score,
                decision_signals_for_source(match, adjusted_signals),
            )
            if shallow_value_note:
                warnings.append(shallow_value_note)

        completeness = int(round(100 * available_weight / (1 - WEIGHTS["data_quality"])))
        completeness = clamp_int(completeness, 0, 100)

        snapshot_stop_lift = snapshot_stop_update_lift(match, snapshot_trend_signal)
        if available_weight < 0.50:
            model_recommendation = recommendation_from_score(weighted_score)
            model_confidence = min(
                confidence_from_score(weighted_score, completeness, model_recommendation),
                45,
            )
            if snapshot_stop_lift:
                warnings.append(
                    "临场数据停更且可用信号权重偏低；模型方向仍按综合分二选一计算，并请结合本地快照趋势理解"
                )
            else:
                warnings.append("可用信号不足，仍按综合分给出二选一方向，置信度已压低")
        else:
            model_recommendation = recommendation_from_score(weighted_score)
            model_confidence = confidence_from_score(weighted_score, completeness, model_recommendation)
        if match.is_stop_update:
            warnings.append("SPDEX 标记该场已停更，推荐仅供复盘，不代表可实时购买")

        data_quality_signal = Signal(
            name="数据质量",
            score=(completeness / 100.0) * (1 if weighted_score >= 0 else -1),
            weight=WEIGHTS["data_quality"],
            available=True,
            reason=f"可用权重 {available_weight:.2f}，完整度 {completeness}%",
        )
        score_momentum_signal = current_score_momentum_signal(weighted_score, snapshot_context)
        decision_reason = model_decision_reason(weighted_score, model_recommendation, available_weight)
        return AnalysisResult(
            match=match,
            recommendation=model_recommendation,
            score=weighted_score,
            confidence=model_confidence,
            completeness=completeness,
            upper_team=upper_team,
            lower_team=lower_team,
            signals=[*signals, score_momentum_signal, data_quality_signal],
            warnings=warnings,
            model_recommendation=model_recommendation,
            model_confidence=model_confidence,
            purchase_score=weighted_score,
            decision_reason=decision_reason,
            is_reversed=False,
        )

    def _refresh_newspdex_detail(self, match: Match, warnings: list[str]) -> Match:
        if match.raw.get("_source") != "newspdex":
            return match
        try:
            detail = self.client.newspdex_match_detail(match.event_id)
            return merge_match_detail(match, detail) if detail else match
        except DataError as exc:
            warnings.append(f"新版详情刷新失败，使用列表实时字段: {exc}")
            return match

    def _refresh_chuqi_bifa_detail(self, match: Match, warnings: list[str]) -> Match:
        if not hasattr(self.client, "enrich_with_chuqi_bifa"):
            return match
        try:
            enriched = self.client.enrich_with_chuqi_bifa(match)
        except DataError as exc:
            warnings.append(f"楚旗必发补充失败: {exc}")
            return match
        if enriched.raw.get("_bifa_source") == "chuqi" and match.raw.get("_bifa_source") != "chuqi":
            warnings.append("已使用楚旗必发数据补充必发指数/成交走势")
        return enriched

    def _handicap_rows(self, match: Match, warnings: list[str]) -> list[HandicapRow]:
        if match.raw.get("_source") in ("newspdex", "chuqi"):
            return sorted(fallback_handicap_rows_from_base(match), key=bookmaker_priority)
        try:
            rows = self.client.handicap_list(match.event_id, match.asian_line)
        except DataError as exc:
            warnings.append(str(exc))
            rows = fallback_handicap_rows_from_base(match)
        if not rows:
            rows = fallback_handicap_rows_from_base(match)
        rows = handicap_rows_near_match_line(rows, match.asian_line)
        return sorted(rows, key=bookmaker_priority)

    def _snapshot_context(self, match: Match) -> SnapshotContext | None:
        if self.snapshot_store is None:
            return None
        records = self.snapshot_store.load_event(match.event_id)
        if not records:
            return None
        metric_records = [(idx, record, snapshot_metrics(record)) for idx, record in enumerate(records)]
        comparable = [
            (idx, record, metrics)
            for idx, record, metrics in metric_records
            if metrics.get("bifa_available", 0.0) > 0
        ]
        if comparable:
            first_idx, _first_record, first_metrics = comparable[0]
        else:
            first_idx, _first_record, first_metrics = metric_records[0]
        last_metrics = snapshot_metrics(records[-1])
        signal_history_score, signal_history_reason = score_snapshot_signal_history(records)
        context_records = records[first_idx:]
        return SnapshotContext(
            records=context_records,
            first_metrics=first_metrics,
            last_metrics=last_metrics,
            signal_history_score=signal_history_score,
            signal_history_reason=signal_history_reason,
        )

    def _bifa_signal(self, match: Match, upper_team: str, lower_team: str) -> Signal:
        raw = match.raw
        upper_key = side_key(match, upper_team)
        lower_key = side_key(match, lower_team)
        if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
            return unavailable_signal("必发指数", WEIGHTS["bifa"], "平手盘无法稳定映射上盘/下盘")

        try:
            upper_index = float(raw.get(f"BfIndex{upper_key}", 0.0))
            lower_index = float(raw.get(f"BfIndex{lower_key}", 0.0))
            upper_amount = float(raw.get(f"BfAmount{upper_key}", 0.0))
            lower_amount = float(raw.get(f"BfAmount{lower_key}", 0.0))
            upper_payout = float(raw.get(f"BfPayout{upper_key}", 0.0))
            lower_payout = float(raw.get(f"BfPayout{lower_key}", 0.0))
            draw_payout_bf = float(raw.get("BfPayoutDraw", 0.0))
            upper_odds = float(raw.get(f"BfOdds{upper_key}", 0.0))
            lower_odds = float(raw.get(f"BfOdds{lower_key}", 0.0))
        except (TypeError, ValueError):
            return unavailable_signal("必发指数", WEIGHTS["bifa"], "必发字段解析失败")

        if upper_index == 0 and lower_index == 0 and upper_amount == 0 and lower_amount == 0:
            return unavailable_signal("必发指数", WEIGHTS["bifa"], "必发指数和成交量为空")

        index_edge, amount_edge = bifa_index_amount_edges(match, upper_team, lower_team)
        payout_edge = score_bifa_payout_edge(match, upper_team, lower_team)
        odds_edge = score_bifa_odds_confirmation(upper_odds, lower_odds)
        heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        hot_divergence_penalty = score_hot_divergence_penalty(
            heat_edge=heat_edge,
            confirmation_edge=odds_edge,
            payout_edge=payout_edge,
            line_depth=line_depth(match.asian_line),
        )
        score = clamp(
            0.30 * index_edge
            + 0.25 * amount_edge
            + 0.20 * payout_edge
            + 0.25 * odds_edge
            - hot_divergence_penalty,
            -1,
            1,
        )
        draw_prob_frac = okooo_draw_implied_prob_from_raw(raw)
        # 浅盘：必发指数一边倒但让球方必发赔付为巨额负、赔付方在另一侧 → 典型「大热难穿」风险，避免单押上盘
        # 平局赔付与隐含平局概率同时偏高时，原「仅对比上下盘赔付」条件常不触发，仍应视为大热承压（README：平局风险要压强度）
        depth_bd = line_depth(match.asian_line)
        extra_bifa = ""
        classic_lower_pressure = lower_payout > 0.0 and abs(upper_payout) >= abs(lower_payout) * 0.82
        draw_side_pressure = (
            draw_payout_bf > 0.0
            and draw_prob_frac is not None
            and draw_prob_frac >= 0.22
            and draw_payout_bf > abs(upper_payout) * 0.82
        )
        if (
            depth_bd <= 0.75
            and score > 0.38
            and upper_index >= 72.0
            and upper_payout < 0.0
            and lower_payout > 0.0
            and (classic_lower_pressure or draw_side_pressure)
        ):
            mut_damper = clamp(0.34 + (upper_index - 72.0) * 0.007 + max(score - 0.38, 0.0) * 0.35, 0.30, 0.68)
            score = clamp(score - mut_damper, -1, 1)
            extra_bifa = f"，浅盘大热但庄家盈亏对让球方承压 扣分 {mut_damper:.2f}"
        reason = (
            f"{upper_team} 必发指数 {upper_index:.1f} vs {lower_team} {lower_index:.1f}，"
            f"成交额 {upper_amount:,.0f} vs {lower_amount:,.0f}，"
            f"盈亏 {upper_payout:.1f} vs {lower_payout:.1f}，"
            f"必发赔率 {upper_odds:.2f} vs {lower_odds:.2f}，"
            f"平局指数/成交/盈亏 {float_or_zero(raw.get('BfIndexDraw')):.1f}/{float_or_zero(raw.get('BfAmountDraw')):,.0f}/{draw_payout_bf:.1f}"
        )
        split_reason = bifa_heat_split_reason(index_edge, amount_edge, upper_team, lower_team)
        if split_reason:
            reason += f"，{split_reason}"
        if hot_divergence_penalty:
            reason += f"，大热未获赔率/盈亏确认 扣分 {hot_divergence_penalty:.2f}"
        reason += extra_bifa
        return Signal("必发指数", score, WEIGHTS["bifa"], True, reason)

    def _trade_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        warnings: list[str],
        snapshot_context: SnapshotContext | None,
    ) -> Signal:
        try:
            upper_selection = selection_for_team(match, upper_team)
            lower_selection = selection_for_team(match, lower_team)
            upper_points = self.client.price_volume(match.event_id, upper_selection)
            lower_points = self.client.price_volume(match.event_id, lower_selection)
        except DataError as exc:
            warnings.append(str(exc))
            return unavailable_signal("必发成交走势", WEIGHTS["bifa_trade"], "成交走势接口不可用")

        upper_score, upper_reason, upper_meta = score_price_volume(upper_points)
        lower_score, lower_reason, lower_meta = score_price_volume(lower_points)
        if upper_score is None or lower_score is None:
            return unavailable_signal("必发成交走势", WEIGHTS["bifa_trade"], "近1小时成交走势不足")

        upper_meta = upper_meta or {}
        lower_meta = lower_meta or {}
        upper_flow = float(upper_meta.get("total_flow") or 0.0)
        lower_flow = float(lower_meta.get("total_flow") or 0.0)
        flow_total = upper_flow + lower_flow
        recent_share = upper_flow / flow_total if flow_total > 0 else 0.5

        upper_key = side_key(match, upper_team)
        lower_key = side_key(match, lower_team)
        upper_amount = float_or_zero(match.raw.get(f"BfAmount{upper_key}"))
        lower_amount = float_or_zero(match.raw.get(f"BfAmount{lower_key}"))
        amount_total = upper_amount + lower_amount
        baseline_share = upper_amount / amount_total if amount_total > 0 else 0.5
        abnormal_share = clamp((recent_share - baseline_share) / 0.18, -1, 1)

        price_impact = clamp(
            (float(upper_meta.get("price_score") or 0.0) - float(lower_meta.get("price_score") or 0.0)) / 2.0,
            -1,
            1,
        )
        upper_acceleration = float(
            upper_meta.get("raw_trend")
            if upper_meta.get("raw_trend") is not None
            else upper_meta.get("volume_score") or 0.0
        )
        lower_acceleration = float(
            lower_meta.get("raw_trend")
            if lower_meta.get("raw_trend") is not None
            else lower_meta.get("volume_score") or 0.0
        )
        acceleration = clamp((upper_acceleration - lower_acceleration) / 2.0, -1, 1)
        signal_score = clamp(
            0.40 * abnormal_share + 0.35 * price_impact + 0.25 * acceleration,
            -1,
            1,
        )

        return Signal(
            "必发成交走势",
            signal_score,
            WEIGHTS["bifa_trade"],
            True,
            (
                f"异常成交份额 {recent_share:.1%} vs 基准 {baseline_share:.1%} ({abnormal_share:+.2f})；"
                f"价格冲击 {price_impact:+.2f}；成交加速度 {acceleration:+.2f}；"
                f"{upper_team}: {upper_reason}；{lower_team}: {lower_reason}"
            ),
        )

    def _handicap_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        rows: list[HandicapRow],
        snapshot_context: SnapshotContext | None,
        fair_line_signal: Signal | None = None,
    ) -> Signal:
        if not rows:
            return unavailable_signal("亚盘水位", WEIGHTS["asian_handicap"], "该盘口暂无公司数据")

        selected_rows = rows
        fallback_only = all(row.source == "fallback" for row in selected_rows)
        score, confidence, median_move, moves = handicap_market_axis(match, selected_rows, upper_team)
        history_only = False
        if not moves:
            history_move = snapshot_equivalent_fair_move(snapshot_context)
            if history_move is None:
                return unavailable_signal(
                    "亚盘水位",
                    WEIGHTS["asian_handicap"],
                    "公司盘口或两边水位缺失，无法计算等价公平盘口",
                )
            history_only = True
            median_move = history_move
            moves = [history_move]
            confidence = 0.45
            score = clamp(history_move / 0.30, -1, 1) * confidence

        persistence, persistence_reason = handicap_path_persistence(snapshot_context)
        score = clamp(score * persistence, -1, 1)
        positive = sum(1 for move in moves if move > 0.025)
        negative = sum(1 for move in moves if move < -0.025)
        neutral = len(moves) - positive - negative
        directional_share = max(positive, negative) / len(moves)
        recent_score, recent_confidence, recent_move, recent_reason = recent_handicap_path_axis(
            match,
            selected_rows,
            upper_team,
            snapshot_context,
        )
        recent_weight = 0.0
        if recent_confidence > 0 and abs(recent_move) >= 0.04:
            minutes_before = current_minutes_before_kickoff(match)
            if minutes_before <= 90:
                recent_weight = 0.68
            elif minutes_before <= 180:
                recent_weight = 0.58
            elif minutes_before <= 360:
                recent_weight = 0.45
            else:
                recent_weight = 0.30
            if score * recent_score < 0 and abs(recent_move) >= 0.06:
                recent_weight = min(0.76, recent_weight + 0.08)
            current_company_market_confirmed = directional_share >= 0.70 and abs(median_move) >= 0.075
            strong_extreme_reversal = (
                score * recent_score < 0
                and abs(recent_move) >= 0.12
                and ("高点回撤" in recent_reason or "低点反弹" in recent_reason)
                and ("公司强确认" in recent_reason or abs(recent_move) >= 0.22)
            )
            if current_company_market_confirmed and score * recent_score < 0 and not strong_extreme_reversal:
                recent_weight = min(recent_weight, 0.22)
            recent_weight *= clamp(recent_confidence, 0.55, 1.0)
            score = clamp((1.0 - recent_weight) * score + recent_weight * recent_score, -1, 1)
        reasons = [
            (
                f"等价公平盘口中位变化 {median_move:+.3f} 球，"
                f"公司同向 上{positive}/下{negative}/中性{neutral}，一致性可信度 {confidence:.2f}"
            ),
            persistence_reason,
        ]
        if recent_weight > 0:
            reasons.append(
                f"{recent_reason}，临场权重 {recent_weight:.2f}；初盘累计方向降至 {1.0 - recent_weight:.2f}"
            )
        elif recent_confidence > 0:
            reasons.append(f"{recent_reason}，变化不足 0.04 球，仅记录不改方向")
        if history_only:
            reasons[0] = (
                f"公司初盘路径缺失，使用连续快照等价公平盘口变化 {median_move:+.3f} 球，"
                f"可信度降至 {confidence:.2f}"
            )
        for row in selected_rows[:3]:
            fair_move = handicap_row_equivalent_fair_move(match, row, upper_team)
            if fair_move is None:
                continue
            reasons.append(
                f"{row.name} 盘口 {row.init_line:+.2f}->{row.latest_line:+.2f}，"
                f"水位 {row.init_sec_a:.3g}/{row.init_sec_b:.3g}->{row.sec_a:.3g}/{row.sec_b:.3g}，"
                f"等价变化 {fair_move:+.3f}"
            )
        if fallback_only and not history_only:
            score = 0.0
            reasons.append("仅有静态均值，无初盘到即时盘路径，本项按中性")
        if (
            match.raw.get("_source") == "okooo"
            and fair_line_signal is not None
            and fair_line_signal.available
        ):
            reasons.append(f"赔率合理盘口仅作深度风险参考，不重复进入亚盘方向：{fair_line_signal.score:+.3f}")
        return Signal(
            "亚盘水位",
            score,
            WEIGHTS["asian_handicap"],
            True,
            "；".join(reasons),
        )

    def _euro_kelly_signal(
        self, match: Match, upper_team: str, lower_team: str, warnings: list[str]
    ) -> Signal:
        if match.raw.get("_source") in ("newspdex", "chuqi"):
            return fallback_euro_kelly_signal(match, upper_team, lower_team)
        try:
            points = self.client.euro_trend(match.event_id)
        except DataError as exc:
            warnings.append(str(exc))
            return fallback_euro_kelly_signal(match, upper_team, lower_team)

        if len(points) < 2:
            return fallback_euro_kelly_signal(match, upper_team, lower_team)

        upper_key = side_key(match, upper_team).lower()
        lower_key = side_key(match, lower_team).lower()
        if upper_key not in ("home", "away") or lower_key not in ("home", "away"):
            return unavailable_signal("欧赔/Kelly", WEIGHTS["euro_kelly"], "平手盘无法稳定映射欧赔方向")

        first = points[0]
        last = points[-1]
        upper_price_drop = getattr(first, f"{upper_key}_price") - getattr(last, f"{upper_key}_price")
        lower_price_drop = getattr(first, f"{lower_key}_price") - getattr(last, f"{lower_key}_price")
        upper_kelly_drop = getattr(first, f"{upper_key}_kelly") - getattr(last, f"{upper_key}_kelly")
        lower_kelly_drop = getattr(first, f"{lower_key}_kelly") - getattr(last, f"{lower_key}_kelly")
        price_edge = clamp((upper_price_drop - lower_price_drop) / 0.6, -1, 1)
        kelly_flat = (
            abs(first.home_kelly - last.home_kelly) < 1e-5
            and abs(first.draw_kelly - last.draw_kelly) < 1e-5
            and abs(first.away_kelly - last.away_kelly) < 1e-5
        )
        # 澳客 peilv 常仅有一组即时凯利复制到走势首尾，Kelly 变化为伪 0；仅用欧赔价差并注明
        if match.raw.get("_source") == "okooo" and kelly_flat:
            kelly_edge = 0.0
            kelly_note = "（澳客仅即时凯利，走势点 Kelly 无变化，Kelly 项按中性）"
        else:
            kelly_edge = clamp((upper_kelly_drop - lower_kelly_drop) / 8.0, -1, 1)
            kelly_note = ""
        draw_risk = 0.0
        if last.draw_kelly < min(last.home_kelly, last.away_kelly):
            draw_risk = 0.20
        score = clamp(0.55 * price_edge + 0.45 * kelly_edge - math.copysign(draw_risk, price_edge or 1), -1, 1)
        return Signal(
            "欧赔/Kelly",
            score,
            WEIGHTS["euro_kelly"],
            True,
            (
                f"{upper_team} 欧赔变化 {upper_price_drop:+.2f}、Kelly变化 {upper_kelly_drop:+.2f}；"
                f"{lower_team} 欧赔变化 {lower_price_drop:+.2f}、Kelly变化 {lower_kelly_drop:+.2f}"
                f"{kelly_note}"
            ),
        )

    def _draw_risk_signal(self, match: Match, upper_team: str, lower_team: str) -> Signal:
        upper_key = side_key(match, upper_team)
        lower_key = side_key(match, lower_team)
        if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
            return unavailable_signal("平局风险", WEIGHTS["draw_risk"], "无法映射上下盘方向")

        upper_price = first_positive(
            match.raw.get(f"EuroAvr{upper_key}"),
            match.raw.get(f"BfOdds{upper_key}"),
        )
        lower_price = first_positive(
            match.raw.get(f"EuroAvr{lower_key}"),
            match.raw.get(f"BfOdds{lower_key}"),
        )
        draw_price = first_positive(match.raw.get("EuroAvrDraw"), match.raw.get("BfOddsDraw"))
        if upper_price <= 0 or lower_price <= 0 or draw_price <= 0:
            return unavailable_signal("平局风险", WEIGHTS["draw_risk"], "缺少平局欧赔/必发赔率")

        upper_prob, draw_prob, lower_prob = normalized_probabilities(upper_price, draw_price, lower_price)
        draw_kelly = first_positive(match.raw.get("KellyDraw"))
        upper_kelly = first_positive(match.raw.get(f"Kelly{upper_key}"))
        lower_kelly = first_positive(match.raw.get(f"Kelly{lower_key}"))
        kelly_warning = 0.0
        if draw_kelly > 0 and upper_kelly > 0 and lower_kelly > 0:
            best_side_kelly = min(upper_kelly, lower_kelly)
            if draw_kelly < best_side_kelly:
                kelly_warning = clamp((best_side_kelly - draw_kelly) / max(best_side_kelly, 1.0), 0, 0.35)

        depth = line_depth(match.asian_line)
        if depth <= 0.5:
            draw_baseline = 0.255
            depth_factor = 1.0
            win_buffer_weight = 0.18
        elif depth <= 1.25:
            draw_baseline = 0.22
            depth_factor = 0.95
            win_buffer_weight = 0.03
        else:
            draw_baseline = 0.18
            depth_factor = 0.45
            win_buffer_weight = 0.08
        draw_pressure = clamp((draw_prob - draw_baseline) / 0.14, 0, 1)
        upper_win_buffer = clamp((upper_prob - draw_prob) / 0.20, -1, 1)
        cover_draw_penalty = 0.0
        if 0.75 <= depth <= 1.25 and draw_prob > 0.20:
            cover_draw_penalty = clamp((draw_prob - 0.20) / 0.08, 0, 1) * 0.30
        score = clamp(
            -(0.70 * draw_pressure + kelly_warning) * depth_factor
            + win_buffer_weight * max(upper_win_buffer, 0)
            - cover_draw_penalty,
            -1,
            1,
        )
        reason = (
            f"平局概率约 {draw_prob:.1%}，{upper_team}胜率约 {upper_prob:.1%}，"
            f"盘口深度 {depth:.2f}"
        )
        if kelly_warning:
            reason += f"，Kelly提示平局风险 {kelly_warning:.2f}"
        if cover_draw_penalty:
            reason += f"，一球盘附近平局输盘风险 {cover_draw_penalty:.2f}"
        return Signal("平局风险", score, WEIGHTS["draw_risk"], True, reason)

    def _fair_line_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        rows: list[HandicapRow],
    ) -> Signal:
        upper_key = side_key(match, upper_team)
        lower_key = side_key(match, lower_team)
        if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
            return unavailable_signal("盘口合理性", WEIGHTS["fair_line"], "无法映射上下盘方向")

        upper_price = first_positive(
            match.raw.get(f"EuroAvr{upper_key}"),
            match.raw.get(f"BfOdds{upper_key}"),
        )
        lower_price = first_positive(
            match.raw.get(f"EuroAvr{lower_key}"),
            match.raw.get(f"BfOdds{lower_key}"),
        )
        draw_price = first_positive(match.raw.get("EuroAvrDraw"), match.raw.get("BfOddsDraw"))
        if upper_price <= 0 or lower_price <= 0:
            return unavailable_signal("盘口合理性", WEIGHTS["fair_line"], "缺少欧赔/必发价格")

        price_edge = score_bifa_odds_confirmation(upper_price, lower_price)
        probability_reason = ""
        upper_prob = draw_prob = lower_prob = 0.0
        if draw_price > 0:
            upper_prob, draw_prob, lower_prob = normalized_probabilities(upper_price, draw_price, lower_price)
            fair_depth = fair_handicap_depth_from_probabilities(upper_prob, draw_prob, lower_prob)
            probability_reason = (
                f"胜/平/负概率约 {upper_prob:.1%}/{draw_prob:.1%}/{lower_prob:.1%}，"
                "已纳入平局风险"
            )
        else:
            fair_depth = fair_handicap_depth(price_edge) * 0.65
            probability_reason = "缺少平局价格，盘口估算保守降权"
        actual_depth = line_depth(match.asian_line)
        upper_water = average_upper_water(rows, match, upper_team)
        lower_water = average_team_water(rows, match, lower_team)
        has_upper_water = upper_water > 0
        water_edge = lower_water - upper_water if has_upper_water and lower_water > 0 else 0.0
        gap = fair_depth - actual_depth
        heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        index_edge, amount_edge = bifa_index_amount_edges(match, upper_team, lower_team)
        payout_edge = score_bifa_payout_edge(match, upper_team, lower_team)
        heat_split = index_edge * amount_edge < 0 and abs(index_edge) >= 0.04 and abs(amount_edge) >= 0.18
        hot_upper_pressure = max(heat_edge, amount_edge, 0.0)
        okooo_shallow_cover_value = (
            match.raw.get("_source") == "okooo"
            and draw_price > 0
            and gap >= 0.30
            and upper_prob >= 0.70
            and draw_prob <= 0.16
            and (not has_upper_water or upper_water < 2.00)
        )
        interpretation = "盘口与价格大致匹配"
        if gap > 0.12:
            shallow_pressure = clamp(gap / 0.65, 0, 1)
            if upper_water >= 2.00:
                water_pressure = clamp((upper_water - 1.96) / 0.24, 0, 1)
                score = -clamp(0.18 + 0.32 * shallow_pressure + 0.25 * water_pressure, 0, 0.75)
                interpretation = "实际盘口偏浅且上盘高水，偏下盘风险"
            elif 0 < upper_water <= 1.82:
                score = clamp(0.12 + 0.30 * shallow_pressure, 0, 0.45)
                interpretation = "实际盘口偏浅但上盘低水防守，偏上盘确认"
            elif gap >= 0.30:
                if okooo_shallow_cover_value:
                    neutral_water_drag = clamp((upper_water - 1.88) / 0.12, 0, 1) if has_upper_water else 0.0
                    score = clamp(0.10 + 0.20 * shallow_pressure - 0.12 * neutral_water_drag, -0.05, 0.28)
                    interpretation = "实际盘口明显偏浅，上盘打穿门槛友好；水位未低水防守，价值降权"
                else:
                    neutral_water_pressure = clamp((upper_water - 1.86) / 0.14, 0, 1) if has_upper_water else 0.0
                    bifa_pressure = clamp(hot_upper_pressure / 0.45, 0, 1)
                    score = -clamp(
                        0.12 + 0.26 * shallow_pressure + 0.18 * neutral_water_pressure + 0.12 * bifa_pressure,
                        0,
                        0.65,
                    )
                    interpretation = "实际盘口明显偏浅且上盘未低水防守，偏下盘风险"
            else:
                score = clamp(0.12 * shallow_pressure, -0.15, 0.18)
                interpretation = "实际盘口偏浅但未见明显高水诱导，弱上盘价值"
        elif gap < -0.12:
            deep_pressure = clamp(abs(gap) / 0.65, 0, 1)
            if upper_water >= 2.00:
                water_pressure = clamp((upper_water - 1.96) / 0.24, 0, 1)
                score = -clamp(0.22 + 0.35 * deep_pressure + 0.20 * water_pressure, 0, 0.75)
                interpretation = "实际盘口偏深且上盘高水，偏下盘风险"
            elif 0 < upper_water <= 1.82:
                score = clamp(0.10 - 0.16 * deep_pressure, -0.35, 0.20)
                interpretation = "实际盘口偏深但上盘低水防守，风险有限"
            else:
                score = -clamp(0.20 * deep_pressure, 0, 0.45)
                interpretation = "实际盘口偏深，提高上盘打穿门槛"
        else:
            score = 0.0
            if upper_water >= 2.05:
                score = -0.10
                interpretation = "盘口匹配但上盘高水，轻微偏下盘"
            elif 0 < upper_water <= 1.82:
                score = 0.08
                interpretation = "盘口匹配且上盘低水，轻微偏上盘"
            elif 0 < upper_water <= 1.90 and water_edge >= 0.06:
                low_water_strength = clamp((water_edge - 0.06) / 0.16, 0, 1)
                score = clamp(0.04 + 0.07 * low_water_strength, 0, 0.11)
                interpretation = "盘口匹配且上盘相对低水，弱上盘确认"

        if not has_upper_water:
            score *= 0.45
            interpretation += "；缺少上盘水位，盘口合理性降权"

        bifa_reasons: list[str] = []
        if heat_split:
            if score > 0:
                score = max(0.0, score - 0.08)
            elif score < 0:
                score = min(0.0, score + 0.08)
            bifa_reasons.append("必发指数/成交额分裂，盘口估算降权")
        if gap > 0.12 and hot_upper_pressure >= 0.25 and upper_water > 1.86:
            penalty = clamp(0.08 + 0.18 * clamp(hot_upper_pressure / 0.55, 0, 1), 0, 0.24)
            if okooo_shallow_cover_value:
                penalty *= 0.35
            score = clamp(score - penalty, -1, 1)
            bifa_reasons.append(f"必发资金偏上盘但盘口未升深/低水防守，扣分 {penalty:.2f}")
        if payout_edge < -0.10 and gap > 0.12:
            penalty = clamp(0.05 + abs(payout_edge) * 0.20, 0, 0.18)
            if okooo_shallow_cover_value:
                penalty *= 0.45
            score = clamp(score - penalty, -1, 1)
            bifa_reasons.append(f"必发盈亏不支持上盘，扣分 {penalty:.2f}")
        elif payout_edge > 0.15 and score > 0:
            bonus = clamp(payout_edge * 0.10, 0, 0.06)
            score = clamp(score + bonus, -1, 1)
            bifa_reasons.append(f"必发盈亏确认上盘，补强 {bonus:.2f}")
        if bifa_reasons:
            interpretation += "；" + "；".join(bifa_reasons)
        # 深盘强队：欧赔 1X2 仍显著倾斜上盘时，「实际盘口偏深」的负分不宜过大，避免与打穿赛果系统性冲突
        if (
            draw_price > 0
            and actual_depth >= 1.2
            and score < -0.35
            and upper_prob >= 0.52
            and (upper_prob - lower_prob) >= 0.16
        ):
            bump = clamp(0.28 + 0.45 * min((upper_prob - lower_prob - 0.16) / 0.22, 1.0), 0.22, 0.62)
            if match.raw.get("_source") == "okooo" and upper_water >= 1.96:
                bump *= 0.35
                score = clamp(score + bump, -1, -0.05)
                interpretation += f"；澳客深盘上盘高水，胜赔只小幅回拉 {bump:.2f}"
            else:
                score = clamp(score + bump, -1, 0.10)
                interpretation += f"；深盘强队胜赔概率仍明显领先，盘口合理性负分回拉 {bump:.2f}"
        water_reason = f"上盘均水 {upper_water:.3g}"
        if lower_water > 0:
            water_reason = f"上下盘均水 {upper_water:.3g}/{lower_water:.3g}"
        return Signal(
            "盘口合理性",
            score,
            WEIGHTS["fair_line"],
            True,
            (
                f"价格估算合理盘口约 {fair_depth:.2f}，实际盘口 {actual_depth:.2f}，"
                f"{water_reason}；{probability_reason}；{interpretation}"
            ),
        )

    def _bookmaker_consensus_signal(
        self, match: Match, upper_team: str, rows: list[HandicapRow]
    ) -> Signal:
        live_rows = [row for row in rows if row.source != "fallback"]
        if len(live_rows) < 2:
            return unavailable_signal("公司一致性", WEIGHTS["bookmaker_consensus"], "主流公司盘口点不足")

        selected_rows = sorted(live_rows, key=bookmaker_priority)[:8]
        _axis, confidence, median_move, moves = handicap_market_axis(match, selected_rows, upper_team)
        if not moves:
            return unavailable_signal("公司一致性", WEIGHTS["bookmaker_consensus"], "主流公司盘口点不足")
        positives = sum(1 for move in moves if move > 0.025)
        negatives = sum(1 for move in moves if move < -0.025)
        neutral = len(moves) - positives - negatives
        direction = 1.0 if median_move > 0.025 else -1.0 if median_move < -0.025 else 0.0
        score = direction * confidence
        return Signal(
            "公司一致性",
            score,
            WEIGHTS["bookmaker_consensus"],
            True,
            (
                f"{len(moves)}家公司等价公平盘口：上盘{positives}，下盘{negatives}，"
                f"中性{neutral}，中位变化 {median_move:+.3f}，可信度 {confidence:.2f}；"
                "本项只表示方向轴可信度，不重复计分"
            ),
        )

    def _depth_profile_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        rows: list[HandicapRow],
        bifa_signal: Signal,
        trade_signal: Signal,
        handicap_signal: Signal,
        euro_kelly_signal: Signal,
        draw_risk_signal: Signal,
    ) -> Signal:
        depth = line_depth(match.asian_line)
        category = line_depth_category(depth)
        price_edge = score_bifa_price_edge(match, upper_team, lower_team)
        handicap_edge = handicap_signal.score if handicap_signal.available else 0.0
        trade_edge = trade_signal.score if trade_signal.available else 0.0
        euro_edge = euro_kelly_signal.score if euro_kelly_signal.available else 0.0
        draw_edge = draw_risk_signal.score if draw_risk_signal.available else 0.0
        upper_water = average_upper_water(rows, match, upper_team)

        if depth <= 0.5:
            score = clamp(0.30 * price_edge + 0.25 * euro_edge + 0.25 * handicap_edge + 0.20 * draw_edge, -1, 1)
            reason = "浅盘更看胜负确认和平局风险"
        elif depth <= 1.25:
            score = clamp(0.25 * price_edge + 0.35 * handicap_edge + 0.20 * euro_edge + 0.20 * trade_edge, -1, 1)
            reason = "中盘要求价格、亚盘和成交同步"
        else:
            high_water_penalty = 0.0
            if upper_water > 2.02:
                high_water_penalty = 0.22 if depth >= 1.5 else 0.12
            weak_cover_penalty = 0.0
            if handicap_edge < 0.08:
                weak_cover_penalty += 0.18
            if trade_signal.available and trade_edge < 0.05:
                weak_cover_penalty += 0.10
            score = clamp(
                0.20 * price_edge
                + 0.38 * handicap_edge
                + 0.20 * trade_edge
                + 0.17 * euro_edge
                + 0.05 * bifa_signal.score
                - high_water_penalty
                - weak_cover_penalty,
                -1,
                1,
            )
            reason = "深盘/超深盘更看打穿能力、上盘水位和连续确认"
        return Signal(
            "盘口深度/打穿能力",
            score,
            WEIGHTS["depth_profile"],
            True,
            f"{category}，盘口 {depth:.2f}，上盘均水 {upper_water:.3g}，{reason}",
        )

    def _cover_risk_signal(
        self,
        match: Match,
        bifa_signal: Signal,
        trade_signal: Signal,
        euro_kelly_signal: Signal,
        draw_risk_signal: Signal,
        handicap_signal: Signal,
        bookmaker_consensus_signal: Signal,
        depth_profile_signal: Signal,
        snapshot_trend_signal: Signal,
        market_balance_signal: Signal,
        snapshot_context: SnapshotContext | None,
    ) -> Signal:
        depth = line_depth(match.asian_line)
        if depth < 0.75:
            return Signal(
                "赢盘门槛风险",
                0.0,
                WEIGHTS["cover_risk"],
                True,
                f"盘口 {depth:.2f} 未达到中深盘门槛",
            )

        penalty = 0.0
        reasons: list[str] = []
        if not trade_signal.available:
            penalty += 0.07
            reasons.append("临场必发成交走势不可用")
        elif trade_signal.score < 0.08:
            penalty += 0.06
            reasons.append("必发成交走势未继续确认上盘")

        if not euro_kelly_signal.available:
            penalty += 0.05
            reasons.append("临场欧赔/Kelly走势不可用")
        elif euro_kelly_signal.score < 0.05:
            penalty += 0.06
            reasons.append("欧赔/Kelly未继续确认上盘")

        if snapshot_trend_signal.available:
            if snapshot_trend_signal.score < -0.05:
                penalty += 0.12
                reasons.append("快照历史趋势转弱")
            elif snapshot_trend_signal.score < 0.08:
                penalty += 0.07
                reasons.append("快照历史趋势未增强")
        else:
            penalty += 0.05
            reasons.append("缺少快照趋势确认")

        if snapshot_context and snapshot_context.available:
            history_fair_move = snapshot_equivalent_fair_move(snapshot_context)
            if snapshot_context.heat_delta > 0.06 and history_fair_move is not None and history_fair_move < -0.025:
                penalty += 0.18
                reasons.append("历史热度升高但等价公平盘口转弱")
            if depth >= 0.75 and snapshot_context.heat_delta > 0.06 and snapshot_context.line_depth_delta <= 0.05:
                penalty += 0.10
                reasons.append("历史热度升高但盘口未升深")

        if draw_risk_signal.available and draw_risk_signal.score < 0.02 and depth <= 1.25:
            penalty += 0.12
            reasons.append("中盘存在平局/小胜风险")

        if bookmaker_consensus_signal.available and bookmaker_consensus_signal.score < -0.08:
            consensus_penalty = 0.14 if depth >= 1.25 else 0.10
            penalty += consensus_penalty
            reasons.append("主流公司分歧偏下盘")

        fallback_handicap = "静态亚盘均值兜底" in handicap_signal.reason if handicap_signal.available else False
        if depth >= 1.25 and handicap_signal.available and handicap_signal.score < -0.10 and not fallback_handicap:
            penalty += 0.12
            reasons.append("深盘亚盘水位未确认上盘打穿")

        if depth_profile_signal.available and depth_profile_signal.score < 0.25:
            penalty += 0.08 if depth <= 1.25 else 0.12
            reasons.append("打穿能力确认不足")

        if depth >= 1.5 and bifa_signal.available and bifa_signal.score < 0.05:
            penalty += 0.15
            reasons.append("深盘热门缺少必发质量确认")

        if market_balance_signal.available and market_balance_signal.score > 0.75 and (
            not trade_signal.available or not euro_kelly_signal.available
        ):
            penalty += 0.07
            reasons.append("市场平衡高分依赖不完整临场数据")

        strong_upper_confirmation = (
            handicap_signal.available
            and bookmaker_consensus_signal.available
            and handicap_signal.score >= 0.40
            and bookmaker_consensus_signal.score >= 0.30
        )
        if strong_upper_confirmation and penalty > 0.20:
            penalty = 0.20
            reasons.append("亚盘水位和公司一致性同向确认，缺数据风险封顶")

        if depth >= 1.25 and euro_kelly_signal.available and euro_kelly_signal.score >= 0.28:
            asian_cover_weak = (
                (handicap_signal.available and handicap_signal.score < -0.10 and not fallback_handicap)
                or (bookmaker_consensus_signal.available and bookmaker_consensus_signal.score < -0.10)
            )
            if match.raw.get("_source") == "okooo" and asian_cover_weak:
                penalty *= 0.85
                reasons.append("深盘欧赔/Kelly只确认胜负，亚盘未确认时门槛扣分保留")
            else:
                penalty *= 0.55
                reasons.append("深盘欧赔/Kelly仍挺上盘，赢盘门槛扣分打折")

        score = -clamp(penalty, 0, 0.75)
        if not reasons:
            reasons.append("赢盘门槛风险不明显")
        return Signal(
            "赢盘门槛风险",
            score,
            WEIGHTS["cover_risk"],
            True,
            "；".join(reasons),
        )

    def _snapshot_trend_signal(self, match: Match, snapshot_context: SnapshotContext | None) -> Signal:
        if self.snapshot_store is None:
            return unavailable_signal("快照趋势", WEIGHTS["snapshot_trend"], "未启用本地快照目录")
        if snapshot_context is None or not snapshot_context.available:
            return unavailable_signal("快照趋势", WEIGHTS["snapshot_trend"], "本地快照不足 2 条")

        records = snapshot_context.records
        first_metrics = snapshot_metrics(records[0])
        previous_metrics = snapshot_metrics(records[-2])
        last_metrics = snapshot_metrics(records[-1])
        total_score_delta = last_metrics["score"] - first_metrics["score"]
        recent_score_delta = last_metrics["score"] - previous_metrics["score"]
        signal_trend_score = snapshot_context.signal_history_score
        signal_reason = snapshot_context.signal_history_reason
        trend_score = clamp(
            0.35 * clamp((0.60 * recent_score_delta + 0.40 * total_score_delta) / 0.25, -1, 1)
            + 0.65 * signal_trend_score,
            -1,
            1,
        )
        return Signal(
            "快照趋势",
            trend_score,
            WEIGHTS["snapshot_trend"],
            True,
            (
                f"本地 {len(records)} 条快照，近期score {recent_score_delta:+.3f}，"
                f"总变化 {total_score_delta:+.3f}；盘口路径已并入亚盘方向，不在此重复计分；"
                f"{signal_reason}"
            ),
        )

    def _market_elasticity_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        rows: list[HandicapRow],
        snapshot_context: SnapshotContext | None,
    ) -> Signal:
        heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        if abs(heat_edge) < 0.18:
            return Signal(
                "资金/盘口弹性",
                0.0,
                WEIGHTS["market_elasticity"],
                True,
                "必发热度未形成明显单边压力",
            )

        hot_direction = math.copysign(1.0, heat_edge)
        hot_team = upper_team if hot_direction > 0 else lower_team
        price_confirm = hot_direction * score_bifa_price_edge(match, upper_team, lower_team)
        payout_confirm = hot_direction * score_bifa_payout_edge(match, upper_team, lower_team)
        market_response, market_confidence, fair_move, market_moves = handicap_market_axis(match, rows, hot_team)
        recent_response, recent_confidence, recent_move, recent_reason = recent_handicap_path_axis(
            match,
            rows,
            upper_team,
            snapshot_context,
        )
        recent_hot_response = recent_response * hot_direction
        reaction = 0.0
        reasons: list[str] = [f"{hot_team}热度 {heat_edge:+.2f}"]
        okooo_source = match.raw.get("_source") == "okooo"

        if price_confirm >= 0.18:
            reaction += 0.10 if okooo_source else 0.20
            reasons.append("必发价格有确认")
        elif price_confirm <= -0.05:
            reaction -= 0.12 if okooo_source else 0.24
            reasons.append("必发价格未确认")

        if market_moves:
            response_weight = 0.52 if okooo_source else 0.42
            response_score = market_response
            if recent_confidence > 0 and abs(recent_move) >= 0.04:
                cumulative_hot_response = market_response
                response_score = 0.30 * cumulative_hot_response + 0.70 * recent_hot_response
            reaction += response_weight * response_score
            reasons.append(
                f"资金后等价公平盘口变化 {fair_move:+.3f}，"
                f"公司可信度 {market_confidence:.2f}，累计响应 {market_response:+.2f}"
            )
            if recent_confidence > 0 and abs(recent_move) >= 0.04:
                reasons.append(
                    f"临场资金后盘口响应 {recent_hot_response:+.2f}：{recent_reason}"
                )
        else:
            reaction -= 0.10
            reasons.append("资金后缺少可用盘口响应")

        if payout_confirm < -0.12:
            reaction -= 0.08 if okooo_source else 0.15
            reasons.append("盈亏压力未支持热门方")
        elif payout_confirm > 0.12:
            reaction += 0.04 if okooo_source else 0.08
            reasons.append("盈亏压力支持热门方")

        score = hot_direction * clamp(reaction, -1, 1)
        return Signal(
            "资金/盘口弹性",
            score,
            WEIGHTS["market_elasticity"],
            True,
            "；".join(reasons),
        )

    def _external_consensus_signal(self, match: Match, upper_team: str, lower_team: str) -> Signal:
        raw = match.raw
        components: list[tuple[float, str]] = []

        spread_upper = first_positive(
            raw.get("ExternalSpreadUpperPrice"),
            raw.get("ExtSpreadUpperPrice"),
            raw.get("OddsApiSpreadUpperPrice"),
        )
        spread_lower = first_positive(
            raw.get("ExternalSpreadLowerPrice"),
            raw.get("ExtSpreadLowerPrice"),
            raw.get("OddsApiSpreadLowerPrice"),
        )
        if match.raw.get("_source") == "okooo":
            spread_upper = spread_lower = 0.0
        if spread_upper > 0 and spread_lower > 0:
            spread_edge = score_bifa_odds_confirmation(spread_upper, spread_lower)
            components.append((0.38 * spread_edge, f"外部让球赔率 {spread_upper:.2f}/{spread_lower:.2f}"))

        h2h_upper = first_positive(
            raw.get("ExternalH2hUpperPrice"),
            raw.get("ExtH2hUpperPrice"),
            raw.get("OddsApiH2hUpperPrice"),
        )
        h2h_lower = first_positive(
            raw.get("ExternalH2hLowerPrice"),
            raw.get("ExtH2hLowerPrice"),
            raw.get("OddsApiH2hLowerPrice"),
        )
        if h2h_upper > 0 and h2h_lower > 0:
            h2h_edge = score_bifa_odds_confirmation(h2h_upper, h2h_lower)
            components.append((0.24 * h2h_edge, f"外部胜负赔率 {h2h_upper:.2f}/{h2h_lower:.2f}"))

        fair_depth = first_positive(
            raw.get("ExternalFairLineDepth"),
            raw.get("ExtFairLineDepth"),
            raw.get("ModelFairLineDepth"),
        )
        if fair_depth > 0:
            fair_gap = clamp((fair_depth - line_depth(match.asian_line)) / 0.85, -1, 1)
            components.append((0.22 * fair_gap, f"外部合理盘口 {fair_depth:.2f}"))

        power_edge = optional_float(
            raw.get("ExternalPowerEdge"),
            raw.get("ExtPowerEdge"),
            raw.get("EloPowerEdge"),
            raw.get("ModelPowerEdge"),
        )
        if power_edge is not None:
            components.append((0.16 * clamp(power_edge, -1, 1), f"外部实力差 {power_edge:+.2f}"))

        if not components:
            return unavailable_signal(
                "外部赔率/实力校验",
                WEIGHTS["external_consensus"],
                "未接入外部赔率、Elo或伤停模型字段",
            )

        score = clamp(sum(value for value, _ in components) / sum(
            0.38 if reason.startswith("外部让球") else
            0.24 if reason.startswith("外部胜负") else
            0.22 if reason.startswith("外部合理") else
            0.16
            for _, reason in components
        ), -1, 1)
        reason_parts = [reason for _, reason in components]
        if raw.get("_source") == "okooo":
            # 澳客适配层里的 External*/Model* 多数仍来自同一组澳客盘口/指数，
            # 只能作为交叉视角，不能按真正外部源满权重确认。
            score = clamp(score * 0.55, -0.18, 0.18)
            reason_parts.append("澳客同源校验已降权")
        return Signal(
            "外部赔率/实力校验",
            score,
            WEIGHTS["external_consensus"],
            True,
            f"{upper_team} vs {lower_team}；" + "；".join(reason_parts),
        )

    def _water_value_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        rows: list[HandicapRow],
        bifa_signal: Signal,
        trade_signal: Signal,
        euro_kelly_signal: Signal,
        fair_line_signal: Signal,
        handicap_signal: Signal,
        bookmaker_consensus_signal: Signal,
        depth_profile_signal: Signal,
        snapshot_trend_signal: Signal,
        market_elasticity_signal: Signal,
        external_consensus_signal: Signal,
    ) -> Signal:
        upper_water = average_team_water(rows, match, upper_team)
        lower_water = average_team_water(rows, match, lower_team)
        if upper_water <= 1.0 or lower_water <= 1.0:
            return unavailable_signal("高低水价值", WEIGHTS["water_value"], "缺少可用上下盘水位")

        upper_market_prob, lower_market_prob = normalized_probabilities(upper_water, lower_water)
        model_edge = model_edge_for_water_value(
            [
                bifa_signal,
                trade_signal,
                euro_kelly_signal,
                fair_line_signal,
                depth_profile_signal,
                snapshot_trend_signal,
                market_elasticity_signal,
                external_consensus_signal,
            ]
        )
        model_upper_prob = clamp(0.50 + 0.18 * model_edge, 0.32, 0.68)
        value_gap = model_upper_prob - upper_market_prob
        score = clamp(value_gap / 0.08, -1, 1)
        reasons = [
            f"{upper_team}水位 {upper_water:.3g} 隐含 {upper_market_prob:.1%}",
            f"{lower_team}水位 {lower_water:.3g} 隐含 {lower_market_prob:.1%}",
            f"模型估计上盘 {model_upper_prob:.1%}",
        ]

        high_side = ""
        high_side_gap = 0.0
        if upper_water >= 2.00 and upper_water >= lower_water + 0.08:
            high_side = "上盘"
            high_side_gap = value_gap
        elif lower_water >= 2.00 and lower_water >= upper_water + 0.08:
            high_side = "下盘"
            high_side_gap = -value_gap

        if high_side:
            high_team = upper_team if high_side == "上盘" else lower_team
            high_water = upper_water if high_side == "上盘" else lower_water
            if high_side_gap >= 0.035:
                reasons.append(f"{high_side}{high_team}高水 {high_water:.3g} 有赔率补偿")
            else:
                penalty = clamp(0.10 + max(0.0, 0.035 - high_side_gap) * 2.0, 0.10, 0.28)
                if high_side == "上盘":
                    score = clamp(score - penalty, -1, 1)
                    score = min(score, -0.05 if high_side_gap < 0.02 else 0.05)
                else:
                    score = clamp(score + penalty, -1, 1)
                    score = max(score, 0.05 if high_side_gap < 0.02 else -0.05)
                reasons.append(f"{high_side}{high_team}高水 {high_water:.3g} 缺少价值补偿，扣分 {penalty:.2f}")

        low_side = ""
        low_side_gap = 0.0
        if 1.0 < upper_water <= 1.78 and upper_water + 0.08 <= lower_water:
            low_side = "上盘"
            low_side_gap = value_gap
        elif 1.0 < lower_water <= 1.78 and lower_water + 0.08 <= upper_water:
            low_side = "下盘"
            low_side_gap = -value_gap

        if low_side:
            low_team = upper_team if low_side == "上盘" else lower_team
            low_water = upper_water if low_side == "上盘" else lower_water
            if low_side_gap >= 0.02:
                reasons.append(f"{low_side}{low_team}低水 {low_water:.3g} 仍有模型溢价")
            else:
                adjustment = 0.10 if low_side == "上盘" else -0.10
                if (
                    low_side == "上盘"
                    and handicap_signal.available
                    and bookmaker_consensus_signal.available
                    and handicap_signal.score >= 0.40
                    and bookmaker_consensus_signal.score >= 0.30
                ):
                    adjustment *= 0.30
                    reasons.append("亚盘水位和公司一致性确认上盘，低水偏贵惩罚降权")
                score = clamp(score - adjustment, -1, 1)
                reasons.append(f"{low_side}{low_team}低水 {low_water:.3g} 偏贵，需更强确认")

        if not high_side and not low_side:
            score = clamp(score, -0.08, 0.08)
            reasons.append("常规水位，仅做极弱修正")

        return Signal(
            "高低水价值",
            score,
            WEIGHTS["water_value"],
            True,
            "；".join(reasons),
        )

    def _market_balance_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        bifa_signal: Signal,
        trade_signal: Signal,
        handicap_signal: Signal,
        euro_kelly_signal: Signal,
        draw_risk_signal: Signal,
        fair_line_signal: Signal,
        depth_profile_signal: Signal,
        snapshot_trend_signal: Signal,
        market_elasticity_signal: Signal,
        external_consensus_signal: Signal,
        water_value_signal: Signal,
        snapshot_context: SnapshotContext | None,
    ) -> Signal:
        if not bifa_signal.available:
            return unavailable_signal("市场平衡/背离", WEIGHTS["market_balance"], "缺少必发基础热度，无法判断盘口防守")
        if not (trade_signal.available or handicap_signal.available or euro_kelly_signal.available):
            return unavailable_signal("市场平衡/背离", WEIGHTS["market_balance"], "缺少成交/亚盘/欧赔确认，无法判断盘口防守")

        heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        bifa_price_edge = score_bifa_price_edge(match, upper_team, lower_team)
        handicap_edge = handicap_signal.score if handicap_signal.available else 0.0
        handicap_is_fallback = "静态亚盘均值兜底" in handicap_signal.reason
        trade_edge = trade_signal.score if trade_signal.available else 0.0
        euro_edge = euro_kelly_signal.score if euro_kelly_signal.available else 0.0
        draw_edge = draw_risk_signal.score if draw_risk_signal.available else 0.0
        fair_edge = fair_line_signal.score if fair_line_signal.available else 0.0
        depth_edge = depth_profile_signal.score if depth_profile_signal.available else 0.0
        elasticity_edge = market_elasticity_signal.score if market_elasticity_signal.available else 0.0
        external_edge = external_consensus_signal.score if external_consensus_signal.available else 0.0
        water_value_edge = water_value_signal.score if water_value_signal.available else 0.0
        depth = line_depth(match.asian_line)
        okooo_core_only = match.raw.get("_source") == "okooo"

        components: list[float] = []
        reasons: list[str] = []
        hot_side = upper_team if heat_edge >= 0 else lower_team
        hot_direction = math.copysign(1.0, heat_edge) if heat_edge else 0.0

        if abs(heat_edge) >= 0.22:
            price_confirm = hot_direction * bifa_price_edge
            handicap_confirm = hot_direction * handicap_edge
            trade_confirm = hot_direction * trade_edge
            euro_confirm = hot_direction * euro_edge
            fair_confirm = hot_direction * fair_edge
            depth_confirm = hot_direction * depth_edge
            elasticity_confirm = hot_direction * elasticity_edge
            external_confirm = hot_direction * external_edge
            water_value_confirm = hot_direction * water_value_edge
            if okooo_core_only:
                price_pos_w, price_neg_w = 0.10, 0.14
                handicap_pos_w, handicap_neg_w, handicap_neutral_w = 0.48, 0.56, 0.22
                trade_pos_w, trade_neg_w = 0.30, 0.34
                euro_pos_w, euro_neg_w = 0.06, 0.06
                fair_pos_w, fair_neg_w = 0.08, 0.10
                elasticity_pos_w, elasticity_neg_w = 0.28, 0.32
            else:
                price_pos_w, price_neg_w = 0.25, 0.35
                handicap_pos_w, handicap_neg_w, handicap_neutral_w = 0.35, 0.45, 0.15
                trade_pos_w, trade_neg_w = 0.20, 0.25
                euro_pos_w, euro_neg_w = 0.15, 0.15
                fair_pos_w, fair_neg_w = 0.12, 0.16
                elasticity_pos_w, elasticity_neg_w = 0.16, 0.20

            if price_confirm >= 0.10:
                components.append(price_pos_w * hot_direction)
                reasons.append(f"{hot_side}热度获必发价格确认")
            else:
                components.append(-price_neg_w * hot_direction)
                reasons.append(f"{hot_side}热度未获必发价格确认")

            if handicap_signal.available:
                if handicap_confirm >= 0.08:
                    weight = 0.10 if handicap_is_fallback else handicap_pos_w
                    components.append(weight * hot_direction)
                    reasons.append("亚盘低水/降水确认热门方" + ("(静态均值降权)" if handicap_is_fallback else ""))
                elif handicap_confirm <= -0.08:
                    weight = 0.12 if handicap_is_fallback else handicap_neg_w
                    components.append(-weight * hot_direction)
                    reasons.append("亚盘升水或分歧，热门方买入更危险" + ("(静态均值降权)" if handicap_is_fallback else ""))
                else:
                    weight = 0.05 if handicap_is_fallback else handicap_neutral_w
                    components.append(-weight * hot_direction)
                    reasons.append("亚盘对热门方防守不足" + ("(静态均值降权)" if handicap_is_fallback else ""))

            if trade_signal.available:
                if trade_confirm >= 0.10:
                    components.append(trade_pos_w * hot_direction)
                    reasons.append("成交走势顺热度")
                elif trade_confirm <= -0.10:
                    components.append(-trade_neg_w * hot_direction)
                    reasons.append("成交走势反热度")

            if euro_kelly_signal.available:
                if euro_confirm >= 0.10:
                    components.append(euro_pos_w * hot_direction)
                    reasons.append("欧赔/Kelly同步")
                elif euro_confirm <= -0.10:
                    components.append(-euro_neg_w * hot_direction)
                    reasons.append("欧赔/Kelly背离")

            if fair_line_signal.available:
                if fair_confirm >= 0.10:
                    components.append(fair_pos_w * hot_direction)
                    reasons.append("盘口深度与价格匹配")
                elif fair_confirm <= -0.10:
                    components.append(-fair_neg_w * hot_direction)
                    reasons.append("盘口相对价格偏深/偏危险")

            if depth_profile_signal.available and not okooo_core_only:
                if depth_confirm >= 0.10:
                    components.append((0.10 if depth <= 0.5 else 0.16) * hot_direction)
                    reasons.append("盘口深度模型确认")
                elif depth_confirm <= -0.10:
                    components.append((-0.12 if depth <= 0.5 else -0.22) * hot_direction)
                    reasons.append("盘口深度模型背离")

            if market_elasticity_signal.available:
                if elasticity_confirm >= 0.12:
                    weight = elasticity_pos_w
                    if trade_confirm <= -0.10 and euro_confirm <= -0.10:
                        weight = 0.12 if okooo_core_only else 0.06
                        reasons.append("资金/盘口弹性确认热门方但成交/Kelly仍背离，降权")
                    else:
                        reasons.append("资金/盘口弹性确认热门方")
                    components.append(weight * hot_direction)
                elif elasticity_confirm <= -0.12:
                    weight = elasticity_neg_w
                    if trade_confirm >= 0.10 and euro_confirm >= 0.10:
                        weight = 0.14 if okooo_core_only else 0.08
                        reasons.append("资金/盘口弹性背离热门方但成交/Kelly同步，降权")
                    else:
                        reasons.append("资金/盘口弹性背离热门方")
                    components.append(-weight * hot_direction)

            if external_consensus_signal.available and not okooo_core_only:
                if external_confirm >= 0.12:
                    components.append(0.12 * hot_direction)
                    reasons.append("外部赔率/实力同步")
                elif external_confirm <= -0.12:
                    components.append(-0.14 * hot_direction)
                    reasons.append("外部赔率/实力背离")

            if water_value_signal.available and not okooo_core_only:
                if water_value_confirm >= 0.30:
                    components.append(0.10 * hot_direction)
                    reasons.append("高低水价值确认热门方")
                elif water_value_confirm <= -0.30:
                    components.append(-0.14 * hot_direction)
                    reasons.append("高低水价值背离热门方")

            if draw_risk_signal.available and depth <= 0.5 and hot_direction > 0 and draw_edge <= -0.18:
                components.append(draw_edge * 0.45)
                reasons.append("浅盘热门存在平局风险")

        else:
            if handicap_signal.available and abs(handicap_edge) >= 0.20:
                components.append(0.45 * math.copysign(1.0, handicap_edge))
                reasons.append(f"热度不高但亚盘主动防守偏{direction_label(handicap_edge)}")
            if trade_signal.available and abs(trade_edge) >= 0.25:
                components.append(0.25 * math.copysign(1.0, trade_edge))
                reasons.append(f"热度不高但成交走势偏{direction_label(trade_edge)}")
            if euro_kelly_signal.available and abs(euro_edge) >= 0.20:
                components.append(0.15 * math.copysign(1.0, euro_edge))
                reasons.append(f"热度不高但欧赔/Kelly偏{direction_label(euro_edge)}")
            if fair_line_signal.available and abs(fair_edge) >= 0.20:
                components.append(0.15 * math.copysign(1.0, fair_edge))
                reasons.append(f"热度不高但盘口合理性偏{direction_label(fair_edge)}")
            if market_elasticity_signal.available and abs(elasticity_edge) >= 0.20:
                components.append(0.16 * math.copysign(1.0, elasticity_edge))
                reasons.append(f"热度不高但资金/盘口弹性偏{direction_label(elasticity_edge)}")
            if external_consensus_signal.available and abs(external_edge) >= 0.18 and not okooo_core_only:
                components.append(0.12 * math.copysign(1.0, external_edge))
                reasons.append(f"热度不高但外部校验偏{direction_label(external_edge)}")
            if water_value_signal.available and abs(water_value_edge) >= 0.30 and not okooo_core_only:
                components.append(0.12 * math.copysign(1.0, water_value_edge))
                reasons.append(f"热度不高但高低水价值偏{direction_label(water_value_edge)}")

        if not components:
            return Signal(
                "市场平衡/背离",
                0.0,
                WEIGHTS["market_balance"],
                True,
                "热度和盘口防守均不明显",
            )

        score = clamp(sum(components), -1, 1)
        upper_trap_pressure = upper_favorite_trap_pressure_from_values(
            match=match,
            heat_edge=heat_edge,
            trade_available=trade_signal.available,
            trade_edge=trade_edge,
            euro_edge=euro_edge,
            handicap_edge=handicap_edge,
            fair_edge=fair_edge,
            depth_edge=depth_edge,
            cover_edge=0.0,
            draw_edge=draw_edge,
            bifa_reason=bifa_signal.reason,
        )
        if upper_trap_pressure > 0:
            score = clamp(score - upper_trap_pressure, -1, 1)
            if okooo_core_only:
                if trade_edge >= 0.10 or handicap_edge >= 0.08 or elasticity_edge >= 0.12:
                    cap = 0.10
                elif handicap_edge <= -0.08 or elasticity_edge <= -0.12:
                    cap = -0.10
                else:
                    cap = 0.02
            elif depth <= 0.5:
                cap = -0.10 if (euro_edge <= -0.10 or fair_edge <= -0.10) else 0.08
            elif depth <= 1.25:
                cap = -0.08 if euro_edge <= 0.02 else 0.10
            else:
                cap = -0.12 if fair_edge <= -0.05 else 0.06
            score = min(score, cap)
            reasons.append(f"上盘大热但成交/欧赔/盘口未形成确认，热门陷阱降权 {upper_trap_pressure:.2f}")
        direction_signals = [
            bifa_signal,
            trade_signal,
            handicap_signal,
            euro_kelly_signal,
            fair_line_signal,
            market_elasticity_signal,
        ]
        if not okooo_core_only:
            direction_signals.extend([depth_profile_signal, external_consensus_signal, water_value_signal])
        same_direction_count = count_same_direction(direction_signals)
        conflicts = signal_conflict_count(direction_signals)
        if abs(score) < 0.08 and conflicts >= 2:
            reasons.append("正反信号抵消，市场平衡中性")
        elif same_direction_count >= 3 and score > 0:
            score = clamp(score * 1.10, -1, 1)
            reasons.append("多信号同向偏上盘")
        elif same_direction_count <= -3 and score < 0:
            score = clamp(score * 1.10, -1, 1)
            reasons.append("多信号同向偏下盘")
        elif conflicts >= 3:
            score = clamp(score * 0.65, -1, 1)
            reasons.append("信号互相矛盾，降权")

        if score > 0 and hot_direction > 0 and depth >= 0.75 and not trade_signal.available and euro_edge <= 0.02:
            if euro_edge <= -0.05:
                cap = 0.18
            elif handicap_edge >= 0.20 and snapshot_trend_signal.available and snapshot_trend_signal.score >= 0.12:
                cap = 0.35
            else:
                cap = 0.24
            if score > cap:
                score = cap
                reasons.append("缺成交且欧赔/Kelly未确认上盘，市场平衡封顶")

        if (
            score > 0.55
            and depth >= 0.75
            and (not trade_signal.available or not euro_kelly_signal.available)
            and (not snapshot_trend_signal.available or snapshot_trend_signal.score < 0.08)
        ):
            cap = 0.55 if depth <= 1.25 else 0.45
            score = min(score, cap)
            reasons.append("中深盘临场成交/Kelly缺失且快照未增强，市场平衡封顶")

        return Signal(
            "市场平衡/背离",
            score,
            WEIGHTS["market_balance"],
            True,
            "；".join(reasons),
        )


def parse_match(item: dict[str, Any]) -> Match:
    if "eventId" in item:
        return parse_newspdex_match(item)

    if isinstance(item.get("Match"), dict) and isinstance(item.get("BaseInfo"), dict):
        match_info = item["Match"]
        base_info = item["BaseInfo"]
        raw = dict(base_info)
        raw["Match"] = match_info
        return Match(
            event_id=int(match_info["EventId"]),
            match_time=parse_datetime(match_info["MatchTime"]),
            home=str(match_info.get("HomeTeam", "")),
            away=str(match_info.get("GuestTeam", match_info.get("AwayTeam", ""))),
            league_id=to_int_or_none(match_info.get("LeagueId")),
            league_name=str(match_info.get("MatchPath", match_info.get("LeagueName", ""))),
            asian_line=str(base_info.get("AsianAvrLet", "0") or "0"),
            is_stop_update=bool(item.get("IsStopUpdate", False)),
            raw=raw,
        )

    event_id = int(item["EventId"])
    match_time = parse_datetime(item["MatchTime"])
    return Match(
        event_id=event_id,
        match_time=match_time,
        home=str(item.get("HomeTeam", "")),
        away=str(item.get("AwayTeam", "")),
        league_id=to_int_or_none(item.get("LeagueId")),
        league_name=str(item.get("SortName", "")),
        asian_line=str(item.get("AsianAvrLet", "0")),
        is_stop_update=bool(item.get("IsStopUpdate", False)),
        raw=item,
    )


def parse_newspdex_match(item: dict[str, Any]) -> Match:
    event_id = int(item["eventId"])
    asian_line = normalize_newspdex_line(item.get("handicap", item.get("asianLetLine", "0")))
    raw = dict(item)
    raw["_source"] = "newspdex"
    bf_index = item.get("bfIndex")
    bf_amounts = item.get("bfAmounts")
    poly_index = item.get("polyIndex")
    asian_let = item.get("asianLet")
    handicap_odds = item.get("handicapOdds")
    raw.update(
        {
            "EventId": event_id,
            "MatchTime": item.get("matchTime"),
            "HomeTeam": item.get("homeTeam", ""),
            "AwayTeam": item.get("awayTeam", ""),
            "LeagueId": item.get("leagueId"),
            "SortName": item.get("leagueName", ""),
            "LeagueName": item.get("leagueName", ""),
            "MatchPath": item.get("leagueName", ""),
            "AsianAvrLet": asian_line,
            "BfIndexHome": amount_to_float(list_get(bf_index, 0)),
            "BfIndexDraw": amount_to_float(list_get(bf_index, 1)),
            "BfIndexAway": amount_to_float(list_get(bf_index, 2)),
            "BfAmountHome": amount_to_float(list_get(bf_amounts, 0)),
            "BfAmountDraw": amount_to_float(list_get(bf_amounts, 1)),
            "BfAmountAway": amount_to_float(list_get(bf_amounts, 2)),
            "BfOddsHome": to_float_or_none(item.get("bfPriceHome")) or 0.0,
            "BfOddsDraw": to_float_or_none(item.get("bfPriceDraw")) or 0.0,
            "BfOddsAway": to_float_or_none(item.get("bfPriceAway")) or 0.0,
            "EuroAvrHome": to_float_or_none(item.get("euroHome")) or 0.0,
            "EuroAvrDraw": to_float_or_none(item.get("euroDraw")) or 0.0,
            "EuroAvrAway": to_float_or_none(item.get("euroAway")) or 0.0,
            "KellyHome": to_float_or_none(item.get("kellyHome")) or 0.0,
            "KellyDraw": to_float_or_none(item.get("kellyDraw")) or 0.0,
            "KellyAway": to_float_or_none(item.get("kellyAway")) or 0.0,
            "AsianAvrHome": to_float_or_none(list_get(asian_let, 0))
            or to_float_or_none(list_get(handicap_odds, 0))
            or 0.0,
            "AsianAvrAway": to_float_or_none(list_get(asian_let, 1))
            or to_float_or_none(list_get(handicap_odds, 2))
            or 0.0,
            "PolyIndexHome": amount_to_float(list_get(poly_index, 0)),
            "PolyIndexDraw": amount_to_float(list_get(poly_index, 1)),
            "PolyIndexAway": amount_to_float(list_get(poly_index, 2)),
        }
    )
    return Match(
        event_id=event_id,
        match_time=parse_datetime(str(item["matchTime"])),
        home=str(item.get("homeTeam", "")),
        away=str(item.get("awayTeam", "")),
        league_id=to_int_or_none(item.get("leagueId")),
        league_name=str(item.get("leagueName", "")),
        asian_line=asian_line,
        is_stop_update=bool(item.get("isStopUpdate", False)) or str(item.get("status", "")) == "finished",
        raw=raw,
    )


def newspdex_upcoming_dates(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    local = now.astimezone()
    dates = ["today-window"]
    for offset in range(3):
        day = local + timedelta(days=offset)
        dates.append(day.strftime("%Y-%m-%d"))
    return list(dict.fromkeys(dates))


def detail_rows_by_key(payload: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    rows = payload.get(section)
    if not isinstance(rows, list):
        return {}
    return {str(row.get("key")): row for row in rows if isinstance(row, dict)}


def enrich_newspdex_detail_raw(raw: dict[str, Any], payload: dict[str, Any]) -> None:
    standard = detail_rows_by_key(payload, "standard")
    key_map = {"home": "Home", "draw": "Draw", "away": "Away"}
    for row_key, legacy_key in key_map.items():
        row = standard.get(row_key)
        if not row:
            continue
        raw[f"BfIndex{legacy_key}"] = amount_to_float(row.get("bfIndex", raw.get(f"BfIndex{legacy_key}", 0)))
        raw[f"BfAmount{legacy_key}"] = amount_to_float(
            row.get("turnover", raw.get(f"BfAmount{legacy_key}", 0))
        )
        raw[f"BfPayout{legacy_key}"] = amount_to_float(row.get("pnl", raw.get(f"BfPayout{legacy_key}", 0)))
        raw[f"BfOdds{legacy_key}"] = to_float_or_none(row.get("price")) or raw.get(f"BfOdds{legacy_key}", 0)
        raw[f"EuroAvr{legacy_key}"] = to_float_or_none(row.get("euroAvg")) or raw.get(
            f"EuroAvr{legacy_key}", 0
        )
    handicap = detail_rows_by_key(payload, "handicap")
    if handicap:
        home = handicap.get("home")
        away = handicap.get("away")
        line = handicap.get("line")
        if home:
            raw["AsianAvrHome"] = to_float_or_none(home.get("price")) or raw.get("AsianAvrHome", 0)
            raw["HandicapBfIndexHome"] = amount_to_float(home.get("bfIndex", 0))
            raw["HandicapBfAmountHome"] = amount_to_float(home.get("turnover", 0))
        if away:
            raw["AsianAvrAway"] = to_float_or_none(away.get("price")) or raw.get("AsianAvrAway", 0)
            raw["HandicapBfIndexAway"] = amount_to_float(away.get("bfIndex", 0))
            raw["HandicapBfAmountAway"] = amount_to_float(away.get("turnover", 0))
        if line and line.get("price") not in (None, ""):
            raw["AsianAvrLet"] = normalize_newspdex_line(line.get("price"))


def merge_match_detail(base: Match, detail: Match) -> Match:
    raw = dict(base.raw)
    for key, value in detail.raw.items():
        old = raw.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)) and value == 0 and old not in (None, "", 0):
            continue
        raw[key] = value
    asian_line = detail.asian_line if line_depth(detail.asian_line) > 0 or line_depth(base.asian_line) == 0 else base.asian_line
    return Match(
        event_id=base.event_id,
        match_time=detail.match_time if detail.match_time else base.match_time,
        home=detail.home or base.home,
        away=detail.away or base.away,
        league_id=detail.league_id if detail.league_id is not None else base.league_id,
        league_name=detail.league_name or base.league_name,
        asian_line=asian_line,
        is_stop_update=base.is_stop_update or detail.is_stop_update,
        raw=raw,
    )


def match_to_dict(match: Match) -> dict[str, Any]:
    return {
        "event_id": match.event_id,
        "match_time": match.match_time.isoformat(),
        "home": match.home,
        "away": match.away,
        "league_id": match.league_id,
        "league_name": match.league_name,
        "asian_line": match.asian_line,
        "is_stop_update": match.is_stop_update,
        "raw": match.raw,
    }


def match_from_dict(data: dict[str, Any]) -> Match:
    """从 SnapshotStore / jsonl 的 ``match`` 字段还原 ``Match``（用于离线重放）。"""
    mt = data.get("match_time", "")
    if isinstance(mt, str):
        text = mt
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        mtime = datetime.fromisoformat(text)
        if mtime.tzinfo is None:
            mtime = mtime.replace(tzinfo=timezone.utc)
        else:
            mtime = mtime.astimezone(timezone.utc)
    else:
        mtime = datetime.now(timezone.utc)
    return Match(
        event_id=int(data.get("event_id", 0)),
        match_time=mtime,
        home=str(data.get("home", "")),
        away=str(data.get("away", "")),
        league_id=to_int_or_none(data.get("league_id")),
        league_name=str(data.get("league_name", "")),
        asian_line=str(data.get("asian_line", "0")),
        is_stop_update=bool(data.get("is_stop_update", False)),
        raw=dict(data.get("raw") or {}),
    )


@dataclass
class OkoooSnapshotReplayClient:
    """用同一 event 的 jsonl 重放离线预测输入，尽量复用保存快照时的盘口/欧赔结构。"""

    records: list[dict[str, Any]]

    def __post_init__(self) -> None:
        self._records = sorted(self.records, key=lambda r: str(r.get("fetched_at", "")))

    @staticmethod
    def _euro_point_from_raw(raw: dict[str, Any]) -> EuroTrendPoint:
        return EuroTrendPoint(
            refresh_time=None,
            home_price=float_or_zero(raw.get("EuroAvrHome")),
            draw_price=float_or_zero(raw.get("EuroAvrDraw")),
            away_price=float_or_zero(raw.get("EuroAvrAway")),
            home_kelly=float_or_zero(raw.get("KellyHome")),
            draw_kelly=float_or_zero(raw.get("KellyDraw")),
            away_kelly=float_or_zero(raw.get("KellyAway")),
        )

    def _last_raw(self) -> dict[str, Any]:
        if not self._records:
            return {}
        raw = (self._records[-1].get("match") or {}).get("raw") or {}
        return raw if isinstance(raw, dict) else {}

    def handicap_list(self, event_id: int, _asian_line: str) -> list[HandicapRow]:
        del event_id
        row_data = self._last_raw().get("_okooo_handicap_row_data")
        if not isinstance(row_data, list):
            return []
        rows = [handicap_row_from_dict(item) for item in row_data if isinstance(item, dict)]
        return sorted(rows, key=bookmaker_priority)

    def euro_trend(self, event_id: int) -> list[EuroTrendPoint]:
        del event_id
        saved_points = self._last_raw().get("_okooo_euro_trend_points")
        if isinstance(saved_points, list):
            points = [euro_trend_point_from_dict(item) for item in saved_points if isinstance(item, dict)]
            if points:
                return points
        if not self._records:
            return []
        raw_first = (self._records[0].get("match") or {}).get("raw") or {}
        raw_last = (self._records[-1].get("match") or {}).get("raw") or {}
        if not isinstance(raw_first, dict):
            raw_first = {}
        if not isinstance(raw_last, dict):
            raw_last = {}
        a = self._euro_point_from_raw(raw_first)
        b = self._euro_point_from_raw(raw_last)
        return [a, b]

    def price_volume(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        del event_id
        saved = self._last_raw().get("_okooo_price_volume_points")
        if not isinstance(saved, dict):
            return []
        rows = saved.get(selection)
        if not isinstance(rows, list):
            return []
        return [price_volume_point_from_dict(item) for item in rows if isinstance(item, dict)]


def parse_handicap(item: dict[str, Any], bookmaker_id: int | None = None) -> HandicapRow:
    init_line_known = any(key in item for key in ("InitLet", "Let", "AsianAvrLet"))
    latest_line_known = any(key in item for key in ("Let", "AsianAvrLet"))
    return HandicapRow(
        bookmaker_id=int(item.get("BookmakerId", bookmaker_id or 0)),
        name=str(item.get("Name", bookmaker_id or "")),
        sec_a=float(item.get("SecA", 0.0)),
        sec_b=float(item.get("SecB", 0.0)),
        init_sec_a=float(item.get("InitSecA", item.get("SecA", 0.0))),
        init_sec_b=float(item.get("InitSecB", item.get("SecB", 0.0))),
        payout=float(item.get("Payout", 0.0)),
        update_time=parse_datetime_or_none(item.get("UpdateTime")),
        source="live",
        init_line=float_or_zero(item.get("InitLet", item.get("Let", item.get("AsianAvrLet", 0.0)))),
        latest_line=float_or_zero(item.get("Let", item.get("AsianAvrLet", 0.0))),
        init_line_known=init_line_known,
        latest_line_known=latest_line_known,
    )


def parse_price_volume(item: dict[str, Any]) -> PriceVolumePoint:
    return PriceVolumePoint(
        price=float(item.get("Price", 0.0)),
        volume=float(item.get("Volumn", 0.0)),
        update_time=parse_datetime_or_none(item.get("UpdateTime")),
        attr=item.get("Attr"),
    )


def parse_euro_trend(item: dict[str, Any]) -> EuroTrendPoint:
    return EuroTrendPoint(
        refresh_time=parse_datetime_or_none(item.get("RefreshTime")),
        home_price=float(item.get("HomePrice", 0.0)),
        draw_price=float(item.get("DrawPrice", 0.0)),
        away_price=float(item.get("AwayPrice", 0.0)),
        home_kelly=float(item.get("HomeKelly", 0.0)),
        draw_kelly=float(item.get("DrawKelly", 0.0)),
        away_kelly=float(item.get("AwayKelly", 0.0)),
    )


def parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_datetime_or_none(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return parse_datetime(str(value))
    except ValueError:
        return None


def to_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def to_float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def list_get(values: Any, index: int, default: Any = 0) -> Any:
    if isinstance(values, (list, tuple)) and len(values) > index:
        return values[index]
    return default


def normalize_newspdex_line(value: Any) -> str:
    if value in (None, ""):
        return "0"
    text = str(value).strip()
    numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", text)
    if "/" in text and len(numbers) >= 2:
        try:
            return normalize_line_for_spdex(str((float(numbers[0]) + float(numbers[1])) / 2))
        except ValueError:
            pass
    if numbers:
        return normalize_line_for_spdex(numbers[0])
    return normalize_line_for_spdex(text)


def amount_to_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    multiplier = 1.0
    if text.upper().endswith("M"):
        multiplier = 1_000_000.0
        text = text[:-1]
    elif text.upper().endswith("K"):
        multiplier = 1_000.0
        text = text[:-1]
    try:
        return float(text) * multiplier
    except ValueError:
        return 0.0


def is_ssl_verify_error(exc: BaseException) -> bool:
    reason = getattr(exc, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return "CERTIFICATE_VERIFY_FAILED" in str(exc)


def is_world_cup(match: Match) -> bool:
    return match.league_id == WORLD_CUP_LEAGUE_ID or match.league_name == "世界杯"


def normalize_line_for_spdex(line: str) -> str:
    cleaned = str(line).strip()
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    if cleaned in ("", "-"):
        return "0"
    try:
        value = float(cleaned)
    except ValueError:
        return cleaned
    if value == 0:
        return "0"
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text


def line_value(line: str) -> float:
    try:
        return float(normalize_line_for_spdex(line))
    except ValueError:
        return 0.0


def line_depth(line: str) -> float:
    return abs(line_value(line))


def line_depth_category(depth: float) -> str:
    if depth <= 0.5:
        return "平浅盘"
    if depth <= 1.25:
        return "中盘"
    if depth < 2.0:
        return "深盘"
    return "超深盘"


def upper_lower_teams(match: Match) -> tuple[str, str]:
    value = line_value(match.asian_line)
    if value < 0:
        return match.home, match.away
    if value > 0:
        return match.away, match.home
    return match.home, match.away


def side_team(side: str, upper_team: str, lower_team: str) -> str:
    if side == "上盘":
        return upper_team
    if side == "下盘":
        return lower_team
    return ""


def direction_label(score: float) -> str:
    return "上盘" if score > 0 else "下盘"


def side_key(match: Match, team: str) -> str:
    if team == match.home:
        return "Home"
    if team == match.away:
        return "Away"
    return "Unknown"


def selection_for_team(match: Match, team: str) -> str:
    key = side_key(match, team)
    if key == "Home":
        return "home"
    if key == "Away":
        return "away"
    raise DataError(f"cannot map team {team} to Betfair selection")


def first_positive(*values: Any) -> float:
    for value in values:
        numeric = float_or_zero(value)
        if numeric > 0:
            return numeric
    return 0.0


def normalized_probabilities(*prices: float) -> tuple[float, ...]:
    probabilities = [1.0 / price if price > 0 else 0.0 for price in prices]
    total = sum(probabilities)
    if total <= 0:
        return tuple(0.0 for _ in prices)
    return tuple(probability / total for probability in probabilities)


def fair_handicap_depth(price_edge: float) -> float:
    if price_edge <= 0:
        return 0.0
    return clamp(price_edge * 2.35, 0.0, 2.5)


def fair_handicap_depth_from_probabilities(upper_prob: float, draw_prob: float, lower_prob: float) -> float:
    if upper_prob <= lower_prob:
        return 0.0
    dominance = max(upper_prob - lower_prob, 0.0)
    win_over_draw = max(upper_prob - draw_prob, 0.0)
    draw_drag = clamp((draw_prob - 0.18) / 0.18, 0, 1)
    raw_depth = (1.35 * dominance + 1.05 * win_over_draw) * (1 - 0.35 * draw_drag)
    if upper_prob < 0.52:
        cap = 0.50
    elif upper_prob < 0.58:
        cap = 0.75
    elif upper_prob < 0.65:
        cap = 1.25
    elif upper_prob < 0.75:
        cap = 1.75
    else:
        cap = 2.50
    return clamp(raw_depth, 0.0, cap)


def average_upper_water(rows: list[HandicapRow], match: Match, upper_team: str) -> float:
    return average_team_water(rows, match, upper_team)


def average_team_water(rows: list[HandicapRow], match: Match, team: str) -> float:
    values: list[float] = []
    team_is_home = team == match.home
    for row in rows:
        value = row.sec_a if team_is_home else row.sec_b
        if value > 0:
            values.append(value)
    if values:
        return sum(values) / len(values)
    team_key = side_key(match, team)
    return first_positive(match.raw.get(f"AsianAvr{team_key}"))


def average_upper_water_movement(
    rows: list[HandicapRow],
    match: Match,
    upper_team: str,
) -> tuple[float, float, float]:
    return average_team_water_movement(rows, match, upper_team)


def average_team_water_movement(
    rows: list[HandicapRow],
    match: Match,
    team: str,
) -> tuple[float, float, float]:
    team_is_home = team == match.home
    init_values: list[float] = []
    now_values: list[float] = []
    for row in rows:
        now = row.sec_a if team_is_home else row.sec_b
        init = row.init_sec_a if team_is_home else row.init_sec_b
        if now > 0 and init > 0:
            now_values.append(now)
            init_values.append(init)
    if not now_values:
        return 0.0, 0.0, 0.0
    init_avg = sum(init_values) / len(init_values)
    now_avg = sum(now_values) / len(now_values)
    return init_avg, now_avg, now_avg - init_avg


def team_line_delta(row: HandicapRow, match: Match, team: str) -> float:
    """Positive means the handicap became deeper/harder for this team."""
    if not handicap_line_pair_known(row):
        return 0.0
    line_delta = row.latest_line - row.init_line
    return -line_delta if team == match.home else line_delta


def average_team_line_delta(rows: list[HandicapRow], match: Match, team: str) -> float:
    values = [
        team_line_delta(row, match, team)
        for row in rows
        if handicap_line_pair_known(row)
    ]
    if not values:
        return 0.0
    return sum(values) / len(values)


def score_dispersion(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = sum(values) / len(values)
    variance = sum((value - avg) ** 2 for value in values) / len(values)
    return math.sqrt(variance)


def median_float(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def asian_no_vig_probability(team_price: float, opponent_price: float) -> float | None:
    """Convert two-way Asian prices to a margin-free probability."""
    team_decimal = normalize_asian_decimal_odds(team_price)
    opponent_decimal = normalize_asian_decimal_odds(opponent_price)
    if team_decimal is None or opponent_decimal is None:
        return None
    if team_decimal <= 1.0 or opponent_decimal <= 1.0:
        return None
    team_inverse = 1.0 / team_decimal
    opponent_inverse = 1.0 / opponent_decimal
    total = team_inverse + opponent_inverse
    if total <= 0:
        return None
    return clamp(team_inverse / total, 0.02, 0.98)


def equivalent_fair_handicap_depth(
    home_line: float,
    team_is_home: bool,
    team_price: float,
    opponent_price: float,
) -> float | None:
    """Express line and water on one axis: positive means a deeper team handicap."""
    probability = asian_no_vig_probability(team_price, opponent_price)
    if probability is None:
        return None
    quoted_depth = -home_line if team_is_home else home_line
    probability = clamp(probability, 0.08, 0.92)
    probability_shift = 0.55 * math.log(probability / (1.0 - probability))
    return quoted_depth + probability_shift


def handicap_row_equivalent_fair_move(match: Match, row: HandicapRow, team: str) -> float | None:
    if not handicap_line_pair_known(row):
        return None
    team_is_home = team == match.home
    initial_team_price = row.init_sec_a if team_is_home else row.init_sec_b
    initial_opponent_price = row.init_sec_b if team_is_home else row.init_sec_a
    latest_team_price = row.sec_a if team_is_home else row.sec_b
    latest_opponent_price = row.sec_b if team_is_home else row.sec_a
    initial_depth = equivalent_fair_handicap_depth(
        row.init_line,
        team_is_home,
        initial_team_price,
        initial_opponent_price,
    )
    latest_depth = equivalent_fair_handicap_depth(
        row.latest_line,
        team_is_home,
        latest_team_price,
        latest_opponent_price,
    )
    if initial_depth is None or latest_depth is None:
        return None
    return latest_depth - initial_depth


def handicap_row_current_equivalent_depth(match: Match, row: HandicapRow, team: str) -> float | None:
    if not handicap_latest_line_known(row):
        return None
    team_is_home = team == match.home
    team_price = row.sec_a if team_is_home else row.sec_b
    opponent_price = row.sec_b if team_is_home else row.sec_a
    return equivalent_fair_handicap_depth(
        row.latest_line,
        team_is_home,
        team_price,
        opponent_price,
    )


def handicap_market_axis(
    match: Match,
    rows: list[HandicapRow],
    team: str,
) -> tuple[float, float, float, list[float]]:
    """Return direction score, confidence, median fair-line move, and row moves."""
    moves = [
        move
        for row in rows
        if (move := handicap_row_equivalent_fair_move(match, row, team)) is not None
    ]
    if not moves:
        return 0.0, 0.0, 0.0, []
    median_move = median_float(moves)
    positive = sum(1 for move in moves if move > 0.025)
    negative = sum(1 for move in moves if move < -0.025)
    aligned = max(positive, negative)
    coherence = aligned / len(moves)
    median_abs_deviation = median_float([abs(move - median_move) for move in moves])
    dispersion_confidence = clamp(1.0 - median_abs_deviation / 0.22, 0.25, 1.0)
    sample_confidence = clamp(len(moves) / 5.0, 0.45, 1.0)
    confidence = clamp(
        (0.35 + 0.65 * coherence) * dispersion_confidence * sample_confidence,
        0.12,
        1.0,
    )
    direction = clamp(median_move / 0.30, -1, 1)
    return clamp(direction * confidence, -1, 1), confidence, median_move, moves


def snapshot_equivalent_fair_depth(metrics: dict[str, float]) -> float | None:
    upper_water = metrics.get("upper_water", 0.0)
    lower_water = metrics.get("lower_water", 0.0)
    probability = asian_no_vig_probability(upper_water, lower_water)
    if probability is None:
        return None
    probability = clamp(probability, 0.08, 0.92)
    return metrics.get("line_depth", 0.0) + 0.55 * math.log(probability / (1.0 - probability))


def snapshot_minutes_before_kickoff(record: dict[str, Any]) -> float | None:
    stored = optional_float(record.get("minutes_before_kickoff"))
    if stored is not None:
        return stored
    fetched_at = snapshot_record_time(record)
    match_info = record.get("match")
    if fetched_at is None or not isinstance(match_info, dict):
        return None
    match_time = parse_datetime_or_none(match_info.get("match_time"))
    if match_time is None:
        return None
    return (match_time - fetched_at).total_seconds() / 60.0


def current_minutes_before_kickoff(match: Match) -> float:
    stored = optional_float(match.raw.get("_snapshot_minutes_before_kickoff"))
    if stored is not None:
        return stored
    replay_fetched_at = parse_datetime_or_none(match.raw.get("_snapshot_fetched_at"))
    if replay_fetched_at is not None:
        return (match.match_time - replay_fetched_at).total_seconds() / 60.0
    return (match.match_time - datetime.now(timezone.utc)).total_seconds() / 60.0


def rows_from_match_raw(match: Match, fallback_rows: list[HandicapRow] | None = None) -> list[HandicapRow]:
    row_data = match.raw.get("_okooo_handicap_row_data")
    if isinstance(row_data, list):
        rows = [handicap_row_from_dict(item) for item in row_data if isinstance(item, dict)]
        if rows:
            return rows
    return list(fallback_rows or [])


def current_market_equivalent_depth(
    match: Match,
    rows: list[HandicapRow],
    upper_team: str,
) -> float | None:
    team_is_home = upper_team == match.home
    upper_key = side_key(match, upper_team)
    lower_team = match.away if team_is_home else match.home
    lower_key = side_key(match, lower_team)
    upper_water = first_positive(match.raw.get(f"AsianAvr{upper_key}"))
    lower_water = first_positive(match.raw.get(f"AsianAvr{lower_key}"))
    current_depth = equivalent_fair_handicap_depth(
        line_value(match.asian_line),
        team_is_home,
        upper_water,
        lower_water,
    )
    if current_depth is not None:
        return current_depth

    current_depths = [
        depth
        for row in rows_from_match_raw(match, rows)
        if (depth := handicap_row_current_equivalent_depth(match, row, upper_team)) is not None
    ]
    return median_float(current_depths) if current_depths else None


def snapshot_equivalent_fair_depth_for_team(record: dict[str, Any], upper_team: str) -> float | None:
    match_info = record.get("match")
    if not isinstance(match_info, dict):
        return None
    try:
        match = match_from_dict(match_info)
    except (TypeError, ValueError):
        return snapshot_equivalent_fair_depth(snapshot_metrics(record))
    if upper_team not in (match.home, match.away):
        return snapshot_equivalent_fair_depth(snapshot_metrics(record))
    return current_market_equivalent_depth(match, rows_from_match_raw(match), upper_team)


def company_extreme_reversal_confirmation(
    match: Match,
    rows: list[HandicapRow],
    upper_team: str,
    extreme_record: dict[str, Any] | None,
    direction: float,
) -> tuple[float, int, int, int, str]:
    if extreme_record is None or abs(direction) < 1e-9:
        return 0.0, 0, 0, 0, "缺少可比较公司极值点"
    match_info = extreme_record.get("match")
    if not isinstance(match_info, dict):
        return 0.0, 0, 0, 0, "缺少可比较公司极值点"

    extreme_match = match_from_dict(match_info)
    if upper_team not in (extreme_match.home, extreme_match.away):
        return 0.0, 0, 0, 0, "极值点上下盘映射不一致"

    current_by_name = {row.name: row for row in rows_from_match_raw(match, rows) if row.name}
    extreme_by_name = {row.name: row for row in rows_from_match_raw(extreme_match) if row.name}
    aligned = 0
    opposite = 0
    neutral = 0
    for name, current_row in current_by_name.items():
        extreme_row = extreme_by_name.get(name)
        if extreme_row is None:
            continue
        current_depth = handicap_row_current_equivalent_depth(match, current_row, upper_team)
        extreme_depth = handicap_row_current_equivalent_depth(extreme_match, extreme_row, upper_team)
        if current_depth is None or extreme_depth is None:
            continue
        delta = current_depth - extreme_depth
        if delta * direction > 0.055:
            aligned += 1
        elif delta * direction < -0.055:
            opposite += 1
        else:
            neutral += 1

    total = aligned + opposite + neutral
    if total <= 0:
        return 0.0, 0, 0, 0, "公司极值回撤缺少共同样本"
    share = aligned / total
    label = "回撤" if direction < 0 else "反弹"
    strong_note = "，公司强确认" if aligned >= 3 and share >= 0.65 else ""
    reason = f"公司极值后{label}同向 {aligned}/{total}（反向{opposite}/中性{neutral}）{strong_note}"
    return share, aligned, opposite, neutral, reason


def recent_handicap_path_axis(
    match: Match,
    rows: list[HandicapRow],
    upper_team: str,
    snapshot_context: SnapshotContext | None,
) -> tuple[float, float, float, str]:
    """Recent market direction, emphasizing the last four hours over the opening line."""
    if snapshot_context is None or not snapshot_context.records:
        return 0.0, 0.0, 0.0, "缺少临场快照路径"

    current_depth = current_market_equivalent_depth(match, rows, upper_team)
    if current_depth is None:
        return 0.0, 0.0, 0.0, "当前等价公平盘口不可用"

    current_minutes = current_minutes_before_kickoff(match)
    history: list[tuple[float | None, float, dict[str, Any]]] = []
    for record in snapshot_context.records:
        depth = snapshot_equivalent_fair_depth_for_team(record, upper_team)
        if depth is None:
            continue
        history.append((snapshot_minutes_before_kickoff(record), depth, record))
    if not history:
        return 0.0, 0.0, 0.0, "历史等价公平盘口不可用"

    timed_recent = [
        (minutes, depth, record)
        for minutes, depth, record in history
        if minutes is not None and current_minutes - 5.0 <= minutes <= current_minutes + 240.0
    ]
    recent = timed_recent if len(timed_recent) >= 2 else history[-3:]
    recent_depths = [depth for _minutes, depth, _record in recent]
    median_reference = median_float(recent_depths)
    first_reference = recent_depths[0]
    last_reference = recent_depths[-1]
    median_move = current_depth - median_reference
    window_move = current_depth - first_reference
    latest_move = current_depth - last_reference
    path_move = 0.45 * median_move + 0.25 * window_move + 0.30 * latest_move

    known_minutes = [minutes for minutes, _depth, _record in recent if minutes is not None]
    span = max(known_minutes) - min(known_minutes) if len(known_minutes) >= 2 else 60.0
    sample_confidence = clamp(len(recent_depths) / 3.0, 0.45, 1.0)
    span_confidence = clamp(span / 120.0, 0.45, 1.0)
    confidence = clamp(sample_confidence * span_confidence, 0.30, 1.0)

    peak_index, peak_depth = max(enumerate(recent_depths), key=lambda item: item[1])
    trough_index, trough_depth = min(enumerate(recent_depths), key=lambda item: item[1])
    peak_retreat = current_depth - peak_depth
    trough_rally = current_depth - trough_depth
    extreme_move = peak_retreat if abs(peak_retreat) >= abs(trough_rally) else trough_rally
    extreme_index = peak_index if extreme_move == peak_retreat else trough_index
    extreme_label = "高点回撤" if extreme_move < 0 else "低点反弹"
    extreme_minutes, extreme_depth, extreme_record = recent[extreme_index]
    extreme_weight = 0.0
    company_reason = ""
    if abs(extreme_move) >= 0.10:
        extreme_weight = clamp((abs(extreme_move) - 0.08) / 0.16, 0.35, 0.75)
        share, aligned, _opposite, _neutral, company_reason = company_extreme_reversal_confirmation(
            match,
            rows,
            upper_team,
            extreme_record,
            math.copysign(1.0, extreme_move),
        )
        if aligned >= 3:
            if share >= 0.65:
                extreme_weight = max(extreme_weight, 0.70)
            elif share <= 0.45:
                extreme_weight *= 0.55

    combined_move = (1.0 - extreme_weight) * path_move + extreme_weight * extreme_move
    score = clamp(combined_move / 0.22, -1, 1) * confidence
    reason = (
        f"近{int(round(span))}分钟公平盘口参考中位 {median_reference:+.3f}、"
        f"首点 {first_reference:+.3f}、上一点 {last_reference:+.3f}、当前 {current_depth:+.3f}，"
        f"常规加权变化 {path_move:+.3f}"
    )
    if extreme_weight > 0:
        minutes_text = f"T-{int(round(extreme_minutes))}" if extreme_minutes is not None else "历史"
        reason += (
            f"；{extreme_label} {minutes_text} {extreme_depth:+.3f}->当前 {current_depth:+.3f} "
            f"({extreme_move:+.3f})，极值权重 {extreme_weight:.2f}"
        )
        if company_reason:
            reason += f"，{company_reason}"
        reason += f"；合成变化 {combined_move:+.3f}，临场方向 {score:+.2f}"
    else:
        reason += f"，合成变化 {combined_move:+.3f}，临场方向 {score:+.2f}"
    return clamp(score, -1, 1), confidence, combined_move, reason


def handicap_path_persistence(snapshot_context: SnapshotContext | None) -> tuple[float, str]:
    if snapshot_context is None or not snapshot_context.available:
        return 0.85, "缺少连续快照，路径可信度按 0.85"
    depths = [
        depth
        for record in snapshot_context.records
        if (depth := snapshot_equivalent_fair_depth(snapshot_metrics(record))) is not None
    ]
    if len(depths) < 2:
        return 0.85, "连续快照水位不足，路径可信度按 0.85"
    deltas = [right - left for left, right in zip(depths, depths[1:])]
    total_move = depths[-1] - depths[0]
    if abs(total_move) < 0.025:
        return 0.72, f"等价公平盘口路径横盘 {total_move:+.3f}"
    direction = math.copysign(1.0, total_move)
    aligned = sum(1 for delta in deltas if delta * direction > 0.012)
    reversed_steps = sum(1 for delta in deltas if delta * direction < -0.012)
    persistence = aligned / max(len(deltas), 1)
    multiplier = clamp(0.68 + 0.42 * persistence - 0.18 * reversed_steps, 0.55, 1.10)
    return multiplier, (
        f"等价公平盘口路径 {depths[0]:+.3f}->{depths[-1]:+.3f}，"
        f"同向 {aligned}/{len(deltas)}，可信度 {multiplier:.2f}"
    )


def snapshot_equivalent_fair_move(snapshot_context: SnapshotContext | None) -> float | None:
    if snapshot_context is None or not snapshot_context.available:
        return None
    first_depth = snapshot_equivalent_fair_depth(snapshot_context.first_metrics)
    last_depth = snapshot_equivalent_fair_depth(snapshot_context.last_metrics)
    if first_depth is None or last_depth is None:
        return None
    return last_depth - first_depth


def percentile_nearest(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round(clamp(q, 0.0, 1.0) * (len(ordered) - 1)))
    return ordered[index]


def _volume_log_volume_vs_time_order_trend(points: list[PriceVolumePoint]) -> tuple[float, str, float, float]:
    """无稳定时间间隔时：用 ``log1p(成交量)`` 对归一化次序 ``0→1`` 做 OLS 斜率，刻画「量随走势推进」的速率。

    返回 ``(trend_clamped, summary_fragment, early_sum, late_sum)``；后两者为按点数切半的合计，仅作文案参考。
    """
    n = len(points)
    if n < 2:
        return 0.0, "", 0.0, 0.0
    xs = [i / max(n - 1, 1) for i in range(n)]
    ys = [math.log1p(max(p.volume, 0.0)) for p in points]
    mx = sum(xs) / n
    my = sum(ys) / n
    var_x = sum((x - mx) ** 2 for x in xs)
    if var_x <= 1e-15:
        return 0.0, "时序方差过小", 0.0, 0.0
    cov_xy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    beta = cov_xy / var_x
    trend = clamp(beta / 2.2, -1, 1)
    mid = max(1, n // 2)
    early_flow = sum(p.volume for p in points[:mid])
    late_flow = sum(p.volume for p in points[mid:])
    frag = (
        f"log(1+量)~走势次序回归斜率 β={beta:+.3f}（x:首点→末点）；"
        f"前/后段量合计(参考) {early_flow:,.0f} / {late_flow:,.0f}"
    )
    return trend, frag, early_flow, late_flow


def _volume_flow_trend_half_series(points: list[PriceVolumePoint]) -> tuple[float, str, float, float]:
    """成交量时间结构：优先「相邻时点成交速率」曲线 + log(速率)回归；否则 ``log1p(量)`` 对走势次序回归。

    返回 ``(trend_in_minus1_1, summary_fragment, early_sum, late_sum)``。
    有时间戳且相邻间隔有效时，用区间中点时间轴上 log(量/小时) 的斜率，并与前 40%/后 40% 时间窗内平均速率对比；
    否则不用粗糙的「按点数对半」，改用 ``_volume_log_volume_vs_time_order_trend``。
    """
    n = len(points)
    if n < 2:
        return 0.0, "", 0.0, 0.0
    timed = [p for p in points if p.update_time is not None]
    if len(timed) >= 3:
        intervals: list[dict[str, float]] = []
        for prev, curr in zip(timed, timed[1:]):
            prev_time = prev.update_time
            curr_time = curr.update_time
            if prev_time is None or curr_time is None:
                continue
            seconds = (curr_time - prev_time).total_seconds()
            if seconds <= 0:
                continue
            hours = max(seconds / 3600.0, 1 / 60)
            volume = max(curr.volume, 0.0)
            midpoint = prev_time + timedelta(seconds=seconds / 2.0)
            intervals.append(
                {
                    "mid_ts": midpoint.timestamp(),
                    "hours": hours,
                    "volume": volume,
                    "rate": volume / hours,
                }
            )
        if len(intervals) >= 2:
            raw_rates = [item["rate"] for item in intervals]
            smooth_rates: list[float] = []
            for index, _rate in enumerate(raw_rates):
                lo = max(0, index - 1)
                hi = min(len(raw_rates), index + 2)
                smooth_rates.append(median_float(raw_rates[lo:hi]))
            for item, smooth_rate in zip(intervals, smooth_rates):
                item["smooth_rate"] = smooth_rate
            start_ts = intervals[0]["mid_ts"]
            end_ts = intervals[-1]["mid_ts"]
            span_ts = end_ts - start_ts
            xs = [0.0 if span_ts <= 0 else (item["mid_ts"] - start_ts) / span_ts for item in intervals]
            ys = [math.log1p(item["smooth_rate"]) for item in intervals]
            x_avg = sum(xs) / len(xs)
            y_avg = sum(ys) / len(ys)
            variance_x = sum((x - x_avg) ** 2 for x in xs)
            slope = 0.0 if variance_x <= 0 else sum((x - x_avg) * (y - y_avg) for x, y in zip(xs, ys)) / variance_x
            slope_score = clamp(slope / 3.2, -1, 1)

            early_items = [item for item, x in zip(intervals, xs) if x <= 0.40]
            late_items = [item for item, x in zip(intervals, xs) if x >= 0.60]
            if not early_items or not late_items:
                mid = max(1, len(intervals) // 2)
                early_items = intervals[:mid]
                late_items = intervals[mid:]
            early_flow = sum(item["volume"] for item in early_items)
            late_flow = sum(item["volume"] for item in late_items)
            early_hours = sum(item["hours"] for item in early_items)
            late_hours = sum(item["hours"] for item in late_items)
            early_rate = sum(item["smooth_rate"] * item["hours"] for item in early_items) / max(early_hours, 1 / 60)
            late_rate = sum(item["smooth_rate"] * item["hours"] for item in late_items) / max(late_hours, 1 / 60)
            denom = early_rate + late_rate
            if denom <= 0:
                return 0.0, "各时点成交量接近零", early_flow, late_flow
            rate_contrast = clamp((late_rate - early_rate) / denom, -1, 1)
            trend = clamp(0.62 * rate_contrast + 0.38 * slope_score, -1, 1)
            peak_rate = percentile_nearest(smooth_rates, 0.90)
            frag = (
                f"成交速率：前40%时间窗均速 {early_rate:,.0f}/h vs 后40%时间窗 {late_rate:,.0f}/h；"
                f"log(平滑速率)~归一化时点回归斜率 {slope_score:+.2f}；P90峰值 {peak_rate:,.0f}/h"
            )
            return trend, frag, early_flow, late_flow

    return _volume_log_volume_vs_time_order_trend(points)


def score_price_volume(points: list[PriceVolumePoint]) -> tuple[float | None, str, dict[str, Any] | None]:
    """单 selection（主/客）必发价量序列 → [-1,1] 分数与说明。

    第三元 ``meta`` 供 ``_trade_signal`` 在「仅时间趋势、无买/卖」时做跨边对比；点不足时 ``meta`` 为 ``None``。
    """
    filtered = [point for point in points if point.price > 0]
    if len(filtered) < 2:
        return None, "走势点不足", None
    usable = sorted(
        filtered,
        key=lambda p: p.update_time or datetime.min.replace(tzinfo=timezone.utc),
    )
    meta: dict[str, Any] = {"n": len(usable)}
    buy_volume = sum(point.volume for point in usable if point.attr and "买" in point.attr)
    sell_volume = sum(point.volume for point in usable if point.attr and "卖" in point.attr)
    labeled = buy_volume + sell_volume
    total_flow = sum(point.volume for point in usable)
    first_price = usable[0].price
    last_price = usable[-1].price
    price_score = clamp((first_price - last_price) / max(first_price * 0.08, 0.08), -1, 1)
    raw_trend: float | None = None

    if labeled > 0:
        branch = "labeled"
        volume_score = (buy_volume - sell_volume) / labeled
        reason = (
            f"买量 {buy_volume:,.0f} / 卖量 {sell_volume:,.0f}，"
            f"价格 {first_price:.2f}->{last_price:.2f}"
        )
    elif total_flow > 0:
        branch = "trend"
        raw_trend, trend_frag, _e, _l = _volume_flow_trend_half_series(usable)
        volume_score = raw_trend
        n = len(usable)
        scale = min(1.0, max(0.38, (n - 2) / 5.5))
        volume_score *= scale
        concord = (raw_trend or 0.0) * price_score
        if concord > 0.02:
            bump = 0.12 * min(abs(raw_trend or 0.0), abs(price_score)) * scale
            volume_score = clamp(volume_score + math.copysign(bump, raw_trend or 0.0), -1, 1)
        elif concord < -0.04 and abs(raw_trend or 0.0) > 0.12 and abs(price_score) > 0.08:
            volume_score *= 0.82
        reason = (
            f"成交量时间趋势：{trend_frag}（明细未标注买/卖），"
            f"价格 {first_price:.2f}->{last_price:.2f}"
        )
    else:
        branch = "flat"
        volume_score = 0.0
        reason = (
            f"买量 {buy_volume:,.0f} / 卖量 {sell_volume:,.0f}，"
            f"价格 {first_price:.2f}->{last_price:.2f}"
        )

    score = clamp(0.65 * volume_score + 0.35 * price_score, -1, 1)
    meta["branch"] = branch
    meta["labeled"] = labeled > 0
    meta["raw_trend"] = raw_trend
    meta["volume_score"] = volume_score
    meta["price_score"] = price_score
    meta["total_flow"] = total_flow
    return score, reason, meta


def score_bifa_odds_confirmation(upper_odds: float, lower_odds: float) -> float:
    if upper_odds <= 0 or lower_odds <= 0:
        return 0.0
    upper_probability = 1.0 / upper_odds
    lower_probability = 1.0 / lower_odds
    total = upper_probability + lower_probability
    if total <= 0:
        return 0.0
    return clamp((upper_probability - lower_probability) / total, -1, 1)


def score_hot_divergence_penalty(
    heat_edge: float,
    confirmation_edge: float,
    payout_edge: float,
    line_depth: float = 0.75,
) -> float:
    """Penalize public heat that is not confirmed by price or payout pressure."""
    heat_size = abs(heat_edge)
    if heat_size < 0.22:
        return 0.0
    same_direction_confirmation = math.copysign(1, heat_edge) * confirmation_edge
    same_direction_payout = math.copysign(1, heat_edge) * payout_edge
    if same_direction_confirmation >= 0.35 and same_direction_payout >= -0.20:
        return 0.0
    if same_direction_confirmation >= 0.12 and same_direction_payout >= -0.05:
        return 0.0
    divergence = max(0.0, 0.12 - same_direction_confirmation)
    payout_warning = max(0.0, -same_direction_payout)
    base = 0.20 + 0.35 * heat_size + 0.25 * divergence + 0.20 * payout_warning
    if line_depth <= 0.5:
        multiplier = 0.72
        cap = 0.38
    elif line_depth <= 1.25:
        multiplier = 1.0
        cap = 0.55
    else:
        multiplier = 1.18 if line_depth < 2.0 else 1.30
        cap = 0.70
    return clamp(base * multiplier, 0, cap)


def fallback_handicap_rows_from_base(match: Match) -> list[HandicapRow]:
    try:
        home = float(match.raw.get("AsianAvrHome", 0.0))
        away = float(match.raw.get("AsianAvrAway", 0.0))
    except (TypeError, ValueError):
        return []
    if home <= 0 or away <= 0:
        return []
    return [
        HandicapRow(
            bookmaker_id=0,
            name="SPDEX平均",
            sec_a=home,
            sec_b=away,
            init_sec_a=home,
            init_sec_b=away,
            payout=0.97,
            update_time=parse_datetime_or_none(match.raw.get("UpdateTime")),
            source="fallback",
            init_line=line_value(match.asian_line),
            latest_line=line_value(match.asian_line),
            init_line_known=True,
            latest_line_known=True,
        )
    ]


def handicap_rows_near_match_line(rows: list[HandicapRow], asian_line: str) -> list[HandicapRow]:
    target = line_value(asian_line)
    if abs(target) < 1e-9:
        return rows
    line_rows = [row for row in rows if handicap_latest_line_known(row)]
    if not line_rows:
        return rows

    near = [row for row in line_rows if abs(row.latest_line - target) <= 0.26]
    if near:
        return near

    closest_distance = min(abs(row.latest_line - target) for row in line_rows)
    closest = [row for row in line_rows if abs(abs(row.latest_line - target) - closest_distance) < 1e-9]
    return closest or rows


def fallback_euro_kelly_signal(match: Match, upper_team: str, lower_team: str) -> Signal:
    upper_key = side_key(match, upper_team)
    lower_key = side_key(match, lower_team)
    if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
        return unavailable_signal("欧赔/Kelly", WEIGHTS["euro_kelly"], "无法映射欧赔/Kelly静态方向")
    try:
        upper_price = float(match.raw.get(f"EuroAvr{upper_key}", 0.0))
        lower_price = float(match.raw.get(f"EuroAvr{lower_key}", 0.0))
        upper_kelly = float(match.raw.get(f"Kelly{upper_key}", 0.0))
        lower_kelly = float(match.raw.get(f"Kelly{lower_key}", 0.0))
    except (TypeError, ValueError):
        return unavailable_signal("欧赔/Kelly", WEIGHTS["euro_kelly"], "欧赔/Kelly静态字段解析失败")
    if upper_price <= 0 or lower_price <= 0 or upper_kelly <= 0 or lower_kelly <= 0:
        return unavailable_signal("欧赔/Kelly", WEIGHTS["euro_kelly"], "欧赔/Kelly走势点不足")
    price_edge = score_bifa_odds_confirmation(upper_price, lower_price)
    kelly_edge = clamp((lower_kelly - upper_kelly) / max(abs(lower_kelly) + abs(upper_kelly), 1.0), -1, 1)
    score = clamp(0.55 * price_edge + 0.45 * kelly_edge, -1, 1)
    return Signal(
        "欧赔/Kelly",
        score,
        WEIGHTS["euro_kelly"],
        True,
        (
            f"静态均赔 {upper_team} {upper_price:.2f} vs {lower_team} {lower_price:.2f}，"
            f"Kelly {upper_kelly:.2f} vs {lower_kelly:.2f}"
        ),
    )


def score_bifa_heat_edge(match: Match, upper_team: str, lower_team: str) -> float:
    index_edge, amount_edge = bifa_index_amount_edges(match, upper_team, lower_team)
    if index_edge * amount_edge < 0 and abs(index_edge) >= 0.04 and abs(amount_edge) >= 0.18:
        return clamp(0.35 * index_edge + 0.25 * amount_edge, -0.16, 0.16)
    return clamp(0.55 * index_edge + 0.45 * amount_edge, -1, 1)


def bifa_index_amount_edges(match: Match, upper_team: str, lower_team: str) -> tuple[float, float]:
    upper_key = side_key(match, upper_team)
    lower_key = side_key(match, lower_team)
    if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
        return 0.0, 0.0
    try:
        upper_index = float(match.raw.get(f"BfIndex{upper_key}", 0.0))
        lower_index = float(match.raw.get(f"BfIndex{lower_key}", 0.0))
        draw_index = float(match.raw.get("BfIndexDraw", 0.0))
        upper_amount = float(match.raw.get(f"BfAmount{upper_key}", 0.0))
        lower_amount = float(match.raw.get(f"BfAmount{lower_key}", 0.0))
        draw_amount = float(match.raw.get("BfAmountDraw", 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0
    draw_index_drag = clamp((draw_index - 18.0) / 22.0, 0.0, 0.45)
    index_edge = clamp((upper_index - lower_index) / 100.0 * (1.0 - draw_index_drag), -1, 1)
    amount_total = upper_amount + lower_amount
    all_amount = amount_total + draw_amount
    draw_amount_share = draw_amount / all_amount if all_amount > 0 else 0.0
    draw_amount_drag = clamp((draw_amount_share - 0.16) / 0.26, 0.0, 0.45)
    amount_edge = 0.0 if amount_total <= 0 else clamp((upper_amount - lower_amount) / amount_total * (1.0 - draw_amount_drag), -1, 1)
    return index_edge, amount_edge


def bifa_heat_split_reason(index_edge: float, amount_edge: float, upper_team: str, lower_team: str) -> str:
    if index_edge * amount_edge >= 0:
        return ""
    if abs(index_edge) < 0.04 or abs(amount_edge) < 0.18:
        return ""
    index_team = upper_team if index_edge > 0 else lower_team
    amount_team = upper_team if amount_edge > 0 else lower_team
    return f"必发指数偏{index_team}、成交额偏{amount_team}，热度分裂已降权"


def score_bifa_price_edge(match: Match, upper_team: str, lower_team: str) -> float:
    upper_key = side_key(match, upper_team)
    lower_key = side_key(match, lower_team)
    if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
        return 0.0
    try:
        upper_odds = float(match.raw.get(f"BfOdds{upper_key}", 0.0))
        lower_odds = float(match.raw.get(f"BfOdds{lower_key}", 0.0))
    except (TypeError, ValueError):
        return 0.0
    return score_bifa_odds_confirmation(upper_odds, lower_odds)


def score_bifa_payout_edge(match: Match, upper_team: str, lower_team: str) -> float:
    upper_key = side_key(match, upper_team)
    lower_key = side_key(match, lower_team)
    if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
        return 0.0
    try:
        upper_payout = float(match.raw.get(f"BfPayout{upper_key}", 0.0))
        lower_payout = float(match.raw.get(f"BfPayout{lower_key}", 0.0))
        draw_payout = float(match.raw.get("BfPayoutDraw", 0.0))
    except (TypeError, ValueError):
        return 0.0
    total = abs(upper_payout) + abs(lower_payout) + abs(draw_payout)
    if total <= 0:
        return 0.0
    return clamp((lower_payout - upper_payout) / total * 1.20, -1, 1)


def score_heat_handicap_divergence_penalty(heat_edge: float, handicap_score: float) -> float:
    """Penalize a very hot side if the Asian handicap signal points the other way."""
    if abs(heat_edge) < 0.25:
        return 0.0
    same_direction_handicap = math.copysign(1, heat_edge) * handicap_score
    if same_direction_handicap >= -0.05:
        return 0.0
    return clamp(0.15 + 0.35 * abs(heat_edge) + 0.25 * abs(same_direction_handicap), 0, 0.50)


def upper_favorite_trap_pressure_from_values(
    *,
    match: Match,
    heat_edge: float,
    trade_available: bool,
    trade_edge: float,
    euro_edge: float,
    handicap_edge: float,
    fair_edge: float,
    depth_edge: float,
    cover_edge: float,
    draw_edge: float,
    bifa_reason: str,
) -> float:
    """Pressure against an over-heated upper side when the confirmation chain is weak.

    This is intentionally not a generic "risk means buy lower" rule.  It only fires
    when the public/Betfair side is clearly hot and the dynamic confirmation chain
    described in the README is missing or actively soft.
    """
    if heat_edge < 0.52:
        return 0.0
    depth = line_depth(match.asian_line)
    trade_weak = (not trade_available) or trade_edge < 0.05
    if not trade_weak:
        return 0.0

    shallow_hot_note = "浅盘大热" in (bifa_reason or "")
    pressure = 0.0
    weak_confirmations = 0
    if euro_edge <= 0.08:
        weak_confirmations += 1
        pressure += 0.04 + clamp(-euro_edge, 0, 0.30) * 0.20
    if handicap_edge <= 0.08:
        weak_confirmations += 1
        pressure += 0.04 + clamp(-handicap_edge, 0, 0.35) * 0.18
    if fair_edge <= 0.02:
        weak_confirmations += 1
        pressure += 0.03 + clamp(-fair_edge, 0, 0.50) * 0.14
    if depth_edge <= 0.12 and depth >= 0.75:
        weak_confirmations += 1
        pressure += 0.03
    if cover_edge <= -0.25 and depth >= 0.75:
        weak_confirmations += 1
        pressure += 0.05 + clamp(abs(cover_edge) - 0.25, 0, 0.35) * 0.14
    if draw_edge <= -0.08 and depth <= 1.25:
        pressure += 0.03

    heat_bonus = clamp((heat_edge - 0.52) / 0.36, 0, 1) * 0.09
    if depth <= 0.5:
        if not shallow_hot_note or weak_confirmations < 2:
            return 0.0
        pressure += 0.11 + heat_bonus
        return clamp(pressure, 0.0, 0.34)
    if depth <= 1.25:
        if weak_confirmations < 3:
            return 0.0
        pressure += 0.08 + heat_bonus
        return clamp(pressure, 0.0, 0.30)
    if euro_edge > 0.18:
        return 0.0
    if weak_confirmations < 3 or (euro_edge > 0.08 and fair_edge > -0.08):
        return 0.0
    pressure += 0.07 + heat_bonus
    return clamp(pressure, 0.0, 0.28)


def model_edge_for_water_value(signals: list[Signal]) -> float:
    weights = {
        "必发指数": 0.22,
        "必发成交走势": 0.12,
        "欧赔/Kelly": 0.15,
        "盘口合理性": 0.14,
        "盘口深度/打穿能力": 0.10,
        # 高低水内部的模型边已与主模型「快照趋势」叠加，略降权避免同维重复
        "快照趋势": 0.08,
        "资金/盘口弹性": 0.12,
        "外部赔率/实力校验": 0.07,
    }
    total = 0.0
    weighted = 0.0
    for signal in signals:
        if not signal.available:
            continue
        weight = weights.get(signal.name)
        if weight is None:
            continue
        total += weight
        weighted += weight * signal.score
    if total <= 0:
        return 0.0
    return clamp(weighted / total, -1, 1)


def signal_direction(signal: Signal, threshold: float = 0.08) -> int:
    if not signal.available:
        return 0
    if signal.score > threshold:
        return 1
    if signal.score < -threshold:
        return -1
    return 0


def count_same_direction(signals: list[Signal]) -> int:
    directions = [signal_direction(signal) for signal in signals]
    positives = sum(1 for direction in directions if direction > 0)
    negatives = sum(1 for direction in directions if direction < 0)
    if positives >= negatives:
        return positives
    return -negatives


def signal_conflict_count(signals: list[Signal]) -> int:
    directions = [signal_direction(signal) for signal in signals]
    positives = sum(1 for direction in directions if direction > 0)
    negatives = sum(1 for direction in directions if direction < 0)
    return min(positives, negatives) * 2


def asian_lower_confirmation_cluster(lookup: dict[str, Signal]) -> tuple[int, list[str]]:
    """Count lower-side confirmations that come from the handicap market itself."""
    count = 0
    reasons: list[str] = []
    handicap = lookup.get("亚盘水位")
    bookmaker = lookup.get("公司一致性")
    fair_line = lookup.get("盘口合理性")
    elasticity = lookup.get("资金/盘口弹性")
    cover_risk = lookup.get("赢盘门槛风险")
    fallback_handicap = bool(handicap and "静态亚盘均值兜底" in handicap.reason)

    if signal_value(handicap) <= -0.10 and not fallback_handicap:
        count += 1
        reasons.append("亚盘水位偏下盘")
    if signal_value(bookmaker) <= -0.12:
        count += 1
        reasons.append("公司一致性偏下盘")
    if signal_value(fair_line) <= -0.10:
        count += 1
        reasons.append("盘口合理性偏下盘")
    if signal_value(elasticity) <= -0.12:
        count += 1
        reasons.append("资金/盘口弹性偏下盘")
    if signal_value(cover_risk) <= -0.35 and not fallback_handicap and (
        signal_value(handicap) <= 0.02 or signal_value(bookmaker) <= 0.02
    ):
        count += 1
        reasons.append("赢盘门槛风险未获亚盘确认")
    return count, reasons


def asian_upper_confirmation_cluster(lookup: dict[str, Signal]) -> tuple[int, list[str]]:
    count = 0
    reasons: list[str] = []
    if signal_value(lookup.get("亚盘水位")) >= 0.18:
        count += 1
        reasons.append("亚盘水位偏上盘")
    if signal_value(lookup.get("公司一致性")) >= 0.18:
        count += 1
        reasons.append("公司一致性偏上盘")
    if signal_value(lookup.get("盘口合理性")) >= 0.08:
        count += 1
        reasons.append("盘口合理性偏上盘")
    if signal_value(lookup.get("资金/盘口弹性")) >= 0.14:
        count += 1
        reasons.append("资金/盘口弹性偏上盘")
    return count, reasons


def handicap_upper_water_rise_mitigated_by_euro(match: Match, upper_team: str) -> bool:
    """欧赔/凯利明显支撑让球方(上盘)时，上盘临场升水多为正常受热，减轻扣分。"""
    uk = side_key(match, upper_team)
    if uk not in ("Home", "Away"):
        return False
    lk = "Away" if uk == "Home" else "Home"
    eu_u = float_or_zero(match.raw.get(f"EuroAvr{uk}"))
    eu_l = float_or_zero(match.raw.get(f"EuroAvr{lk}"))
    if min(eu_u, eu_l) <= 0:
        return False
    if eu_u > eu_l - 0.12:
        return False
    ke_u = float_or_zero(match.raw.get(f"Kelly{uk}"))
    ke_l = float_or_zero(match.raw.get(f"Kelly{lk}"))
    if min(ke_u, ke_l) <= 0:
        return eu_u + 0.4 < eu_l
    return ke_u >= ke_l - 0.03


def score_handicap_row(match: Match, row: HandicapRow, upper_team: str) -> float:
    fair_move = handicap_row_equivalent_fair_move(match, row, upper_team)
    if fair_move is None:
        return 0.0
    return clamp(fair_move / 0.30, -1, 1)


def handicap_line_move_edge(match: Match, row: HandicapRow, upper_team: str) -> float:
    if not handicap_line_pair_known(row):
        return 0.0
    line_delta = row.latest_line - row.init_line
    upper_line_delta = -line_delta if upper_team == match.home else line_delta
    return clamp(upper_line_delta / 0.50, -1, 1)


def bookmaker_priority(row: HandicapRow) -> tuple[int, str]:
    if row.priority_hint is not None:
        return (row.priority_hint, row.name)
    try:
        return (TOP_BOOKMAKERS.index(row.name), row.name)
    except ValueError:
        return (len(TOP_BOOKMAKERS), row.name)


def okooo_draw_implied_prob_from_raw(raw: dict[str, Any]) -> float | None:
    """澳客导入的平局概率（0–1）；优先 `_okooo_euro_prob_draw` / `_okooo_probability_draw`。"""
    for key in ("_okooo_euro_prob_draw", "_okooo_probability_draw"):
        v = raw.get(key)
        if v is None:
            continue
        try:
            x = float(v)
        except (TypeError, ValueError):
            continue
        if x > 1.0:
            x /= 100.0
        return clamp(x, 0.0, 1.0)
    return None


def okooo_replay_chain_weak(
    match: Match,
    trade_signal: Signal,
    euro_kelly_signal: Signal,
    snapshot_context: SnapshotContext | None,
) -> bool:
    """澳客数据源且缺必发成交走势、欧赔/Kelly 无有效走势、本地无≥2条快照时，视为 README 所述「多源互证」链断裂。"""
    if match.raw.get("_source") != "okooo":
        return False
    if trade_signal.available:
        return False
    if euro_kelly_signal.available:
        if abs(euro_kelly_signal.score) > 0.08:
            return False
    if snapshot_context is not None and snapshot_context.available:
        return False
    return True


def okooo_static_snapshot_score_adjustment(
    match: Match,
    weighted_score: float,
    trade_signal: Signal,
    euro_kelly_signal: Signal,
    snapshot_context: SnapshotContext | None,
) -> tuple[float, str | None]:
    """单条/静态澳客重放：缺成交与欧赔走势时，对偏正的综合分做保守回撤（README：不可靠则保守，避免仅靠静态大热顶穿阈值）。"""
    if not okooo_replay_chain_weak(match, trade_signal, euro_kelly_signal, snapshot_context):
        return weighted_score, None
    if weighted_score <= 0.08:
        return weighted_score, None
    # 略正：小幅压；越接近/超过上盘阈值，回撤略加大
    penalty = 0.05 + 0.55 * min(max(weighted_score - 0.08, 0.0), 0.25)
    adjusted = weighted_score - penalty
    note = f"澳客静态快照：缺成交走势且欧赔/Kelly无有效走势、本地快照不足，综合分保守回撤 {penalty:.3f}"
    return adjusted, note


def okooo_missing_live_asian_model_cap(
    match: Match,
    weighted_score: float,
    signals: list[Signal],
) -> tuple[float, str | None]:
    if match.raw.get("_source") != "okooo":
        return weighted_score, None
    lookup = {signal.name: signal for signal in signals}
    handicap = lookup.get("亚盘水位")
    bookmaker = lookup.get("公司一致性")
    trade = lookup.get("必发成交走势")
    fallback_or_missing_asian = bool(
        (handicap and "静态亚盘均值兜底" in handicap.reason)
        or bookmaker is None
        or not bookmaker.available
    )
    if not fallback_or_missing_asian:
        return weighted_score, None
    upper_team, lower_team = upper_lower_teams(match)
    heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
    trade_missing = trade is None or not trade.available
    direction_against_heat = abs(heat_edge) >= 0.25 and weighted_score * heat_edge < 0
    if not trade_missing or (abs(weighted_score) >= 0.16 and not direction_against_heat):
        return weighted_score, None
    capped = clamp(weighted_score, LOWER_THRESHOLD + 0.005, UPPER_THRESHOLD - 0.005)
    if abs(capped - weighted_score) < 1e-9:
        return weighted_score, None
    if direction_against_heat:
        return capped, "缺少真实亚盘公司行，反热度模型方向封顶为轻微"
    return capped, "缺少真实亚盘公司行，弱模型方向封顶为轻微"


def deep_favorite_retracement_guard(
    match: Match,
    upper_team: str,
    handicap_signal: Signal,
    market_elasticity_signal: Signal,
    bifa_signal: Signal,
    trade_signal: Signal,
    euro_kelly_signal: Signal,
) -> tuple[Signal, Signal]:
    """Treat deep-favorite late line drops as cover risk unless other markets also turn."""
    if not handicap_signal.available:
        return handicap_signal, market_elasticity_signal
    depth = line_depth(match.asian_line)
    if depth < 1.5:
        return handicap_signal, market_elasticity_signal
    if "高点回撤" not in handicap_signal.reason or "公司强确认" not in handicap_signal.reason:
        return handicap_signal, market_elasticity_signal
    if handicap_signal.score > -0.20:
        return handicap_signal, market_elasticity_signal
    if not euro_kelly_signal.available or euro_kelly_signal.score < 0.28:
        return handicap_signal, market_elasticity_signal
    if trade_signal.available and trade_signal.score < -0.12:
        return handicap_signal, market_elasticity_signal
    if bifa_signal.available and bifa_signal.score < -0.05:
        return handicap_signal, market_elasticity_signal

    upper_key = side_key(match, upper_team)
    upper_water = first_positive(match.raw.get(f"AsianAvr{upper_key}"))
    if upper_water >= 2.08:
        return handicap_signal, market_elasticity_signal

    note = (
        "深盘强队退浅保护：欧赔/Kelly和必发未同步反向，且当前盘口更易打穿；"
        "高点回撤只降上盘强度，不直接翻成下盘"
    )
    adjusted_handicap = handicap_signal
    if handicap_signal.score < -0.12:
        adjusted_handicap = replace(
            handicap_signal,
            score=-0.12,
            reason=f"{handicap_signal.reason}；{note}",
        )
    elif note not in handicap_signal.reason:
        adjusted_handicap = replace(handicap_signal, reason=f"{handicap_signal.reason}；{note}")

    adjusted_elasticity = market_elasticity_signal
    if market_elasticity_signal.available and market_elasticity_signal.score < -0.04:
        adjusted_elasticity = replace(
            market_elasticity_signal,
            score=-0.04,
            reason=f"{market_elasticity_signal.reason}；{note}",
        )
    elif market_elasticity_signal.available and note not in market_elasticity_signal.reason:
        adjusted_elasticity = replace(
            market_elasticity_signal,
            reason=f"{market_elasticity_signal.reason}；{note}",
        )
    return adjusted_handicap, adjusted_elasticity


def deep_favorite_retracement_score_floor(
    match: Match,
    weighted_score: float,
    signals: list[Signal],
) -> tuple[float, str | None]:
    """Avoid turning a still-confirmed deep favorite into a lower-side pick on a line drop alone."""
    if weighted_score >= 0 or weighted_score <= -0.15:
        return weighted_score, None
    if line_depth(match.asian_line) < 1.5:
        return weighted_score, None

    lookup = {signal.name: signal for signal in signals}
    handicap = lookup.get("亚盘水位")
    if (
        handicap is None
        or not handicap.available
        or "深盘强队退浅保护" not in handicap.reason
    ):
        return weighted_score, None

    euro_kelly = lookup.get("欧赔/Kelly")
    trade = lookup.get("必发成交走势")
    bifa = lookup.get("必发指数")
    if euro_kelly is None or not euro_kelly.available or euro_kelly.score < 0.28:
        return weighted_score, None
    if trade is not None and trade.available and trade.score < -0.12:
        return weighted_score, None
    if bifa is not None and bifa.available and bifa.score < -0.05:
        return weighted_score, None

    floored = max(weighted_score, MODEL_DIRECTION_EPSILON)
    if floored <= weighted_score + 1e-9:
        return weighted_score, None
    return (
        floored,
        f"深盘强队退浅保护总分地板：{weighted_score:+.3f}->{floored:+.3f}，退盘仅作赢盘风险不翻下盘",
    )


def source_adjusted_signals(match: Match, signals: list[Signal]) -> list[Signal]:
    if match.raw.get("_source") != "okooo":
        return signals
    adjusted: list[Signal] = []
    for signal in signals:
        override = OKOOO_SCORING_WEIGHT_OVERRIDES.get(signal.name)
        if override is None or abs(signal.weight - override) < 1e-9:
            adjusted.append(signal)
            continue
        reason = signal.reason
        if override < signal.weight and "澳客同源加权降权" not in reason:
            reason += "；仅作热门/赔付压力参数，不直接进入方向加权" if override == 0 else "；澳客同源加权降权"
        elif override > signal.weight and signal.name in ("亚盘水位", "公司一致性", "盘口合理性", "赢盘门槛风险", "资金/盘口弹性"):
            reason += "；澳客亚盘主导权重提升"
        adjusted.append(replace(signal, weight=override, reason=reason))
    return adjusted


def scoring_signals_for_source(match: Match, signals: list[Signal]) -> list[Signal]:
    if match.raw.get("_source") != "okooo":
        return signals
    return [signal for signal in signals if signal.name not in OKOOO_INTERNAL_SIGNAL_NAMES]


def decision_signals_for_source(match: Match, signals: list[Signal]) -> list[Signal]:
    if match.raw.get("_source") != "okooo":
        return signals
    return [signal for signal in signals if signal.name not in OKOOO_PURCHASE_INTERNAL_SIGNAL_NAMES]


def marginal_model_score_lift_for_mid_deep_upper(
    match: Match,
    weighted_score: float,
    available_weight: float,
    euro_kelly_signal: Signal,
    market_balance_signal: Signal,
    draw_risk_signal: Signal,
    trade_signal: Signal,
    snapshot_context: SnapshotContext | None,
) -> tuple[float, str | None]:
    """综合分略低于上盘阈值时，中深盘且欧赔/市场较强支撑上盘、平局风险未极端时小幅上调（例：美国净胜盘）。"""
    if okooo_replay_chain_weak(match, trade_signal, euro_kelly_signal, snapshot_context):
        return weighted_score, None
    if available_weight < 0.62 or not (0.072 <= weighted_score < UPPER_THRESHOLD):
        return weighted_score, None
    depth = line_depth(match.asian_line)
    if not (0.75 <= depth <= 1.25):
        return weighted_score, None
    if not euro_kelly_signal.available or euro_kelly_signal.score < 0.46:
        return weighted_score, None
    if not market_balance_signal.available or market_balance_signal.score < 0.14:
        return weighted_score, None
    if draw_risk_signal.available and draw_risk_signal.score < -0.22:
        return weighted_score, None
    euro_factor = clamp((euro_kelly_signal.score - 0.46) / 0.24, 0.0, 1.0)
    market_factor = clamp((market_balance_signal.score - 0.14) / 0.36, 0.0, 1.0)
    draw_factor = 0.5
    if draw_risk_signal.available:
        draw_factor = clamp((draw_risk_signal.score + 0.22) / 0.30, 0.0, 1.0)
    gap_factor = clamp((UPPER_THRESHOLD - weighted_score) / 0.048, 0.0, 1.0)
    bump = clamp(0.022 + 0.014 * euro_factor + 0.010 * market_factor + 0.008 * draw_factor, 0.020, 0.055)
    bump *= 0.72 + 0.28 * gap_factor
    lifted = min(UPPER_THRESHOLD + 0.003, weighted_score + bump)
    if lifted <= weighted_score + 1e-9:
        return weighted_score, None
    return lifted, f"中深盘边际补强上盘 (+{bump:.3f})"


def marginal_deep_upper_cover_score_lift(
    match: Match,
    weighted_score: float,
    available_weight: float,
    euro_kelly_signal: Signal,
    external_consensus_signal: Signal,
    market_balance_signal: Signal,
    handicap_signal: Signal,
    cover_risk_signal: Signal,
    trade_signal: Signal,
    snapshot_context: SnapshotContext | None,
) -> tuple[float, str | None]:
    """深让（净胜≥1.25）且综合分略偏下盘、欧赔与外部仍明显挺上盘时小幅上调，避免深盘强队被净胜门槛误伤（例：巴西）。

    澳客等数据源仅有一条欧赔/Kelly 时 ``欧赔/Kelly`` 常为中性分（无走势）；此时若盘口≥2 球且外部共识仍明显挺上盘，也允许触发本补强（例：德国 -3）。
    """
    if okooo_replay_chain_weak(match, trade_signal, euro_kelly_signal, snapshot_context):
        return weighted_score, None
    if available_weight < 0.62:
        return weighted_score, None
    depth = line_depth(match.asian_line)
    if depth < 1.25:
        return weighted_score, None
    if not (-0.12 < weighted_score < 0.05):
        return weighted_score, None
    if not euro_kelly_signal.available or not external_consensus_signal.available:
        return weighted_score, None
    ext = external_consensus_signal.score
    es = euro_kelly_signal.score
    euro_track_ok = es >= 0.30
    euro_flat_ok = abs(es) <= 0.04 and depth >= 2.0 and ext >= 0.15
    if not (euro_track_ok or euro_flat_ok):
        return weighted_score, None
    if not euro_flat_ok and ext < 0.06:
        return weighted_score, None
    if match.raw.get("_source") == "okooo":
        blockers: list[str] = []
        if handicap_signal.available and handicap_signal.score <= -0.18:
            blockers.append("亚盘水位反向")
        if market_balance_signal.available and market_balance_signal.score <= -0.12:
            blockers.append("市场平衡反向")
        if cover_risk_signal.available and cover_risk_signal.score <= -0.20:
            blockers.append("赢盘门槛反向")
        if blockers:
            return weighted_score, "深让净胜边际补强跳过（" + "、".join(blockers[:3]) + "）"
    euro_factor = clamp((max(es, 0.0) - 0.30) / 0.30, 0.0, 1.0) if not euro_flat_ok else 0.35
    external_factor = clamp((ext - 0.06) / 0.22, 0.0, 1.0)
    depth_factor = clamp((depth - 1.25) / 1.25, 0.0, 1.0)
    gap_factor = clamp((0.05 - weighted_score) / 0.17, 0.0, 1.0)
    bump = clamp(0.12 + 0.055 * euro_factor + 0.045 * external_factor + 0.030 * depth_factor, 0.10, 0.24)
    bump *= 0.72 + 0.28 * gap_factor
    lifted = min(UPPER_THRESHOLD + 0.02, weighted_score + bump)
    if lifted <= weighted_score + 1e-9:
        return weighted_score, None
    return lifted, f"深让净胜边际补强上盘 (+{bump:.3f})"


def model_upper_trap_score_adjustment(
    match: Match,
    weighted_score: float,
    signals: list[Signal],
) -> tuple[float, str | None]:
    """Apply upper-hot trap pressure to the model score itself when lower evidence is explicit.

    Betfair/public heat is useful, but for Asian handicap it is not equivalent to
    cover value.  The model score should be allowed to turn lower when the hot
    upper side lacks the README confirmation chain and there is a concrete lower
    confirmation cluster; otherwise the risk stays as a lighter model direction.
    """
    if weighted_score <= LOWER_THRESHOLD:
        return weighted_score, None

    lookup = {signal.name: signal for signal in signals}
    bifa = lookup.get("必发指数")
    if bifa is None or not bifa.available:
        return weighted_score, None

    upper_team, lower_team = upper_lower_teams(match)
    heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
    if heat_edge < 0.52:
        return weighted_score, None

    trade = lookup.get("必发成交走势")
    euro = lookup.get("欧赔/Kelly")
    handicap = lookup.get("亚盘水位")
    bookmaker = lookup.get("公司一致性")
    fair_line = lookup.get("盘口合理性")
    depth_profile = lookup.get("盘口深度/打穿能力")
    cover_risk = lookup.get("赢盘门槛风险")
    draw_risk = lookup.get("平局风险")
    market = lookup.get("市场平衡/背离")
    snapshot = lookup.get("快照趋势")
    water_value = lookup.get("高低水价值")
    external = lookup.get("外部赔率/实力校验")
    market_elasticity = lookup.get("资金/盘口弹性")

    trade_available = bool(trade and trade.available)
    pressure = upper_favorite_trap_pressure_from_values(
        match=match,
        heat_edge=heat_edge,
        trade_available=trade_available,
        trade_edge=signal_value(trade),
        euro_edge=signal_value(euro),
        handicap_edge=signal_value(handicap),
        fair_edge=signal_value(fair_line),
        depth_edge=signal_value(depth_profile),
        cover_edge=signal_value(cover_risk),
        draw_edge=signal_value(draw_risk),
        bifa_reason=bifa.reason,
    )
    if pressure <= 0:
        return weighted_score, None

    depth = line_depth(match.asian_line)
    no_snapshot = snapshot is None or not snapshot.available
    risk_cluster = signal_value(cover_risk) <= -0.34 and signal_value(draw_risk) <= -0.10
    lower_confirmations = 0
    confirmation_reasons: list[str] = []

    if signal_value(euro) <= -0.10:
        lower_confirmations += 1
        confirmation_reasons.append("欧赔/Kelly背离")
    if signal_value(fair_line) <= -0.08:
        lower_confirmations += 1
        confirmation_reasons.append("盘口合理性偏下盘")
    if signal_value(market) <= -0.10:
        lower_confirmations += 1
        confirmation_reasons.append("市场平衡偏下盘")
    if signal_value(handicap) <= -0.12:
        lower_confirmations += 1
        confirmation_reasons.append("亚盘水位偏下盘")
    if signal_value(bookmaker) <= -0.12:
        lower_confirmations += 1
        confirmation_reasons.append("公司一致性偏下盘")
    if signal_value(water_value) <= -0.18:
        lower_confirmations += 1
        confirmation_reasons.append("水位价值偏下盘")
    if signal_value(market_elasticity) <= -0.12:
        lower_confirmations += 1
        confirmation_reasons.append("资金/盘口弹性偏下盘")
    if snapshot and snapshot.available and signal_value(snapshot) <= -0.18 and weighted_score < 0.10:
        lower_confirmations += 1
        confirmation_reasons.append("快照趋势偏下盘")
    if no_snapshot and risk_cluster:
        lower_confirmations += 1
        confirmation_reasons.append("缺少快照且一球/小胜风险集中")
    if depth >= 1.25 and signal_value(fair_line) <= -0.05 and signal_value(cover_risk) <= -0.25:
        lower_confirmations += 1
        confirmation_reasons.append("深盘打穿价值不足")

    if lower_confirmations <= 0:
        return weighted_score, None

    okooo_source = match.raw.get("_source") == "okooo"
    asian_lower_count, asian_lower_reasons = asian_lower_confirmation_cluster(lookup)
    strong_positive_snapshot_guard = bool(
        snapshot
        and snapshot.available
        and signal_value(snapshot) >= 0.50
        and weighted_score > 0
    )
    if okooo_source and asian_lower_count <= 0:
        if strong_positive_snapshot_guard:
            return weighted_score, "模型层热门陷阱保护：快照趋势强烈偏上且缺少亚盘下盘确认，不反向扣分"
        guarded_shift = clamp(pressure * 0.46 + 0.020, 0.045, 0.13)
        adjusted = max(weighted_score - guarded_shift, LOWER_THRESHOLD + 0.005)
        if adjusted < weighted_score:
            reason = "、".join(confirmation_reasons[:3])
            return adjusted, f"模型层热门陷阱降级 {guarded_shift:.3f}（{reason}；缺少亚盘下盘确认，不反打）"
        return weighted_score, None

    positive_snapshot_guard = bool(snapshot and snapshot.available and signal_value(snapshot) >= 0.25)
    if okooo_source:
        core_lower_cluster = asian_lower_count >= 2 or (asian_lower_count >= 1 and lower_confirmations >= 2)
    else:
        core_lower_cluster = (
            lower_confirmations >= 2
            or signal_value(market) <= -0.12
            or signal_value(handicap) <= -0.12
            or signal_value(fair_line) <= -0.08
            or signal_value(water_value) <= -0.18
        )
    if positive_snapshot_guard and not core_lower_cluster and weighted_score > 0:
        guarded_shift = pressure * 0.42 + 0.025
        guarded_shift = clamp(guarded_shift, 0.05, 0.13)
        adjusted = max(weighted_score - guarded_shift, LOWER_THRESHOLD + 0.005)
        if adjusted < weighted_score:
            reason = "、".join(confirmation_reasons[:3])
            return adjusted, f"模型层热门陷阱回撤 {guarded_shift:.3f}（快照仍偏上，仅单项下盘确认：{reason}）"
        return weighted_score, None

    fallback_handicap_lower = bool(
        handicap
        and handicap.available
        and signal_value(handicap) <= -0.12
        and "静态亚盘均值兜底" in handicap.reason
    )
    direct_lower_confirmation = (
        signal_value(euro) <= -0.10
        or signal_value(fair_line) <= -0.08
        or (signal_value(handicap) <= -0.12 and not fallback_handicap_lower)
        or signal_value(water_value) <= -0.18
    )
    if (
        fallback_handicap_lower
        and not direct_lower_confirmation
        and signal_value(bifa) >= 0.30
        and signal_value(external) >= 0.12
    ):
        guarded_shift = clamp(pressure * 0.45 + 0.025, 0.05, 0.14)
        adjusted = max(weighted_score - guarded_shift, LOWER_THRESHOLD + 0.005)
        if adjusted < weighted_score:
            return adjusted, f"模型层热门陷阱回撤 {guarded_shift:.3f}（亚盘为静态均水兜底，缺少直接下盘确认）"
        return weighted_score, None

    # 强热且快照冲突，但缺少欧赔/盘口/合理线等直接下盘确认时，模型只降到轻微方向。
    if (
        snapshot
        and snapshot.available
        and weighted_score >= 0.10
        and signal_value(snapshot) <= -0.12
        and signal_value(euro) > -0.10
        and signal_value(fair_line) > -0.08
        and signal_value(market) > -0.10
    ):
        adjusted = min(weighted_score, LEAN_THRESHOLD - 0.005)
        if adjusted < weighted_score:
            return adjusted, "模型层热门陷阱：静态强热与快照冲突但缺少直接下盘确认，降至轻微方向"
        return weighted_score, None

    shift = pressure * (0.86 if lower_confirmations >= 2 else 0.68) + 0.035
    if no_snapshot and risk_cluster:
        shift = max(shift, pressure)
    shift = clamp(shift, 0.10, 0.34)
    adjusted = clamp(weighted_score - shift, -1, 1)
    if adjusted >= weighted_score:
        return weighted_score, None
    reason = "、".join(confirmation_reasons[:3])
    if okooo_source and asian_lower_reasons:
        reason = "、".join(asian_lower_reasons[:3])
    return adjusted, f"模型层热门陷阱回撤 {shift:.3f}（{reason}）"


def _signal_lookup_value(signal: Signal | dict[str, Any] | None) -> float:
    if signal is None:
        return 0.0
    if isinstance(signal, dict):
        if not signal.get("available"):
            return 0.0
        try:
            return float(signal.get("score") or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return signal_value(signal)


def _signal_lookup_reason(signal: Signal | dict[str, Any] | None) -> str:
    if signal is None:
        return ""
    if isinstance(signal, dict):
        return str(signal.get("reason") or "")
    return signal.reason or ""


def asian_matrix_unconfirmed(lookup: dict[str, Signal | dict[str, Any]]) -> bool:
    """True when the handicap market lacks a real multi-bookmaker confirmation matrix."""
    handicap = lookup.get("亚盘水位")
    bookmaker = lookup.get("公司一致性")
    if bookmaker is None or (
        isinstance(bookmaker, dict) and not bookmaker.get("available")
    ) or (
        not isinstance(bookmaker, dict) and not bookmaker.available
    ):
        return True
    if handicap is None or (
        isinstance(handicap, dict) and not handicap.get("available")
    ) or (
        not isinstance(handicap, dict) and not handicap.available
    ):
        return True
    reason = _signal_lookup_reason(handicap)
    if "静态亚盘均值兜底" in reason:
        return True
    confidence_match = re.search(r"一致性可信度 ([0-9.]+)", reason)
    if confidence_match and float(confidence_match.group(1)) <= 0.20 and re.search(
        r"上0/下0", reason
    ):
        return True
    return False


def shallow_hot_trap_applies(
    match: Match,
    score: float,
    signals: list[Signal] | list[dict[str, Any]],
) -> bool:
    """Shallow hot favorite with weak positive score and no Asian-book confirmation."""
    if score <= 0 or score >= LEAN_THRESHOLD:
        return False
    if line_depth(match.asian_line) > 0.5:
        return False
    upper_team, lower_team = upper_lower_teams(match)
    if score_bifa_heat_edge(match, upper_team, lower_team) < 0.52:
        return False
    lookup = (
        {signal.name: signal for signal in signals}
        if signals and isinstance(signals[0], Signal)
        else {str(item.get("name")): item for item in signals if isinstance(item, dict) and item.get("name")}
    )
    if not asian_matrix_unconfirmed(lookup):
        return False
    if _signal_lookup_value(lookup.get("市场平衡/背离")) >= 0:
        return False
    if _signal_lookup_value(lookup.get("欧赔/Kelly")) > 0.08:
        return False
    return True


def shallow_hot_favorite_trap_score_adjustment(
    match: Match,
    weighted_score: float,
    signals: list[Signal],
) -> tuple[float, str | None]:
    """Pull weak upper scores lower using existing trap pressure and purchase-layer market shift."""
    if not shallow_hot_trap_applies(match, weighted_score, signals):
        return weighted_score, None
    lookup = {signal.name: signal for signal in signals}
    upper_team, lower_team = upper_lower_teams(match)
    heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
    bifa = lookup.get("必发指数")
    trade = lookup.get("必发成交走势")
    pressure = upper_favorite_trap_pressure_from_values(
        match=match,
        heat_edge=heat_edge,
        trade_available=bool(trade and trade.available),
        trade_edge=signal_value(trade),
        euro_edge=signal_value(lookup.get("欧赔/Kelly")),
        handicap_edge=signal_value(lookup.get("亚盘水位")),
        fair_edge=signal_value(lookup.get("盘口合理性")),
        depth_edge=signal_value(lookup.get("盘口深度/打穿能力")),
        cover_edge=signal_value(lookup.get("赢盘门槛风险")),
        draw_edge=signal_value(lookup.get("平局风险")),
        bifa_reason=bifa.reason if bifa else "",
    )
    market = signal_value(lookup.get("市场平衡/背离"))
    market_shift = 0.08 * market if market < 0 else 0.0
    shift = clamp(max(pressure, 0.04) * 0.46 + 0.02 + abs(market_shift), 0.045, 0.12)
    adjusted = weighted_score - shift
    if adjusted >= weighted_score - 1e-9:
        return weighted_score, None
    return (
        adjusted,
        f"浅盘热门陷阱压分 -{shift:.3f}（大热缺亚盘/欧赔确认，市场平衡偏负）",
    )


def shallow_antihot_value_confirmation_guard(
    match: Match,
    weighted_score: float,
    signals: list[Signal],
) -> tuple[float, str | None]:
    """Separate shallow favorite risk from real lower-side value.

    In a shallow/quarter-ball market, a hot upper side can be dangerous, but
    "upper risk" is not automatically "lower value".  The model is allowed to
    keep or turn lower only when lower-side value is confirmed by several
    independent channels; if the latest trade/snapshot flow is already
    recovering toward the hot upper side, the output is pulled back to a light
    upper lean instead of treating the risk cluster as a strong lower pick.
    """
    if match.raw.get("_source") != "okooo":
        return weighted_score, None
    if weighted_score >= -LEAN_THRESHOLD:
        return weighted_score, None
    if line_depth(match.asian_line) > 0.5:
        return weighted_score, None

    lookup = {signal.name: signal for signal in signals}
    upper_team, lower_team = upper_lower_teams(match)
    heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
    if heat_edge < 0.25:
        return weighted_score, None
    if weighted_score * heat_edge >= 0:
        return weighted_score, None

    handicap = lookup.get("亚盘水位")
    bookmaker = lookup.get("公司一致性")
    trade = lookup.get("必发成交走势")
    euro = lookup.get("欧赔/Kelly")
    market = lookup.get("市场平衡/背离")
    fair_line = lookup.get("盘口合理性")
    cover_risk = lookup.get("赢盘门槛风险")
    snapshot = lookup.get("快照趋势")
    market_elasticity = lookup.get("资金/盘口弹性")
    score_momentum = lookup.get("临场score变化")

    lower_confirmations: list[str] = []
    if signal_value(handicap) <= -0.22 and not (
        handicap and "静态亚盘均值兜底" in handicap.reason
    ):
        lower_confirmations.append("亚盘水位")
    if signal_value(bookmaker) <= -0.16:
        lower_confirmations.append("公司一致性")
    if signal_value(euro) <= -0.12:
        lower_confirmations.append("欧赔/Kelly")
    if signal_value(trade) <= -0.12:
        lower_confirmations.append("成交走势")
    if signal_value(fair_line) <= -0.08:
        lower_confirmations.append("公平盘口")
    if signal_value(market_elasticity) <= -0.12:
        lower_confirmations.append("资金/盘口弹性")
    if snapshot and snapshot.available and signal_value(snapshot) <= -0.18:
        lower_confirmations.append("快照趋势")

    upper_recovery: list[str] = []
    if signal_value(trade) >= 0.08:
        upper_recovery.append("成交走势回到上盘")
    if signal_value(euro) >= 0.12:
        upper_recovery.append("欧赔/Kelly回到上盘")
    if signal_value(snapshot) >= 0.10:
        upper_recovery.append("快照最近回到上盘")
    if signal_value(score_momentum) >= 0.25:
        upper_recovery.append("临场score回升")
    if signal_value(market_elasticity) >= 0.08:
        upper_recovery.append("资金后盘口响应偏上")

    handicap_reason = handicap.reason if handicap else ""
    extreme_company_strong = "公司强确认" in handicap_reason
    extreme_company_match = re.search(r"公司极值后回撤同向\s+(\d+)/(\d+)", handicap_reason)
    if extreme_company_match:
        same = int(extreme_company_match.group(1))
        total = max(int(extreme_company_match.group(2)), 1)
        if total >= 8 and same / total >= 0.65:
            extreme_company_strong = True
    retracement_company_confirmed = (
        "高点回撤" in handicap_reason
        and extreme_company_strong
        and (
            signal_value(market_elasticity) <= 0.02
            or signal_value(market) <= -0.08
            or signal_value(handicap) <= -0.45
        )
    )
    if retracement_company_confirmed:
        return weighted_score, None

    # Very coherent lower clusters are allowed to stand.  Otherwise, shallow
    # anti-hot is only a risk warning; it should not dominate a two-way market.
    strong_lower_cluster = (
        len(lower_confirmations) >= 3
        and not upper_recovery
        and signal_value(market) <= -0.35
        and signal_value(cover_risk) <= 0.02
    )
    if strong_lower_cluster:
        return weighted_score, None

    if len(lower_confirmations) >= 4 and not upper_recovery:
        return weighted_score, None
    if len(lower_confirmations) >= 2 and not upper_recovery:
        return weighted_score, None

    adjusted = max(weighted_score, MODEL_DIRECTION_EPSILON)
    if adjusted <= weighted_score + 1e-9:
        return weighted_score, None

    lower_text = "、".join(lower_confirmations[:4]) if lower_confirmations else "直接下盘确认不足"
    recovery_text = "、".join(upper_recovery[:3]) if upper_recovery else "缺少多通道下盘确认"
    return (
        adjusted,
        (
            f"浅盘反热门价值确认保护：{upper_team}是热门，但{lower_team}下盘确认不足"
            f"（{lower_text}；{recovery_text}），从下盘风险分回拉为轻微上盘"
        ),
    )


def deep_favorite_live_recovery_applies(
    match: Match,
    last_score: float,
    aggregate_score: float,
    signals: list[Signal] | list[dict[str, Any]],
) -> bool:
    if aggregate_score >= 0 or last_score <= -0.06:
        return False
    if line_depth(match.asian_line) < 2.0:
        return False
    lookup = (
        {signal.name: signal for signal in signals}
        if signals and isinstance(signals[0], Signal)
        else {str(item.get("name")): item for item in signals if isinstance(item, dict) and item.get("name")}
    )
    if _signal_lookup_value(lookup.get("欧赔/Kelly")) < 0.30:
        return False
    if _signal_lookup_value(lookup.get("市场平衡/背离")) <= -0.35:
        return False
    if _signal_lookup_value(lookup.get("临场score变化")) < 0.60:
        return False
    if _signal_lookup_value(lookup.get("亚盘水位")) < -0.15:
        return False
    return True


def deep_favorite_live_recovery_aggregate_score(
    median_score: float,
    last_score: float,
    match: Match,
    last_signals: list[Signal] | list[dict[str, Any]],
) -> tuple[float, str | None]:
    """Blend median toward the recovering last snapshot; lift uses purchase-layer momentum coeff."""
    if not deep_favorite_live_recovery_applies(match, last_score, median_score, last_signals):
        return median_score, None
    lookup = (
        {signal.name: signal for signal in last_signals}
        if last_signals and isinstance(last_signals[0], Signal)
        else {
            str(item.get("name")): item
            for item in last_signals
            if isinstance(item, dict) and item.get("name")
        }
    )
    momentum = _signal_lookup_value(lookup.get("临场score变化"))
    recovery_gap = max(last_score - median_score, 0.0)
    live_weight = clamp(
        0.50 + 0.35 * min(recovery_gap / LEAN_THRESHOLD, 1.0),
        0.50,
        0.85,
    )
    aggregated = (1.0 - live_weight) * median_score + live_weight * last_score
    aggregated += PURCHASE_LAYER_MOMENTUM_COEFF * max(momentum, 0.0)
    aggregated = clamp(aggregated, -1, 1)
    return (
        aggregated,
        (
            f"深盘强队临场修复：中位 {median_score:+.3f}、最后 {last_score:+.3f} "
            f"按修复幅度加权 {live_weight:.2f}，临场动能 +{PURCHASE_LAYER_MOMENTUM_COEFF * max(momentum, 0.0):.3f}"
            f" -> {aggregated:+.3f}"
        ),
    )


def guarded_median_snapshot_recommendation(
    match: Match,
    median_score: float,
    last_score: float | None,
    last_signals: list[Signal] | list[dict[str, Any]],
) -> tuple[str, float, list[str]]:
    guarded_score = median_score
    notes: list[str] = []
    if last_score is not None:
        aggregated, deep_note = deep_favorite_live_recovery_aggregate_score(
            median_score,
            last_score,
            match,
            last_signals,
        )
        if deep_note:
            guarded_score = aggregated
            notes.append(deep_note)
    recommendation = _recommendation_from_median_score(guarded_score)
    return recommendation, guarded_score, notes


def _recommendation_from_median_score(score: float) -> str:
    if score >= 0:
        return "上盘"
    return "下盘"


def recommendation_from_score(score: float) -> str:
    return _recommendation_from_median_score(score)


def score_strength_label(score: float) -> str:
    abs_score = abs(score)
    if abs_score < UPPER_THRESHOLD:
        return "轻微"
    if abs_score < STRONG_THRESHOLD:
        return "中等"
    return "强烈"


def model_decision_reason(weighted_score: float, recommendation: str, available_weight: float) -> str:
    strength = score_strength_label(weighted_score)
    if recommendation not in ("上盘", "下盘"):
        recommendation = recommendation_from_score(weighted_score)
    reason = f"模型综合分 {weighted_score:+.3f}，直接推荐{recommendation}，强度{strength}"
    if available_weight < 0.50:
        reason += "；可用信号偏少，置信度已按数据完整度压低"
    reason += "；未再叠加购买门槛"
    return reason


def snapshot_stop_update_lift(match: Match, snapshot_trend: Signal | None) -> bool:
    """True when SPDEX marks the match stopped but we still have usable local snapshot history."""
    return bool(
        match.is_stop_update
        and snapshot_trend is not None
        and snapshot_trend.available
    )


def confidence_kwargs_for_snapshot_stop_lift(snapshot_stop_lift: bool, available_weight: float) -> dict[str, Any]:
    """Relax purchase confidence caps when live feeds freeze but snapshots still inform the run."""
    if not snapshot_stop_lift or available_weight >= 0.65:
        return {}
    kwargs: dict[str, Any] = {"confidence_cap_weight": max(available_weight, 0.58)}
    if available_weight < 0.50:
        kwargs["confidence_completeness_floor"] = 72
    return kwargs


def snapshot_record_time(record: dict[str, Any]) -> datetime | None:
    return parse_datetime_or_none(record.get("fetched_at") or record.get("timestamp"))


def current_score_momentum_signal(current_score: float, snapshot_context: SnapshotContext | None) -> Signal:
    if snapshot_context is None or not snapshot_context.records:
        return unavailable_signal("临场score变化", 0.0, "本轮之前无本地快照")

    first_score = snapshot_context.first_metrics.get("score", 0.0)
    previous_score = snapshot_context.last_metrics.get("score", first_score)
    recent_delta = current_score - previous_score
    total_delta = current_score - first_score
    momentum = clamp((0.85 * recent_delta + 0.15 * total_delta) / 0.22, -1, 1)
    notes: list[str] = []

    if previous_score > LEAN_THRESHOLD and current_score < LOWER_THRESHOLD:
        momentum = min(momentum, -0.85)
        notes.append("本轮跌破下盘阈值")
    elif previous_score < -LEAN_THRESHOLD and current_score > UPPER_THRESHOLD:
        momentum = max(momentum, 0.85)
        notes.append("本轮升破上盘阈值")
    elif abs(current_score) < 0.08 and abs(recent_delta) < 0.04:
        momentum = clamp(momentum, -0.20, 0.20)
        notes.append("当前score仍弱，历史趋势降权参考")

    last_snapshot_time = snapshot_record_time(snapshot_context.records[-1])
    if last_snapshot_time is not None:
        age_minutes = max(0.0, (datetime.now(timezone.utc) - last_snapshot_time).total_seconds() / 60.0)
        if age_minutes < 8.0:
            recency_factor = clamp(0.20 + age_minutes / 10.0, 0.20, 0.95)
            momentum *= recency_factor
            notes.append(f"上一快照仅 {age_minutes:.1f} 分钟前写入，临场动能降权")

    reason = (
        f"当前score {current_score:+.3f}，上一快照 {previous_score:+.3f}，"
        f"近期变化 {recent_delta:+.3f}，总变化 {total_delta:+.3f}"
    )
    if notes:
        reason += "；" + "；".join(notes)
    return Signal("临场score变化", momentum, 0.0, True, reason)


# 模型只有轻微方向时，购买层若增强方向，需欧赔/Kelly 与亚盘同向且购买分足够，避免弱噪声买错边
MODEL_WAIT_PURCHASE_ABS_FLOOR = 0.14
MODEL_WAIT_CORE_SCORE_THRESH = 0.06
MODEL_WAIT_SINGLE_CORE_ABS_FLOOR = 0.18


def purchase_core_signals_confirm_when_model_wait(
    match: Match, lookup: dict[str, Signal], want_upper: bool, abs_adjusted: float
) -> bool:
    if abs_adjusted < MODEL_WAIT_PURCHASE_ABS_FLOOR:
        return False
    need = 1 if want_upper else -1
    euro = lookup.get("欧赔/Kelly")
    ah = lookup.get("亚盘水位")
    bookmaker = lookup.get("公司一致性")

    def dir3(sig: Signal | None) -> int:
        if sig is None or not sig.available:
            return 0
        if sig.score > MODEL_WAIT_CORE_SCORE_THRESH:
            return 1
        if sig.score < -MODEL_WAIT_CORE_SCORE_THRESH:
            return -1
        return 0

    de, dh = dir3(euro), dir3(ah)
    if want_upper:
        asian_upper_count, _asian_upper_reasons = asian_upper_confirmation_cluster(lookup)
        if asian_upper_count >= 2 and abs_adjusted >= 0.10:
            return True
    else:
        asian_lower_count, _asian_lower_reasons = asian_lower_confirmation_cluster(lookup)
        if (
            asian_lower_count >= 2
            and signal_value(ah) <= -0.10
            and signal_value(bookmaker) <= -0.12
            and abs_adjusted >= 0.10
        ):
            return True
    if not want_upper:
        bifa = lookup.get("必发指数")
        external = lookup.get("外部赔率/实力校验")
        fallback_or_missing_asian = bool(
            (ah and "静态亚盘均值兜底" in ah.reason)
            or bookmaker is None
            or not bookmaker.available
        )
        if match.raw.get("_source") == "okooo" and asian_lower_count < 2 and fallback_or_missing_asian:
            return False
        fallback_handicap_lower = bool(
            ah
            and ah.available
            and signal_value(ah) <= -0.12
            and "静态亚盘均值兜底" in ah.reason
        )
        if (
            fallback_handicap_lower
            and signal_value(euro) > -0.10
            and signal_value(bifa) >= 0.30
            and signal_value(external) >= 0.12
        ):
            return False
    non_zero = [x for x in (de, dh) if x != 0]
    if len(non_zero) >= 2:
        return all(x == need for x in non_zero)
    if len(non_zero) == 1:
        if non_zero[0] == need and abs_adjusted >= MODEL_WAIT_SINGLE_CORE_ABS_FLOOR:
            return True
    if not want_upper:
        market = lookup.get("市场平衡/背离")
        fair_line = lookup.get("盘口合理性")
        cover_risk = lookup.get("赢盘门槛风险")
        water_value = lookup.get("高低水价值")
        bifa = lookup.get("必发指数")
        external = lookup.get("外部赔率/实力校验")
        market_score = signal_value(market)
        trap_reason = "热门陷阱" in (market.reason if market else "")
        shallow_hot_trap = trap_reason and "浅盘大热" in (bifa.reason if bifa else "")
        fallback_handicap_lower = bool(
            ah
            and ah.available
            and signal_value(ah) <= -0.12
            and "静态亚盘均值兜底" in ah.reason
        )
        direct_lower_confirmation = (
            signal_value(euro) <= -0.10
            or signal_value(fair_line) <= -0.08
            or (signal_value(ah) <= -0.12 and not fallback_handicap_lower)
            or signal_value(water_value) <= -0.18
        )
        if (
            fallback_handicap_lower
            and not direct_lower_confirmation
            and signal_value(bifa) >= 0.30
            and signal_value(external) >= 0.12
        ):
            return False
        if (
            abs_adjusted >= 0.13
            and (
                market_score <= -0.18
                or (
                    trap_reason
                    and (
                        signal_value(fair_line) <= -0.08
                        or (shallow_hot_trap and signal_value(fair_line) <= -0.03)
                        or signal_value(water_value) <= -0.18
                        or signal_value(euro) <= -0.10
                    )
                )
            )
            and (
                signal_value(fair_line) <= -0.08
                or (shallow_hot_trap and signal_value(fair_line) <= -0.03)
                or signal_value(cover_risk) <= -0.25
                or signal_value(water_value) <= -0.18
                or signal_value(euro) <= -0.10
            )
        ):
            return True
        snapshot_trend = lookup.get("快照趋势")
        draw_risk = lookup.get("平局风险")
        handicap = lookup.get("亚盘水位")
        no_snapshot = snapshot_trend is None or not snapshot_trend.available
        if (
            abs_adjusted >= 0.12
            and no_snapshot
            and trap_reason
            and signal_value(cover_risk) <= -0.34
            and signal_value(draw_risk) <= -0.10
            and signal_value(euro) <= 0.03
            and signal_value(handicap) <= 0.04
        ):
            return True
    else:
        market = lookup.get("市场平衡/背离")
        bifa = lookup.get("必发指数")
        depth_profile = lookup.get("盘口深度/打穿能力")
        external_consensus = lookup.get("外部赔率/实力校验")
        snapshot_trend = lookup.get("快照趋势")
        cover_risk = lookup.get("赢盘门槛风险")
        fair_line = lookup.get("盘口合理性")
        score_momentum = lookup.get("临场score变化")
        if (
            abs_adjusted >= 0.12
            and signal_value(euro) >= 0.40
            and signal_value(depth_profile) >= 0.15
            and signal_value(external_consensus) >= 0.10
            and signal_value(snapshot_trend) >= 0.12
            and signal_value(cover_risk) > -0.35
            and signal_value(market) >= 0.10
        ):
            return True
        if (
            abs_adjusted >= 0.14
            and signal_value(bifa) >= 0.25
            and signal_value(euro) >= 0.15
            and signal_value(score_momentum) >= 0.50
            and signal_value(fair_line) >= -0.05
            and signal_value(cover_risk) >= -0.05
            and signal_value(snapshot_trend) > -0.12
        ):
            return True
        draw_risk = lookup.get("平局风险")
        if (
            abs_adjusted >= MODEL_WAIT_PURCHASE_ABS_FLOOR
            and signal_value(euro) >= 0.35
            and signal_value(external_consensus) >= 0.08
            and signal_value(score_momentum) >= 0.80
            and signal_value(draw_risk) >= 0.04
            and signal_value(cover_risk) > -0.50
            and signal_value(fair_line) > -0.35
        ):
            return True
    return False


def purchase_decision_from_signals(
    match: Match,
    weighted_score: float,
    completeness: int,
    available_weight: float,
    model_recommendation: str,
    signals: list[Signal],
) -> PurchaseDecision:
    """Convert the model output into a purchase decision with risk gates."""
    lookup = {signal.name: signal for signal in signals}
    snapshot_stop_lift = snapshot_stop_update_lift(match, lookup.get("快照趋势"))
    conf_kwargs = confidence_kwargs_for_snapshot_stop_lift(snapshot_stop_lift, available_weight)
    adjusted_score = weighted_score
    reasons: list[str] = [f"原始综合分 {weighted_score:+.3f}"]
    depth = line_depth(match.asian_line)

    if available_weight < 0.50 and not snapshot_stop_lift:
        reasons.append("可用信号不足，方向仅按轻微分级输出")
        side = recommendation_from_score(adjusted_score)
        if abs(adjusted_score) < MODEL_DIRECTION_EPSILON:
            adjusted_score = MODEL_DIRECTION_EPSILON if side == "上盘" else -MODEL_DIRECTION_EPSILON
        confidence = purchase_confidence_from_score(
            purchase_score=adjusted_score,
            completeness=completeness,
            available_weight=available_weight,
            model_recommendation=model_recommendation,
            final_side=side,
            raw_score=weighted_score,
        )
        return PurchaseDecision(
            side=side,
            score=adjusted_score,
            confidence=confidence,
            reason="；".join(reasons),
            is_reversed=False,
        )

    if available_weight < 0.50 and snapshot_stop_lift:
        reasons.append("临场停更且可用权重偏低；继续应用购买门控（快照趋势已参与加权）")

    secondary_enabled = abs(weighted_score) < 0.15 or model_recommendation not in ("上盘", "下盘")
    if secondary_enabled:
        reasons.append("原始优势较弱，启用购买门控")

    handicap = lookup.get("亚盘水位")
    bookmaker_consensus = lookup.get("公司一致性")
    market_balance = lookup.get("市场平衡/背离")
    euro_kelly = lookup.get("欧赔/Kelly")
    bifa = lookup.get("必发指数")
    trade = lookup.get("必发成交走势")
    fair_line = lookup.get("盘口合理性")
    market_elasticity = lookup.get("资金/盘口弹性")
    strong_upper_consensus = (
        weighted_score > 0.10
        and signal_value(handicap) >= 0.40
        and signal_value(bookmaker_consensus) >= 0.30
    )
    strong_upper_consensus_euro = (
        weighted_score > 0.05
        and signal_value(handicap) >= 0.35
        and signal_value(bookmaker_consensus) >= 0.28
        and signal_value(euro_kelly) >= 0.25
    )
    strong_upper_protected = strong_upper_consensus or strong_upper_consensus_euro

    cover_risk = lookup.get("赢盘门槛风险")
    deep_asian_lower_count, deep_asian_lower_reasons = asian_lower_confirmation_cluster(lookup)
    deep_asian_upper_count, _deep_asian_upper_reasons = asian_upper_confirmation_cluster(lookup)
    okooo_deep_cover_block = (
        match.raw.get("_source") == "okooo"
        and depth >= 1.25
        and deep_asian_lower_count >= 2
        and deep_asian_upper_count < 2
        and not strong_upper_protected
        and weighted_score > -LEAN_THRESHOLD
    )
    if okooo_deep_cover_block:
        deep_cover_pressure = clamp(
            0.08
            + 0.035 * min(deep_asian_lower_count, 4)
            + 0.12 * clamp(abs(min(signal_value(handicap), 0.0)) - 0.10, 0, 0.45)
            + 0.10 * clamp(abs(min(signal_value(bookmaker_consensus), 0.0)) - 0.12, 0, 0.45),
            0.10,
            0.28,
        )
        if signal_value(euro_kelly) >= 0.35 or signal_value(bifa) >= 0.45:
            reasons.append("深盘必发/欧赔仅确认胜负，需服从亚盘打穿确认")
        adjusted_score -= deep_cover_pressure
        reasons.append(f"深盘亚盘确认不足修正 -{deep_cover_pressure:.2f}（{'、'.join(deep_asian_lower_reasons[:3])}）")

    if (
        secondary_enabled
        and cover_risk
        and cover_risk.available
        and cover_risk.score < -0.05
        and weighted_score > -LEAN_THRESHOLD
    ):
        risk_shift = clamp(abs(cover_risk.score) * 0.35, 0.02, 0.20)
        if strong_upper_protected:
            risk_shift *= 0.30
            reasons.append(
                "强亚盘/公司共识保护，门槛风险仅降权不反向"
                if strong_upper_consensus
                else "强亚盘/公司/欧赔共识保护，门槛风险仅降权不反向"
            )
        adjusted_score -= risk_shift
        reasons.append(f"上盘赢盘门槛风险向下修正 {risk_shift:.2f}")
        if cover_risk.score <= -0.35 and weighted_score > 0 and not strong_upper_protected:
            adjusted_score = max(adjusted_score, weighted_score * 0.35)
            reasons.append("门槛风险较高，仅缩小上盘优势")

    score_momentum = lookup.get("临场score变化")
    momentum_shift = 0.0
    if secondary_enabled and score_momentum and score_momentum.available and abs(score_momentum.score) >= 0.25:
        momentum_shift = 0.14 * score_momentum.score
        adjusted_score += momentum_shift
        reasons.append(f"临场score变化修正 {momentum_shift:+.2f}")

    snapshot_trend = lookup.get("快照趋势")
    if secondary_enabled and snapshot_trend and snapshot_trend.available:
        trend_shift = 0.18 * snapshot_trend.score
        if score_momentum and score_momentum.available:
            if snapshot_trend.score * score_momentum.score < 0:
                trend_shift *= 0.35
                reasons.append("临场score变化与历史快照趋势冲突，快照修正降权")
            elif abs(weighted_score) < 0.08 and abs(score_momentum.score) < 0.30:
                trend_shift *= 0.45
                reasons.append("当前score未强确认历史快照趋势，快照修正降权")
            elif snapshot_trend.score * score_momentum.score > 0 and abs(score_momentum.score) >= 0.25:
                # 临场修正已用「当前综合分 − 上条快照」；快照趋势含相邻快照间 score，同向时降权去重
                trend_shift *= 0.52
                reasons.append("临场与快照趋势同向，快照修正去重降权")
        adjusted_score += trend_shift
        if abs(trend_shift) >= 0.02:
            reasons.append(f"快照趋势二次修正 {trend_shift:+.2f}")

    if secondary_enabled and market_balance and market_balance.available and abs(market_balance.score) >= 0.25:
        market_shift = 0.08 * market_balance.score
        adjusted_score += market_shift
        reasons.append(f"盘口防守/背离修正 {market_shift:+.2f}")

    if secondary_enabled and market_elasticity and market_elasticity.available and abs(market_elasticity.score) >= 0.12:
        elasticity_shift = 0.12 * market_elasticity.score
        elasticity_direction = math.copysign(1.0, market_elasticity.score)
        conflict_count = 0
        for core_name in ("必发成交走势", "欧赔/Kelly"):
            core_signal = lookup.get(core_name)
            if core_signal and core_signal.available and elasticity_direction * core_signal.score <= -0.10:
                conflict_count += 1
        if conflict_count >= 2:
            elasticity_shift *= 0.45
            reasons.append("资金/盘口弹性与成交/Kelly冲突，修正降权")
        adjusted_score += elasticity_shift
        reasons.append(f"资金/盘口弹性修正 {elasticity_shift:+.2f}")

    if secondary_enabled and handicap and handicap.available and abs(handicap.score) >= 0.15:
        water_shift = 0.10 * handicap.score
        adjusted_score += water_shift
        reasons.append(f"亚盘水位二次修正 {water_shift:+.2f}")

    water_value = lookup.get("高低水价值")
    if secondary_enabled and water_value and water_value.available and abs(water_value.score) >= 0.12:
        value_shift = 0.075 * water_value.score
        if strong_upper_protected and value_shift < 0:
            value_shift *= 0.40
            reasons.append("强亚盘/公司共识保护，低水价值负修正降权")
        adjusted_score += value_shift
        reasons.append(f"高低水价值修正 {value_shift:+.2f}")

    external_consensus = lookup.get("外部赔率/实力校验")
    if (
        secondary_enabled
        and match.raw.get("_source") != "okooo"
        and external_consensus
        and external_consensus.available
        and abs(external_consensus.score) >= 0.12
    ):
        external_shift = 0.10 * external_consensus.score
        adjusted_score += external_shift
        reasons.append(f"外部赔率/实力修正 {external_shift:+.2f}")

    draw_risk = lookup.get("平局风险")
    if (
        secondary_enabled
        and draw_risk
        and draw_risk.available
        and depth <= 1.25
        and draw_risk.score < -0.05
        and weighted_score > -LEAN_THRESHOLD
    ):
        draw_shift = clamp(0.12 * draw_risk.score, -0.08, 0)
        adjusted_score += draw_shift
        reasons.append(f"平局/小胜风险修正 {draw_shift:+.2f}")

    depth_profile = lookup.get("盘口深度/打穿能力")
    if secondary_enabled and depth_profile and depth_profile.available:
        if depth >= 1.50 and depth_profile.score < 0.10 and weighted_score > -LEAN_THRESHOLD:
            adjusted_score -= 0.10
            reasons.append("深盘打穿能力不足，倾向下盘")
        elif depth <= 0.50 and depth_profile.score > 0.20:
            adjusted_score += 0.04
            reasons.append("浅盘胜负确认补强上盘")

    if (
        secondary_enabled
        and euro_kelly
        and euro_kelly.available
        and euro_kelly.score >= 0.40
        and depth_profile
        and depth_profile.available
        and depth_profile.score >= 0.15
        and external_consensus
        and external_consensus.available
        and external_consensus.score >= 0.10
        and snapshot_trend
        and snapshot_trend.available
        and snapshot_trend.score >= 0.12
        and signal_value(cover_risk) > -0.35
    ):
        adjusted_score = max(adjusted_score, MODEL_WAIT_PURCHASE_ABS_FLOOR)
        reasons.append("欧赔/外部/快照强确认，低优势上盘可投资")

    if (
        model_recommendation == "上盘"
        and depth >= 1.25
        and euro_kelly
        and euro_kelly.available
        and euro_kelly.score >= 0.35
        and score_momentum
        and score_momentum.available
        and score_momentum.score >= 0.60
        and draw_risk
        and draw_risk.available
        and draw_risk.score >= 0.05
        and external_consensus
        and external_consensus.available
        and external_consensus.score >= 0.08
        and weighted_score >= UPPER_THRESHOLD
    ):
        adjusted_score = max(adjusted_score, 0.09)
        reasons.append("深盘欧赔强支撑且临场score回升，保留上盘投资")

    if (
        secondary_enabled
        and depth >= 1.25
        and euro_kelly
        and euro_kelly.available
        and euro_kelly.score >= 0.35
        and score_momentum
        and score_momentum.available
        and score_momentum.score >= 0.80
        and draw_risk
        and draw_risk.available
        and draw_risk.score >= 0.04
        and external_consensus
        and external_consensus.available
        and external_consensus.score >= 0.08
        and signal_value(fair_line) > -0.35
        and signal_value(cover_risk) > -0.50
        and weighted_score >= 0.06
    ):
        adjusted_score = max(adjusted_score, MODEL_WAIT_PURCHASE_ABS_FLOOR)
        reasons.append("深盘欧赔/外部支撑且临场score强回升，低优势上盘可投资")

    if secondary_enabled and bifa and bifa.available:
        upper_team, lower_team = upper_lower_teams(match)
        heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        trap_pressure = upper_favorite_trap_pressure_from_values(
            match=match,
            heat_edge=heat_edge,
            trade_available=bool(trade and trade.available),
            trade_edge=signal_value(trade),
            euro_edge=signal_value(euro_kelly),
            handicap_edge=signal_value(handicap),
            fair_edge=signal_value(fair_line),
            depth_edge=signal_value(depth_profile),
            cover_edge=signal_value(cover_risk),
            draw_edge=signal_value(draw_risk),
            bifa_reason=bifa.reason,
        )
        if trap_pressure > 0:
            adjusted_score -= trap_pressure
            reasons.append(f"上盘热门缺少成交/欧赔/盘口确认，陷阱修正 -{trap_pressure:.2f}")

    if secondary_enabled and bookmaker_consensus and bookmaker_consensus.available and bookmaker_consensus.score < -0.12:
        adjusted_score -= 0.05
        reasons.append("主流公司一致性偏下盘")

    if strong_upper_protected and adjusted_score < 0:
        adjusted_score = max(min(weighted_score * 0.50, 0.08), 0.03)
        reasons.append("强亚盘/公司共识保护，不允许二次门控反向")

    if weighted_score > 0.08 and (
        (
            weighted_score > 0.10
            and signal_value(market_balance) > 0.45
            and signal_value(handicap) > 0.30
            and signal_value(cover_risk) > -0.55
        )
        or (
            signal_value(euro_kelly) >= 0.30
            and signal_value(handicap) > 0.28
            and signal_value(cover_risk) > -0.55
            and signal_value(market_balance) > 0.35
        )
    ):
        adjusted_score = max(adjusted_score, 0.06)
        reasons.append("盘口防守和亚盘同向，保留正向")

    adjusted_score = clamp(adjusted_score, -1, 1)
    reference_side = reference_side_from_model(weighted_score, model_recommendation)
    if reference_side == "无明显倾向" and abs(adjusted_score) < 0.06:
        side = recommendation_from_score(adjusted_score)
        adjusted_score = MODEL_DIRECTION_EPSILON if side == "上盘" else -MODEL_DIRECTION_EPSILON
        reasons.append("方向和购买优势都不足，仅输出轻微方向")
    else:
        side = "上盘" if adjusted_score > 0 else "下盘"
    if side in ("上盘", "下盘") and abs(adjusted_score) < 0.06:
        reasons.append("购买优势过低，降为轻微方向")
        adjusted_score = MODEL_DIRECTION_EPSILON if side == "上盘" else -MODEL_DIRECTION_EPSILON

    fallback_or_missing_asian = bool(
        (handicap and "静态亚盘均值兜底" in handicap.reason)
        or bookmaker_consensus is None
        or not bookmaker_consensus.available
    )
    if (
        side == "下盘"
        and match.raw.get("_source") == "okooo"
        and deep_asian_lower_count < 2
        and fallback_or_missing_asian
        and (
            model_recommendation not in ("上盘", "下盘")
            or abs(weighted_score) < 0.16
            or signal_value(euro_kelly) >= 0.10
            or signal_value(bifa) >= 0.25
        )
    ):
        reasons.append("下盘缺少真实亚盘/公司确认，风险信号只降为轻微下盘")
        adjusted_score = -MODEL_DIRECTION_EPSILON

    if side == "上盘" and okooo_deep_cover_block and adjusted_score < 0.18:
        reasons.append("深盘亚盘/公司未确认打穿，剩余上盘优势不足，降为轻微上盘")
        adjusted_score = MODEL_DIRECTION_EPSILON

    # 整数一球盘：上盘净胜 1 常为走水；综合分未拉开且赢盘门槛/平局风险已明显预警时，不强吃上盘。
    lv_one = line_value(match.asian_line)
    if (
        side == "上盘"
        and abs(abs(lv_one) - 1.0) < 1e-6
        and cover_risk
        and cover_risk.available
        and cover_risk.score <= -0.32
        and draw_risk
        and draw_risk.available
        and draw_risk.score <= -0.12
        and weighted_score < 0.22
    ):
        adjusted_score = MODEL_DIRECTION_EPSILON
        reasons.append("整数一球盘：走水/净胜边界与门槛及平局风险叠加，只保留轻微上盘")

    if (
        model_recommendation not in ("上盘", "下盘")
        and side in ("上盘", "下盘")
        and not purchase_core_signals_confirm_when_model_wait(match, lookup, side == "上盘", abs(adjusted_score))
    ):
        reasons.append("模型只有轻微方向，欧赔/Kelly与亚盘未同向增强；保持轻微")
        adjusted_score = MODEL_DIRECTION_EPSILON if side == "上盘" else -MODEL_DIRECTION_EPSILON

    attempted_reverse = (
        reference_side in ("上盘", "下盘")
        and side != reference_side
        and abs(weighted_score) >= LEAN_THRESHOLD
    )
    model_wait_reverse_confirmed = (
        attempted_reverse
        and model_recommendation not in ("上盘", "下盘")
        and purchase_core_signals_confirm_when_model_wait(match, lookup, side == "上盘", abs(adjusted_score))
    )
    model_direction_reverse_confirmed = (
        attempted_reverse
        and model_recommendation in ("上盘", "下盘")
        and purchase_core_signals_confirm_when_model_wait(match, lookup, side == "上盘", abs(adjusted_score))
    )
    if attempted_reverse and (
        (model_recommendation not in ("上盘", "下盘") and not model_wait_reverse_confirmed)
        or (model_recommendation in ("上盘", "下盘") and not model_direction_reverse_confirmed)
        or abs(adjusted_score) < 0.10
        or (completeness < 60 and not snapshot_stop_lift)
    ):
        reasons.append(f"二次门控尝试由{reference_side}反向到{side}，但核心确认或置信不足，保留原轻微方向")
        side = reference_side
        adjusted_score = MODEL_DIRECTION_EPSILON if side == "上盘" else -MODEL_DIRECTION_EPSILON
        attempted_reverse = False
    elif attempted_reverse:
        reasons.append(f"最终购买方向由{reference_side}反向到{side}")
    elif model_recommendation not in ("上盘", "下盘") and side in ("上盘", "下盘"):
        reasons.append(f"模型只有轻微方向，低优势选择{side}")
    else:
        reasons.append(f"最终购买方向保持{side}")

    confidence = purchase_confidence_from_score(
        purchase_score=adjusted_score,
        completeness=completeness,
        available_weight=available_weight,
        model_recommendation=model_recommendation,
        final_side=side,
        raw_score=weighted_score,
        **conf_kwargs,
    )
    is_reversed = attempted_reverse and side in ("上盘", "下盘")
    return PurchaseDecision(
        side=side,
        score=adjusted_score,
        confidence=confidence,
        reason="；".join(reasons),
        is_reversed=is_reversed,
    )


def signal_value(signal: Signal | None) -> float:
    if signal is None or not signal.available:
        return 0.0
    return signal.score


def reference_side_from_model(score: float, model_recommendation: str) -> str:
    if model_recommendation in ("上盘", "下盘"):
        return model_recommendation
    if score > LEAN_THRESHOLD:
        return "上盘"
    if score < -LEAN_THRESHOLD:
        return "下盘"
    return "无明显倾向"


def purchase_confidence_from_score(
    purchase_score: float,
    completeness: int,
    available_weight: float,
    model_recommendation: str,
    final_side: str,
    raw_score: float,
    *,
    confidence_cap_weight: float | None = None,
    confidence_completeness_floor: int | None = None,
) -> int:
    if available_weight <= 0:
        return 0
    cap_weight = available_weight if confidence_cap_weight is None else confidence_cap_weight
    eff_completeness = completeness
    if confidence_completeness_floor is not None:
        eff_completeness = max(eff_completeness, confidence_completeness_floor)
    eff_completeness = clamp_int(eff_completeness, 0, 100)
    if final_side not in ("上盘", "下盘"):
        final_side = recommendation_from_score(purchase_score)
    strength = min(abs(purchase_score), 0.70)
    base = 32 + strength * 72
    if model_recommendation == final_side:
        base += 8
    elif model_recommendation not in ("上盘", "下盘"):
        base -= 4
    else:
        base -= 8
    if abs(raw_score) < LEAN_THRESHOLD and strength < 0.12:
        base -= 6
    if cap_weight < 0.25:
        base = min(base, 25)
    elif cap_weight < 0.50:
        base = min(base, 35)
    elif cap_weight < 0.65:
        base = min(base, 55)
    quality_factor = 0.50 + 0.50 * (eff_completeness / 100)
    return clamp_int(int(base * quality_factor), 1, 92)


def confidence_from_score(score: float, completeness: int, recommendation: str) -> int:
    if recommendation not in ("上盘", "下盘"):
        recommendation = recommendation_from_score(score)
    return clamp_int(int((42 + abs(score) * 53) * completeness / 100), 1, 95)


def unavailable_signal(name: str, weight: float, reason: str) -> Signal:
    return Signal(name=name, score=0.0, weight=weight, available=False, reason=reason)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def clamp_int(value: int, lower: int, upper: int) -> int:
    return max(lower, min(upper, value))


def upcoming_matches(client: SpdexClient, now: datetime | None = None) -> list[Match]:
    now = now or datetime.now(timezone.utc)
    return [match for match in client.world_cup_matches() if match.match_time >= now]


def suggested_pull_times(match_time: datetime, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    windows = [
        ("T-24h 建立基线", match_time - timedelta(hours=24)),
        ("T-8h 观察盘口", match_time - timedelta(hours=8)),
        ("T-4h 追踪热度/盘口修正", match_time - timedelta(hours=4)),
        ("T-2h 追踪临场资金变化", match_time - timedelta(hours=2)),
        ("T-60m 首次正式推荐", match_time - timedelta(minutes=60)),
        ("T-30m 复核", match_time - timedelta(minutes=30)),
    ]
    future = [f"{label}: {format_local(dt)}" for label, dt in windows if dt >= now]
    return future or ["当前已进入临场窗口，建议立即拉取并结合 T-30m 快照复核"]


def format_local(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def print_upcoming(matches: list[Match], limit: int) -> None:
    if not matches:
        print("没有找到未开赛的世界杯比赛。")
        return
    for match in matches[:limit]:
        pull_times = "；".join(suggested_pull_times(match.match_time)[:3])
        print(
            f"{match.event_id} | {format_local(match.match_time)} | "
            f"{match.home} vs {match.away} | 亚盘 {match.asian_line} | {pull_times}"
        )


def print_analysis(result: AnalysisResult, verbose: bool = False) -> None:
    match = result.match
    print(
        f"{match.event_id} | {format_local(match.match_time)} | {match.home} vs {match.away} | "
        f"盘口 {match.asian_line} | 推荐 {purchase_display_text(result)} | "
        f"置信度 {result.confidence}% | 完整度 {result.completeness}% | score {result.score:+.3f}"
    )
    print(f"  [DECISION] {result.decision_reason}")
    if result.review2:
        print(f"  [ROI] {review2_display_text(result.review2)}")
    for signal in result.signals:
        if verbose or signal.available:
            mark = "OK" if signal.available else "NA"
            summary = signal_summary_text(signal, result.upper_team, result.lower_team)
            print(f"  [{mark}] {signal.name}: {signal.score:+.3f} - {signal.reason}；{summary}")
    for warning in result.warnings:
        print(f"  [WARN] {warning}")


def purchase_display_text(result: AnalysisResult) -> str:
    return f"{result.purchase_side}({result.strength}:{result.purchase_team})"


def review2_display_text(review2: dict[str, Any]) -> str:
    side = None
    sides = review2.get("sides") if isinstance(review2.get("sides"), dict) else {}
    rec_side = review2.get("recommendation_side")
    if isinstance(sides, dict):
        side = sides.get(rec_side)
    side = side if isinstance(side, dict) else {}
    euro_roi = side.get("euro_roi")
    model_roi = side.get("model_roi")
    euro_kelly = side.get("euro_kelly")
    model_kelly = side.get("model_kelly")

    def pct(value: Any) -> str:
        try:
            return f"{float(value):+.1%}"
        except (TypeError, ValueError):
            return "N/A"

    return (
        f"R2 {review2.get('decision') or '-'} {review2.get('recommendation_label') or '-'} | "
        f"ROI 欧 {pct(euro_roi)} / 模 {pct(model_roi)} | "
        f"Kelly 欧 {pct(euro_kelly)} / 模 {pct(model_kelly)} | "
        f"{review2.get('risk') or '-'}"
    )


def print_snapshot_saved(path: Path, result: AnalysisResult) -> None:
    print(
        f"已保存快照: {path} | {result.match.event_id} | "
        f"{result.match.home} vs {result.match.away} | score {result.score:+.3f}"
    )


def print_snapshot_trend(store: SnapshotStore, event_id: int) -> int:
    records = store.load_event(event_id)
    if not records:
        print(f"没有找到本地快照: {store.event_path(event_id)}")
        return 1
    if len(records) < 2:
        first_time = records[0].get("fetched_at", "unknown")
        print(f"本地只有 1 条快照({first_time})，至少需要 2 条才能判断趋势。")
        return 1

    first = records[0]
    last = records[-1]
    first_metrics = snapshot_metrics(first)
    last_metrics = snapshot_metrics(last)
    match_info = last.get("match", {})
    result_info = last.get("result", {})
    home = match_info.get("home", "")
    away = match_info.get("away", "")
    score_delta = last_metrics["score"] - first_metrics["score"]
    heat_delta = last_metrics["heat_edge"] - first_metrics["heat_edge"]
    amount_delta = last_metrics["amount_edge"] - first_metrics["amount_edge"]
    payout_delta = last_metrics["payout_edge"] - first_metrics["payout_edge"]
    upper_water_delta = last_metrics["upper_water"] - first_metrics["upper_water"]
    line_depth_delta = last_metrics["line_depth"] - first_metrics["line_depth"]

    print(f"{event_id} | {home} vs {away} | 本地快照 {len(records)} 条")
    print(f"  时间: {first.get('fetched_at')} -> {last.get('fetched_at')}")
    print(
        f"  score: {first_metrics['score']:+.3f} -> {last_metrics['score']:+.3f} "
        f"({score_delta:+.3f}) | 当前推荐 {result_info.get('recommendation', '未知')} "
        f"| 强度 {result_info.get('strength', '未知')}"
    )
    print(
        f"  必发热度: {first_metrics['heat_edge']:+.3f} -> {last_metrics['heat_edge']:+.3f} "
        f"({heat_delta:+.3f})；成交倾斜 {first_metrics['amount_edge']:+.3f} -> "
        f"{last_metrics['amount_edge']:+.3f} ({amount_delta:+.3f})"
    )
    print(
        f"  盈亏压力: {first_metrics['payout_edge']:+.3f} -> {last_metrics['payout_edge']:+.3f} "
        f"({payout_delta:+.3f})；上盘水位 {first_metrics['upper_water']:.3g} -> "
        f"{last_metrics['upper_water']:.3g} ({upper_water_delta:+.3f})"
    )
    print(
        f"  盘口深度: {first_metrics['line_depth']:.2f} -> {last_metrics['line_depth']:.2f} "
        f"({line_depth_delta:+.2f})；欧赔确认 {first_metrics['euro_edge']:+.3f} -> "
        f"{last_metrics['euro_edge']:+.3f}"
    )
    signal_history_score, signal_history_reason = score_snapshot_signal_history(records)
    print(f"  历史基础信号: {signal_history_score:+.3f} - {signal_history_reason}")
    print(f"  结论: {snapshot_trend_summary(score_delta, heat_delta, upper_water_delta, line_depth_delta)}")
    return 0


def schedule_task_key(match: Match, label: str) -> str:
    return f"{match.event_id}:{match.match_time.isoformat()}:{label}"


def build_scheduled_tasks(
    matches: list[Match],
    now: datetime,
    horizon: timedelta,
    completed: set[str] | None = None,
    catch_up: bool = True,
) -> list[ScheduledTask]:
    completed = completed or set()
    horizon_end = now + horizon
    tasks: list[ScheduledTask] = []
    for match in matches:
        if match.match_time < now or match.match_time > horizon_end:
            continue

        missed: list[ScheduledTask] = []
        for label, offset, do_predict in SCHEDULE_WINDOWS:
            key = schedule_task_key(match, label)
            if key in completed:
                continue
            run_at = match.match_time - offset
            task = ScheduledTask(key, label, run_at, match, do_predict)
            if run_at >= now:
                tasks.append(task)
            else:
                missed.append(task)

        if catch_up and missed:
            latest = max(missed, key=lambda item: item.run_at)
            tasks.append(
                ScheduledTask(
                    latest.key,
                    f"{latest.label} 补采样",
                    now,
                    latest.match,
                    latest.do_predict,
                    is_catch_up=True,
                )
            )
    return sorted(tasks, key=lambda item: (item.run_at, item.match.match_time, item.match.event_id))


def run_watch(
    client: SpdexClient,
    store: SnapshotStore,
    horizon_hours: float,
    poll_seconds: int,
    limit: int,
    once: bool,
    catch_up: bool,
    verbose: bool,
) -> int:
    predictor = Predictor(client, store)
    print(
        f"启动自动快照: 未来 {horizon_hours:g} 小时，检查间隔 {poll_seconds}s，"
        f"状态文件 {store.scheduler_state_path()}"
    )
    maybe_print_auth_hint(client)
    while True:
        now = datetime.now(timezone.utc)
        try:
            matches = upcoming_matches(client, now)[:limit]
        except DataError as exc:
            print(f"[{format_local(now)}] 数据获取失败: {exc}", file=sys.stderr)
            if once:
                return 2
            time.sleep(max(10, poll_seconds))
            continue

        state = store.load_scheduler_state()
        completed = set((state.get("completed") or {}).keys())
        tasks = build_scheduled_tasks(
            matches,
            now=now,
            horizon=timedelta(hours=horizon_hours),
            completed=completed,
            catch_up=catch_up,
        )
        due = [task for task in tasks if task.run_at <= now and task.key not in completed]
        if due:
            for task in due:
                run_scheduled_task(task, client, predictor, store, verbose)
                maybe_print_ssl_warning(client)
        elif once:
            print_watch_summary(tasks, completed)
            return 0
        else:
            print_watch_summary(tasks, completed)

        if once:
            return 0
        next_task = next((task for task in tasks if task.key not in completed and task.run_at > now), None)
        sleep_seconds = poll_seconds
        if next_task:
            seconds_to_next = max(1, int((next_task.run_at - datetime.now(timezone.utc)).total_seconds()))
            sleep_seconds = min(poll_seconds, seconds_to_next)
        time.sleep(max(1, sleep_seconds))


def run_scheduled_task(
    task: ScheduledTask,
    client: SpdexClient,
    predictor: Predictor,
    store: SnapshotStore,
    verbose: bool,
) -> None:
    now = datetime.now(timezone.utc)
    print(
        f"[{format_local(now)}] 执行 {task.label}: {task.match.event_id} | "
        f"{task.match.home} vs {task.match.away} | 开赛 {format_local(task.match.match_time)}"
    )
    match = task.match
    try:
        match = client.find_match(task.match.event_id)
    except DataError as exc:
        print(f"  [WARN] 无法刷新比赛详情，使用赛程列表数据: {exc}")
    result = predictor.analyze(match)
    path = store.save(result, fetched_at=now)
    print_snapshot_saved(path, result)
    if task.do_predict:
        print_analysis(result, verbose=verbose)
    store.mark_task_completed(task, completed_at=now)


def print_watch_summary(tasks: list[ScheduledTask], completed: set[str]) -> None:
    pending = [task for task in tasks if task.key not in completed]
    if not pending:
        print("未来窗口内没有待执行的自动快照任务。")
        return
    next_task = pending[0]
    print(
        f"下次任务: {format_local(next_task.run_at)} | {next_task.label} | "
        f"{next_task.match.event_id} {next_task.match.home} vs {next_task.match.away}"
    )


def snapshot_metrics(record: dict[str, Any]) -> dict[str, float]:
    match_info = record.get("match", {})
    result_info = record.get("result", {})
    if not isinstance(match_info, dict):
        match_info = {}
    if not isinstance(result_info, dict):
        result_info = {}
    raw = match_info.get("raw", {})
    if not isinstance(raw, dict):
        raw = {}

    upper_key = snapshot_side_key(match_info, str(result_info.get("upper_team", "")))
    lower_key = snapshot_side_key(match_info, str(result_info.get("lower_team", "")))
    upper_index = float_or_zero(raw.get(f"BfIndex{upper_key}"))
    lower_index = float_or_zero(raw.get(f"BfIndex{lower_key}"))
    upper_amount = float_or_zero(raw.get(f"BfAmount{upper_key}"))
    lower_amount = float_or_zero(raw.get(f"BfAmount{lower_key}"))
    upper_payout = float_or_zero(raw.get(f"BfPayout{upper_key}"))
    lower_payout = float_or_zero(raw.get(f"BfPayout{lower_key}"))
    draw_payout = float_or_zero(raw.get("BfPayoutDraw"))
    upper_water = float_or_zero(raw.get(f"AsianAvr{upper_key}"))
    lower_water = float_or_zero(raw.get(f"AsianAvr{lower_key}"))
    upper_euro = float_or_zero(raw.get(f"EuroAvr{upper_key}"))
    lower_euro = float_or_zero(raw.get(f"EuroAvr{lower_key}"))
    amount_total = upper_amount + lower_amount
    amount_edge = 0.0 if amount_total <= 0 else clamp((upper_amount - lower_amount) / amount_total, -1, 1)
    index_edge = clamp((upper_index - lower_index) / 100.0, -1, 1)
    payout_total = abs(upper_payout) + abs(lower_payout) + abs(draw_payout)
    payout_edge = 0.0 if payout_total <= 0 else clamp((lower_payout - upper_payout) / payout_total * 1.20, -1, 1)
    bifa_available = 1.0 if (upper_index or lower_index or upper_amount or lower_amount) else 0.0
    return {
        "score": float_or_zero(result_info.get("score")),
        "bifa_available": bifa_available,
        "heat_edge": clamp(0.55 * index_edge + 0.45 * amount_edge, -1, 1),
        "amount_edge": amount_edge,
        "payout_edge": payout_edge,
        "upper_water": upper_water,
        "lower_water": lower_water,
        "line_depth": abs(line_value(str(match_info.get("asian_line", "0")))),
        "euro_edge": score_bifa_odds_confirmation(upper_euro, lower_euro),
    }


def snapshot_side_key(match_info: dict[str, Any], team: str) -> str:
    if team == match_info.get("home"):
        return "Home"
    if team == match_info.get("away"):
        return "Away"
    return "Home"


def snapshot_trend_summary(
    score_delta: float,
    heat_delta: float,
    upper_water_delta: float,
    line_depth_delta: float,
) -> str:
    parts: list[str] = []
    if score_delta > 0.05:
        parts.append("综合分增强，趋势偏上盘")
    elif score_delta < -0.05:
        parts.append("综合分减弱，趋势偏下盘")
    else:
        parts.append("综合分变化不大")

    if heat_delta > 0.08 and upper_water_delta > 0.04:
        parts.append("热度增加但上盘水位变贵，警惕热门买入成本被抬高")
    elif heat_delta > 0.08 and upper_water_delta < -0.04:
        parts.append("热度增加且上盘水位被压低，属于顺势确认")
    elif heat_delta < -0.08:
        parts.append("上盘热度回落")

    if line_depth_delta > 0.24:
        parts.append("盘口升深，对上盘打穿能力要求提高")
    elif line_depth_delta < -0.24:
        parts.append("盘口降浅，上盘门槛降低但也可能代表信心下降")
    return "；".join(parts)


SNAPSHOT_SIGNAL_IMPORTANCE = {
    "必发成交走势": 0.24,
    "欧赔/Kelly": 0.22,
    "亚盘水位": 0.18,
    "盘口合理性": 0.12,
    "平局风险": 0.10,
    "盘口深度/打穿能力": 0.10,
    "公司一致性": 0.04,
}


def snapshot_signal_map(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result_info = record.get("result", {})
    if not isinstance(result_info, dict):
        return {}
    signals = result_info.get("signals", [])
    if not isinstance(signals, list):
        return {}
    mapped: dict[str, dict[str, Any]] = {}
    for signal in signals:
        if not isinstance(signal, dict):
            continue
        name = str(signal.get("name", ""))
        if not name:
            continue
        mapped[name] = signal
    return mapped


def snapshot_signal_score(record: dict[str, Any], name: str) -> tuple[bool, float]:
    signal = snapshot_signal_map(record).get(name)
    if not signal or not signal.get("available"):
        return False, 0.0
    return True, float_or_zero(signal.get("score"))


def score_snapshot_trade_history(records: list[dict[str, Any]]) -> tuple[float | None, str]:
    signal_score, signal_reason = score_snapshot_trade_signal_series(records)
    raw_score, raw_reason = score_snapshot_trade_raw_series(records)

    components: list[tuple[float, float]] = []
    reasons: list[str] = []
    if signal_score is not None:
        components.append((0.65, signal_score))
        reasons.append(signal_reason)
    if raw_score is not None:
        components.append((0.35, raw_score))
        reasons.append(raw_reason)
    if not components:
        return None, "历史快照缺少可用成交走势"

    weight_total = sum(weight for weight, _score in components)
    score = clamp(sum(weight * value for weight, value in components) / weight_total, -1, 1)
    return score, "；".join(reasons)


def score_snapshot_trade_signal_series(records: list[dict[str, Any]]) -> tuple[float | None, str]:
    series: list[float] = []
    skipped_substitute = 0
    for record in records:
        signal = snapshot_signal_map(record).get("必发成交走势")
        if not signal or not signal.get("available"):
            continue
        reason = str(signal.get("reason", ""))
        if "历史快照兜底" in reason:
            skipped_substitute += 1
            continue
        series.append(float_or_zero(signal.get("score")))

    if not series:
        return None, "历史真实成交信号不足"
    if len(series) == 1:
        evidence = clamp(0.55 * series[0], -1, 1)
        reason = f"真实成交信号历史仅1点 {series[0]:+.2f}，降权证据{evidence:+.2f}"
        if skipped_substitute:
            reason += f"，忽略历史替代点{skipped_substitute}个"
        return evidence, reason

    total_delta = series[-1] - series[0]
    slope = linear_series_slope(series)
    historical_avg = sum(series) / len(series)
    last_vs_avg = series[-1] - historical_avg
    score = clamp(
        0.45 * clamp(series[-1], -1, 1)
        + 0.25 * clamp(total_delta / 0.55, -1, 1)
        + 0.20 * clamp(slope / 0.20, -1, 1)
        + 0.10 * clamp(last_vs_avg / 0.35, -1, 1),
        -1,
        1,
    )
    reason = (
        f"真实成交信号历史 {series[0]:+.2f}->{series[-1]:+.2f} "
        f"均值{historical_avg:+.2f} 趋势{score:+.2f}"
    )
    if skipped_substitute:
        reason += f"，忽略历史替代点{skipped_substitute}个"
    return score, reason


def score_snapshot_trade_raw_series(records: list[dict[str, Any]]) -> tuple[float | None, str]:
    points = [point for point in (snapshot_trade_raw_point(record) for record in records) if point is not None]
    if len(points) < 2:
        return None, "历史基础成交字段不足"

    first = points[0]
    previous = points[-2]
    last = points[-1]
    total_flow = snapshot_amount_flow_edge(first, last)
    recent_flow = snapshot_amount_flow_edge(previous, last)
    total_price = snapshot_bifa_price_edge(first, last)
    recent_price = snapshot_bifa_price_edge(previous, last)
    total_score = clamp(0.68 * total_flow + 0.32 * total_price, -1, 1)
    recent_score = clamp(0.68 * recent_flow + 0.32 * recent_price, -1, 1)
    score = clamp(0.60 * recent_score + 0.40 * total_score, -1, 1)

    total_upper_delta = max(0.0, last["upper_amount"] - first["upper_amount"])
    total_lower_delta = max(0.0, last["lower_amount"] - first["lower_amount"])
    recent_upper_delta = max(0.0, last["upper_amount"] - previous["upper_amount"])
    recent_lower_delta = max(0.0, last["lower_amount"] - previous["lower_amount"])
    reason = (
        f"基础成交/赔率 {len(points)}点，近段新增成交上盘 {recent_upper_delta:,.0f} / "
        f"下盘 {recent_lower_delta:,.0f}，全程新增 {total_upper_delta:,.0f} / {total_lower_delta:,.0f}，"
        f"必发赔率上盘 {first['upper_odds']:.2f}->{last['upper_odds']:.2f}，"
        f"下盘 {first['lower_odds']:.2f}->{last['lower_odds']:.2f}"
    )
    return score, reason


def snapshot_trade_raw_point(record: dict[str, Any]) -> dict[str, float] | None:
    match_info = record.get("match", {})
    result_info = record.get("result", {})
    if not isinstance(match_info, dict) or not isinstance(result_info, dict):
        return None
    raw = match_info.get("raw", {})
    if not isinstance(raw, dict):
        return None

    upper_key = snapshot_side_key(match_info, str(result_info.get("upper_team", "")))
    lower_key = snapshot_side_key(match_info, str(result_info.get("lower_team", "")))
    upper_amount = float_or_zero(raw.get(f"BfAmount{upper_key}"))
    lower_amount = float_or_zero(raw.get(f"BfAmount{lower_key}"))
    upper_odds = float_or_zero(raw.get(f"BfOdds{upper_key}"))
    lower_odds = float_or_zero(raw.get(f"BfOdds{lower_key}"))
    if upper_amount <= 0 and lower_amount <= 0 and upper_odds <= 0 and lower_odds <= 0:
        return None
    return {
        "upper_amount": upper_amount,
        "lower_amount": lower_amount,
        "upper_odds": upper_odds,
        "lower_odds": lower_odds,
    }


def snapshot_amount_flow_edge(first: dict[str, float], last: dict[str, float]) -> float:
    upper_delta = max(0.0, last["upper_amount"] - first["upper_amount"])
    lower_delta = max(0.0, last["lower_amount"] - first["lower_amount"])
    total = upper_delta + lower_delta
    if total <= 0:
        return 0.0
    return clamp((upper_delta - lower_delta) / total, -1, 1)


def snapshot_bifa_price_edge(first: dict[str, float], last: dict[str, float]) -> float:
    upper_first = first["upper_odds"]
    upper_last = last["upper_odds"]
    lower_first = first["lower_odds"]
    lower_last = last["lower_odds"]
    upper_edge = 0.0
    lower_edge = 0.0
    if upper_first > 0 and upper_last > 0:
        upper_edge = clamp((upper_first - upper_last) / max(upper_first * 0.08, 0.08), -1, 1)
    if lower_first > 0 and lower_last > 0:
        lower_edge = clamp((lower_first - lower_last) / max(lower_first * 0.08, 0.08), -1, 1)
    return clamp((upper_edge - lower_edge) / 2.0, -1, 1)


def score_snapshot_signal_history(records: list[dict[str, Any]]) -> tuple[float, str]:
    components: list[float] = []
    reasons: list[str] = []
    for name, weight in SNAPSHOT_SIGNAL_IMPORTANCE.items():
        series: list[float] = []
        for record in records:
            available, score = snapshot_signal_score(record, name)
            if available:
                series.append(score)
        if len(series) < 2:
            if len(series) == 1:
                historical_level = clamp(series[0], -1, 1)
                evidence = 0.45 * historical_level
                components.append(weight * evidence)
                if abs(evidence) >= 0.08:
                    reasons.append(f"{name}历史仅1点 {series[0]:+.2f} 证据{evidence:+.2f}")
            continue
        total_delta = series[-1] - series[0]
        slope = linear_series_slope(series)
        historical_avg = sum(series) / len(series)
        last_vs_avg = series[-1] - historical_avg
        historical_level = clamp(historical_avg, -1, 1)
        trend = clamp(
            0.42 * clamp(total_delta / 0.45, -1, 1)
            + 0.33 * clamp(slope / 0.18, -1, 1)
            + 0.15 * clamp(last_vs_avg / 0.30, -1, 1)
            + 0.10 * historical_level,
            -1,
            1,
        )
        components.append(weight * trend)
        if abs(trend) >= 0.08:
            reasons.append(
                f"{name}历史 {series[0]:+.2f}->{series[-1]:+.2f} "
                f"均值{historical_avg:+.2f} 趋势{trend:+.2f}"
            )
    if not components:
        return 0.0, "历史基础信号趋势不足"
    score = clamp(sum(components) / sum(SNAPSHOT_SIGNAL_IMPORTANCE.values()), -1, 1)
    return score, "全历史基础信号" + ("；".join(reasons) if reasons else "变化不大")


def linear_series_slope(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    x_avg = (len(values) - 1) / 2.0
    y_avg = sum(values) / len(values)
    numerator = sum((index - x_avg) * (value - y_avg) for index, value in enumerate(values))
    denominator = sum((index - x_avg) ** 2 for index in range(len(values)))
    if denominator <= 0:
        return 0.0
    return numerator / denominator


def float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def optional_float(*values: Any) -> float | None:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


PUBLIC_SOURCES = [
    {
        "name": "SPDEX",
        "url": "https://app.spdex.com/pstand/",
        "role": "默认主源：世界杯赛程、必发指数、成交量、盈亏指数、亚盘、欧赔/Kelly",
        "auth": "公开 Web 接口；可选 .env 中 SPDEX_COOKIE / SPDEX_AUTHORIZATION 附加到 app.spdex.com 请求（见 auth-probe）",
    },
    {
        "name": "楚旗 live-bifa",
        "url": "https://www.chuqi.com/data_channel/bifa/",
        "role": "默认补充源：匿名页面解析必发指数、成交额、盈亏、必发赔率和成交曲线；可在 SPDEX tradeflow 不可用时补趋势",
        "auth": "公开网页，无 API Key；只作为必发补充，不提供完整亚盘公司水位",
    },
    {
        "name": "The Odds API",
        "url": "https://the-odds-api.com/liveapi/guides/v4/",
        "role": "赔率校验：h2h、spreads、totals，可交叉验证让球方向",
        "auth": "API Key，有额度限制",
    },
    {
        "name": "football-data.org",
        "url": "https://www.football-data.org/documentation/quickstart",
        "role": "赛程/赛果/球队元数据；不是必发或亚盘源",
        "auth": "API Token，免费层有限制",
    },
    {
        "name": "football-data.co.uk",
        "url": "https://www.football-data.co.uk/data.php",
        "role": "历史赛果和赔率 CSV，适合长期回测",
        "auth": "公开下载",
    },
    {
        "name": "TheSportsDB",
        "url": "https://www.thesportsdb.com/free_sports_api",
        "role": "基础体育元数据、球队、图片；不作为盘口主源",
        "auth": "免费 API Key / 测试 Key",
    },
    {
        "name": "API-Football",
        "url": "https://www.api-football.com/documentation-v3",
        "role": "赛程、阵容、伤停、排名、赔率等，可补充基本面和赔率交叉验证",
        "auth": "API Key，免费层和付费层额度不同",
    },
    {
        "name": "SportMonks Football API",
        "url": "https://docs.sportmonks.com/football",
        "role": "赛程、球队、阵容、伤停、赔率和统计，适合做高质量基本面适配器",
        "auth": "API Token，通常需要付费方案",
    },
    {
        "name": "World Football Elo Ratings",
        "url": "https://www.eloratings.net/",
        "role": "国家队 Elo 实力差，可生成 ExternalPowerEdge / ModelFairLineDepth",
        "auth": "公开网站；需注意抓取频率和使用条款",
    },
    {
        "name": "OpenFootball",
        "url": "https://openfootball.github.io/",
        "role": "公共领域历史赛程/赛果，适合测试样例",
        "auth": "公开数据",
    },
    {
        "name": "Betfair Exchange API",
        "url": "https://docs.developer.betfair.com/",
        "role": "最高质量官方交易所数据，可替代/校验必发指数",
        "auth": "Betfair 账户、App Key、认证和权限",
    },
]


def print_sources() -> None:
    for source in PUBLIC_SOURCES:
        print(f"{source['name']} | {source['role']}")
        print(f"  URL: {source['url']}")
        print(f"  鉴权: {source['auth']}")


def _match_detail_item_event_id(item: dict[str, Any]) -> int | None:
    try:
        if isinstance(item.get("Match"), dict):
            return int(item["Match"]["EventId"])
        return int(item["EventId"])
    except (KeyError, TypeError, ValueError):
        return None


def pick_match_detail_item(candidates: list[dict[str, Any]], event_id: int) -> dict[str, Any] | None:
    for item in candidates:
        if _match_detail_item_event_id(item) == event_id:
            return item
    for item in candidates:
        try:
            if parse_match(item).event_id == event_id:
                return item
        except (KeyError, TypeError, ValueError):
            continue
    return candidates[0] if candidates else None


def summarize_match_detail_item(item: dict[str, Any] | None) -> str:
    if item is None:
        return "(无可用条目)"
    lines = [f"IsStopUpdate={bool(item.get('IsStopUpdate', False))}", f"顶层字段数={len(item)}"]
    bi = item.get("BaseInfo")
    if isinstance(bi, dict):
        lines.append(f"BaseInfo 字段数={len(bi)}")
        for k in ("BfIndexHome", "BfIndexAway", "BfAmountHome", "BfAmountAway", "AsianAvrHome", "AsianAvrAway"):
            if k in bi:
                lines.append(f"  {k}={bi.get(k)!r}")
    return "\n".join(lines)


def validate_spdex_auth(client: SpdexClient, event_id: int | None = None) -> str:
    if event_id is not None:
        items = client._match_detail_candidates(str(event_id))
        if not items:
            raise DataError(f"SPDEX match_detail returned no rows for event_id={event_id}")
        return f"match_detail JSON OK，event_id={event_id}，条目数={len(items)}"
    matches = client.world_cup_matches()
    return f"match_list JSON OK，世界杯比赛数={len(matches)}"


def run_auth_cookie(args: argparse.Namespace, env_path: Path) -> int:
    if args.cookie_stdin:
        raw_cookie = sys.stdin.read()
    elif args.cookie:
        raw_cookie = args.cookie
    else:
        raw_cookie = os.environ.get("SPDEX_COOKIE", "")

    cookie = normalize_cookie_value(raw_cookie)
    if not cookie:
        print(
            "没有可验证的 Cookie。请使用 --cookie-stdin 从标准输入读取，或使用 --cookie 传入 Cookie。",
            file=sys.stderr,
        )
        return 2

    client = SpdexClient(
        timeout=args.timeout,
        ssl_fallback=not args.no_ssl_fallback,
        retries=args.retries,
        curl_fallback=not args.no_curl_fallback,
        use_env_auth=False,
        extra_headers={"Cookie": cookie},
        chuqi_bifa=False,
    )
    try:
        message = validate_spdex_auth(client, args.event_id)
    except DataError as exc:
        print(f"Cookie 验证失败，未更新 {env_path}: {exc}", file=sys.stderr)
        maybe_print_ssl_warning(client)
        return 2

    if not args.check:
        set_env_file_value(env_path, "SPDEX_COOKIE", cookie)
        os.environ["SPDEX_COOKIE"] = cookie
        print(f"Cookie 验证通过并已更新 {env_path}: {mask_secret(cookie)}")
    else:
        print(f"Cookie 验证通过: {mask_secret(cookie)}")
    print(message)
    maybe_print_ssl_warning(client)
    return 0


def run_auth_probe(event_id: int) -> int:
    """Compare /spdex/match_detail payloads with and without .env auth headers."""
    plain = SpdexClient(use_env_auth=False, chuqi_bifa=False)
    authed = SpdexClient(use_env_auth=True, chuqi_bifa=False)
    keyword = str(event_id)

    def fetch_items(client: SpdexClient) -> tuple[list[dict[str, Any]], DataError | None]:
        try:
            return client._match_detail_candidates(keyword), None
        except DataError as exc:
            return [], exc

    plain_items, plain_error = fetch_items(plain)
    auth_items, auth_error = fetch_items(authed)
    print(f"事件 {event_id} | match_detail 无鉴权条目数={len(plain_items)} | 有鉴权条目数={len(auth_items)}")
    if plain_error:
        print(f"无鉴权请求失败: {plain_error}", file=sys.stderr)
    if auth_error:
        print(f"有鉴权请求失败: {auth_error}", file=sys.stderr)
    if plain_error and auth_error:
        maybe_print_ssl_warning(plain)
        maybe_print_ssl_warning(authed)
        return 2
    try:
        p_item = pick_match_detail_item(plain_items, event_id)
        a_item = pick_match_detail_item(auth_items, event_id)
    except DataError as exc:
        print(f"请求失败: {exc}", file=sys.stderr)
        return 2
    if not authed.auth_configured:
        print(
            "当前未配置 SPDEX_COOKIE / SPDEX_AUTHORIZATION（或已使用 --no-dotenv），"
            "下面两栏应一致；配置后若仍一致，说明 app.spdex.com 该接口不区分会员会话。",
            file=sys.stderr,
        )
    print("\n--- 无鉴权 ---")
    print(summarize_match_detail_item(p_item))
    print("\n--- 有鉴权（.env 中的 Cookie / Authorization）---")
    print(summarize_match_detail_item(a_item))
    if p_item and a_item:
        pk = set(p_item.keys())
        ak = set(a_item.keys())
        only_p = sorted(pk - ak)
        only_a = sorted(ak - pk)
        if only_p or only_a:
            print("\n顶层字段差集:")
            if only_p:
                print("  仅无鉴权:", only_p)
            if only_a:
                print("  仅有鉴权:", only_a)
        p_bi = p_item.get("BaseInfo") if isinstance(p_item.get("BaseInfo"), dict) else {}
        a_bi = a_item.get("BaseInfo") if isinstance(a_item.get("BaseInfo"), dict) else {}
        if isinstance(p_bi, dict) and isinstance(a_bi, dict):
            bpk = set(p_bi.keys())
            bak = set(a_bi.keys())
            obp = sorted(bpk - bak)
            oba = sorted(bak - bpk)
            if obp or oba:
                print("\nBaseInfo 字段差集:")
                if obp:
                    print("  仅无鉴权:", obp)
                if oba:
                    print("  仅有鉴权:", oba)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "根据 SPDEX 必发指数、成交走势、亚盘水位、欧赔/Kelly 和市场平衡背离，"
            "预测世界杯亚盘购买方向。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "常用示例:\n"
            "  python3 worldcup_ah_cli.py upcoming --limit 5\n"
            "      列出最近 5 场未开赛世界杯比赛和建议拉取时间。\n\n"
            "  python3 worldcup_ah_cli.py predict --event-id 35035283 --verbose\n"
            "      分析单场比赛，显示推荐、倾向、置信度和各信号理由。\n\n"
            "  python3 worldcup_ah_cli.py predict --event-id 35035283 --chuqi-id 13870086 --verbose\n"
            "      手动指定楚旗 live-bifa 详情 ID，用其补充必发成交曲线（会校验队名）。\n\n"
            "  python3 worldcup_ah_cli.py predict --all --limit 10\n"
            "      批量分析未开赛世界杯比赛。\n\n"
            "  python3 worldcup_ah_cli.py predict --event-id 35035283 --json\n"
            "      输出 JSON，方便后续保存或回测。\n\n"
            "  python3 worldcup_ah_cli.py snapshot --event-id 35035283\n"
            "      抓取单场当前数据并追加到本地 .spdex_snapshots/，用于趋势判断。\n\n"
            "  python3 worldcup_ah_cli.py trend --event-id 35035283\n"
            "      根据本地快照比较 score、必发热度、盈亏压力、盘口/水位变化。\n\n"
            "  python3 worldcup_ah_cli.py watch --limit 20\n"
            "      常驻运行，按 T-24h/T-8h/T-4h/T-2h/T-60m/T-30m 自动快照并输出预测。\n\n"
            "  python3 worldcup_ah_cli.py auth-probe --event-id 35035286\n"
            "      对比无鉴权与 .env 鉴权下 match_detail 返回字段是否不同（会员 Cookie 是否作用于 app.spdex.com）。\n\n"
            "  python3 worldcup_ah_cli.py auth-cookie --cookie-stdin --event-id 35035286\n"
            "      从标准输入读取浏览器 Cookie，验证通过后写入 .env 的 SPDEX_COOKIE。\n\n"
            "  python3 worldcup_ah_cli.py sources\n"
            "      查看后续可接入的公开数据源。\n\n"
            "输出说明:\n"
            "  默认会用楚旗 live-bifa 页面补充必发指数/成交走势；如需只看 SPDEX，请加 --no-chuqi。\n"
            "  推荐 上盘/下盘(轻微|中等|强烈:球队): 直接按模型综合分给出亚盘方向和强度。\n"
            "  score >= 0 偏上盘，score < 0 偏下盘；0.12/0.25 用于区分中等和强烈。\n"
            "  算法会综合盘口深度、健康/危险大热、平局风险、盘口合理性、公司一致性、\n"
            "  深盘打穿能力、赢盘门槛风险、高低水价值和本地快照趋势；\n"
            "  高水只有在模型概率高于市场隐含概率时才加分，低水偏贵且缺少溢价会扣分。\n"
            "  IsStopUpdate 场次只用于复盘提示，不再把模型置信度强制设为 0。"
        ),
    )
    parser.add_argument("--timeout", type=float, default=8.0, help="HTTP 超时时间，单位秒，默认 8")
    parser.add_argument("--retries", type=int, default=1, help="SPDEX 请求重试次数，默认 1")
    parser.add_argument(
        "--no-ssl-fallback",
        action="store_true",
        help="SPDEX 证书校验失败时不自动降级为不校验证书",
    )
    parser.add_argument(
        "--no-curl-fallback",
        action="store_true",
        help="urllib 请求失败时不使用系统 curl 兜底",
    )
    parser.add_argument(
        "--env-file",
        type=str,
        default=None,
        metavar="PATH",
        help="从该文件加载 KEY=value（不覆盖 shell 已有同名变量）；默认: 与脚本同目录的 .env；不存在则跳过",
    )
    parser.add_argument(
        "--no-dotenv",
        action="store_true",
        help="不加载 .env",
    )
    parser.add_argument(
        "--snapshot-dir",
        default=SNAPSHOT_DIR_NAME,
        help=f"本地快照目录，默认 {SNAPSHOT_DIR_NAME}；建议保持在 .gitignore 中",
    )
    parser.add_argument(
        "--no-chuqi",
        action="store_true",
        help="禁用楚旗 live-bifa 必发指数/成交走势补充源",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    upcoming_parser = subparsers.add_parser(
        "upcoming",
        help="列出未开赛世界杯赛程",
        description="列出 SPDEX 当前可见的未开赛世界杯比赛，并给出 T-24h/T-8h/T-4h/T-2h/T-60m/T-30m 等建议拉取时间。",
    )
    upcoming_parser.add_argument("--limit", type=int, default=20, help="最多显示多少场")

    predict_parser = subparsers.add_parser(
        "predict",
        help="预测亚盘购买方向",
        description=(
            "分析单场或批量世界杯比赛。输出推荐、倾向、置信度、数据完整度和各信号分数。"
        ),
    )
    group = predict_parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--event-id", type=int, help="SPDEX EventId")
    group.add_argument("--all", action="store_true", help="分析所有未开赛世界杯比赛")
    predict_parser.add_argument("--limit", type=int, default=20, help="--all 时最多分析多少场")
    predict_parser.add_argument("--json", action="store_true", help="输出 JSON")
    predict_parser.add_argument("--verbose", action="store_true", help="显示所有信号细节")
    predict_parser.add_argument(
        "--chuqi-id",
        type=int,
        default=None,
        help="单场预测时手动指定楚旗 live-bifa 详情 ID，补充必发指数和成交曲线",
    )

    snapshot_parser = subparsers.add_parser(
        "snapshot",
        help="保存本地赛前快照",
        description=(
            "抓取当前 SPDEX 数据、执行一次预测，并把原始字段和预测结果追加到本地 JSONL。"
            "默认目录 .spdex_snapshots/ 应保留在 .gitignore 中，不提交 Git。"
        ),
    )
    snapshot_group = snapshot_parser.add_mutually_exclusive_group(required=True)
    snapshot_group.add_argument("--event-id", type=int, help="SPDEX EventId")
    snapshot_group.add_argument("--all", action="store_true", help="保存所有未开赛世界杯比赛快照")
    snapshot_parser.add_argument("--limit", type=int, default=20, help="--all 时最多保存多少场")
    snapshot_parser.add_argument("--verbose", action="store_true", help="保存后同时显示详细分析")
    snapshot_parser.add_argument(
        "--chuqi-id",
        type=int,
        default=None,
        help="单场快照时手动指定楚旗 live-bifa 详情 ID，补充必发指数和成交曲线",
    )

    trend_parser = subparsers.add_parser(
        "trend",
        help="查看本地快照趋势",
        description="读取本地快照，比较 score、必发热度、成交倾斜、盈亏压力、盘口深度和上盘水位变化。",
    )
    trend_parser.add_argument("--event-id", type=int, required=True, help="SPDEX EventId")

    watch_parser = subparsers.add_parser(
        "watch",
        help="按赛前窗口自动保存快照/预测",
        description=(
            "常驻运行：每次检查未来窗口内的世界杯比赛，并按 T-24h/T-8h/T-4h/T-2h/T-60m/T-30m "
            "自动保存快照并打印预测；T-24h 到 T-2h 为预判，T-60m/T-30m 为正式复核/确认。"
        ),
    )
    watch_parser.add_argument("--limit", type=int, default=20, help="最多跟踪多少场未来比赛")
    watch_parser.add_argument("--horizon-hours", type=float, default=24.0, help="扫描未来多少小时，默认 24")
    watch_parser.add_argument("--poll-seconds", type=int, default=3600, help="检查间隔秒数，默认 3600")
    watch_parser.add_argument("--once", action="store_true", help="只扫描并执行当前到期任务一次，不常驻")
    watch_parser.add_argument(
        "--no-catch-up",
        action="store_true",
        help="启动时不补采样已经错过的最近一个窗口",
    )
    watch_parser.add_argument("--verbose", action="store_true", help="临场预测窗口显示详细信号")

    auth_probe_parser = subparsers.add_parser(
        "auth-probe",
        help="对比鉴权前后 SPDEX match_detail 数据",
        description=(
            "各请求一次 /spdex/match_detail（无鉴权 vs 使用 .env 中的 SPDEX_COOKIE / SPDEX_AUTHORIZATION），"
            "对比条目数、IsStopUpdate、BaseInfo 字段与若干必发/亚盘示例字段。"
            "若两者始终一致，说明 app.spdex.com 该公开接口可能不识别 new.spdex 登录 Cookie，或需其它请求头。"
        ),
    )
    auth_probe_parser.add_argument("--event-id", type=int, required=True, help="SPDEX EventId")

    auth_cookie_parser = subparsers.add_parser(
        "auth-cookie",
        help="更新或验证 .env 中的 SPDEX_COOKIE",
        description=(
            "验证浏览器 Cookie 是否能让 app.spdex.com 返回 JSON。"
            "验证通过后默认写入 .env；加 --check 时只验证不写入。"
        ),
    )
    auth_cookie_group = auth_cookie_parser.add_mutually_exclusive_group()
    auth_cookie_group.add_argument("--cookie", help="直接传入 Cookie 字符串或完整 Cookie: 请求头")
    auth_cookie_group.add_argument("--cookie-stdin", action="store_true", help="从标准输入读取 Cookie，避免进入 shell 历史")
    auth_cookie_parser.add_argument("--check", action="store_true", help="只验证当前 Cookie，不写入 .env")
    auth_cookie_parser.add_argument(
        "--event-id",
        type=int,
        default=None,
        help="用指定 event_id 的 match_detail 验证；不填则用 match_list 验证",
    )

    subparsers.add_parser(
        "sources",
        help="列出可后续接入的公开数据源",
        description="列出 SPDEX、The Odds API、football-data.org、Betfair Exchange API 等可选数据源。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    env_path = Path(args.env_file) if args.env_file else default_env_file_path()
    if not args.no_dotenv:
        load_dotenv_file(env_path)

    if args.command == "auth-cookie":
        return run_auth_cookie(args, env_path)

    warn_if_credentials_without_cookie()

    if args.command == "auth-probe":
        return run_auth_probe(args.event_id)

    client = SpdexClient(
        timeout=args.timeout,
        ssl_fallback=not args.no_ssl_fallback,
        retries=args.retries,
        curl_fallback=not args.no_curl_fallback,
        chuqi_bifa=not args.no_chuqi,
    )

    if args.command == "sources":
        print_sources()
        return 0

    if args.command == "upcoming":
        try:
            print_upcoming(upcoming_matches(client), args.limit)
        except DataError as exc:
            print(f"数据获取失败: {exc}", file=sys.stderr)
            return 2
        maybe_print_ssl_warning(client)
        maybe_print_auth_hint(client)
        return 0

    if args.command == "predict":
        store = SnapshotStore(args.snapshot_dir)
        predictor = Predictor(client, store)
        if args.all and args.chuqi_id is not None:
            print("--chuqi-id 只能和单场 --event-id 一起使用，不能用于 --all", file=sys.stderr)
            return 2
        try:
            if args.all:
                matches = upcoming_matches(client)[: args.limit]
            else:
                match = client.find_match(args.event_id)
                if args.chuqi_id is not None:
                    match = attach_chuqi_id(match, args.chuqi_id)
                matches = [match]
        except DataError as exc:
            print(f"数据获取失败: {exc}", file=sys.stderr)
            return 2

        results = [predictor.analyze(match) for match in matches]
        if args.json:
            print(json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2))
        else:
            for index, result in enumerate(results):
                if index:
                    print()
                print_analysis(result, verbose=args.verbose)
        maybe_print_ssl_warning(client)
        maybe_print_auth_hint(client)
        return 0

    if args.command == "snapshot":
        store = SnapshotStore(args.snapshot_dir)
        predictor = Predictor(client, store)
        if args.all and args.chuqi_id is not None:
            print("--chuqi-id 只能和单场 --event-id 一起使用，不能用于 --all", file=sys.stderr)
            return 2
        try:
            if args.all:
                matches = upcoming_matches(client)[: args.limit]
            else:
                match = client.find_match(args.event_id)
                if args.chuqi_id is not None:
                    match = attach_chuqi_id(match, args.chuqi_id)
                matches = [match]
        except DataError as exc:
            print(f"数据获取失败: {exc}", file=sys.stderr)
            return 2

        for index, match in enumerate(matches):
            if index:
                print()
            result = predictor.analyze(match)
            path = store.save(result)
            print_snapshot_saved(path, result)
            if args.verbose:
                print_analysis(result, verbose=True)
        maybe_print_ssl_warning(client)
        return 0

    if args.command == "trend":
        store = SnapshotStore(args.snapshot_dir)
        return print_snapshot_trend(store, args.event_id)

    if args.command == "watch":
        store = SnapshotStore(args.snapshot_dir)
        return run_watch(
            client=client,
            store=store,
            horizon_hours=args.horizon_hours,
            poll_seconds=args.poll_seconds,
            limit=args.limit,
            once=args.once,
            catch_up=not args.no_catch_up,
            verbose=args.verbose,
        )

    parser.print_help()
    return 1


def maybe_print_ssl_warning(client: SpdexClient) -> None:
    if client.ssl_fallback_used:
        print(
            "警告: SPDEX HTTPS 证书校验失败，本次请求已自动降级为不校验证书。"
            "如需强制校验证书，请加 --no-ssl-fallback。",
            file=sys.stderr,
        )
    if client.curl_fallback_used:
        print(
            "提示: 部分 SPDEX 请求已使用系统 curl 兜底完成。",
            file=sys.stderr,
        )


def maybe_print_auth_hint(client: SpdexClient) -> None:
    if not client.auth_configured:
        print(
            "提示: 如果 SPDEX 返回登录页，请在浏览器登录后把 Cookie 写入 .env 的 "
            "SPDEX_COOKIE=...；USERNAME/PASSWORD 本身不能绕过验证码登录。",
            file=sys.stderr,
        )


if __name__ == "__main__":
    raise SystemExit(main())
