#!/usr/bin/env python3
"""World Cup Asian handicap helper based on public SPDEX data.

This tool is a decision aid, not a betting guarantee. It uses public endpoints
observed from SPDEX's web app and intentionally keeps the data layer isolated so
commercial or official data providers can be added later.
"""

from __future__ import annotations

import argparse
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


SPDEX_BASE_URL = "https://app.spdex.com/spdexapi"
SPDEX_WEB_BASE_URL = "https://app.spdex.com"
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
    "water_value": 0.04,
    "data_quality": 0.05,
}

UPPER_THRESHOLD = 0.12
LOWER_THRESHOLD = -0.12
LEAN_THRESHOLD = 0.05

TOP_BOOKMAKERS = ("PinnacleSports", "Bet365", "Singbet", "IBC", "Ysb88")
MATCH_LIST_HOT_MODES = (1, None)
SNAPSHOT_DIR_NAME = ".spdex_snapshots"
SCHEDULE_WINDOWS = (
    ("T-24h 建立基线", timedelta(hours=24), True),
    ("T-8h 观察盘口", timedelta(hours=8), True),
    ("T-4h 观察热度/水位背离", timedelta(hours=4), True),
    ("T-3h 追踪热度/盘口修正", timedelta(hours=3), True),
    ("T-2h 追踪临场资金变化", timedelta(hours=2), True),
    ("T-60m 首次正式推荐", timedelta(minutes=60), True),
    ("T-30m 复核", timedelta(minutes=30), True),
    ("T-15m 最终确认", timedelta(minutes=15), True),
)


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


@dataclass(frozen=True)
class PriceVolumePoint:
    price: float
    volume: float
    update_time: datetime | None
    attr: str | None


@dataclass(frozen=True)
class EuroTrendPoint:
    refresh_time: datetime | None
    home_price: float
    draw_price: float
    away_price: float
    home_kelly: float
    draw_kelly: float
    away_kelly: float


@dataclass
class Signal:
    name: str
    score: float
    weight: float
    available: bool
    reason: str


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

    def __post_init__(self) -> None:
        if not self.model_recommendation:
            self.model_recommendation = self.recommendation
        if self.purchase_score == 0.0:
            self.purchase_score = self.score
        if not self.decision_reason:
            self.decision_reason = "按综合分数直接推荐"

    @property
    def lean(self) -> str:
        if abs(self.score) < LEAN_THRESHOLD:
            return "无明显倾向"
        if self.score < 0:
            return "下盘"
        return "上盘"

    @property
    def lean_team(self) -> str:
        if self.lean == "无明显倾向":
            return ""
        if self.lean == "下盘":
            return self.lower_team
        return self.upper_team

    @property
    def purchase_side(self) -> str:
        if self.recommendation in ("上盘", "下盘"):
            return self.recommendation
        return "观望"

    @property
    def purchase_team(self) -> str:
        if self.purchase_side == "上盘":
            return self.upper_team
        if self.purchase_side == "下盘":
            return self.lower_team
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.match.event_id,
            "match": f"{self.match.home} vs {self.match.away}",
            "match_time": self.match.match_time.isoformat(),
            "asian_line": self.match.asian_line,
            "upper_team": self.upper_team,
            "lower_team": self.lower_team,
            "recommendation": self.recommendation,
            "purchase_side": self.purchase_side,
            "purchase_team": self.purchase_team,
            "purchase_score": round(self.purchase_score, 4),
            "model_recommendation": self.model_recommendation,
            "model_confidence": self.model_confidence,
            "decision_reason": self.decision_reason,
            "is_reversed": self.is_reversed,
            "lean": self.lean,
            "lean_team": self.lean_team,
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
                }
                for signal in self.signals
            ],
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class PurchaseDecision:
    side: str
    score: float
    confidence: int
    reason: str
    is_reversed: bool


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
        if successful_requests == 0:
            if last_error:
                raise last_error
            raise DataError("SPDEX match list is unavailable")
        return sorted(matches, key=lambda item: item.match_time)

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
        record = {
            "schema": 1,
            "fetched_at": fetched_at.isoformat(),
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
        upper_team, lower_team = upper_lower_teams(match)

        snapshot_context = self._snapshot_context(match)
        handicap_rows = self._handicap_rows(match, warnings)
        bifa_signal = self._bifa_signal(match, upper_team, lower_team)
        trade_signal = self._trade_signal(match, upper_team, lower_team, warnings, snapshot_context)
        handicap_signal = self._handicap_signal(match, upper_team, lower_team, handicap_rows, snapshot_context)
        euro_kelly_signal = self._euro_kelly_signal(match, upper_team, lower_team, warnings)
        draw_risk_signal = self._draw_risk_signal(match, upper_team, lower_team)
        fair_line_signal = self._fair_line_signal(match, upper_team, lower_team, handicap_rows)
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

        signals: list[Signal] = [
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

        available_weight = sum(s.weight for s in signals if s.available)
        weighted_score = 0.0
        if available_weight > 0:
            weighted_score = sum(s.score * s.weight for s in signals if s.available)
            weighted_score = weighted_score / available_weight

        completeness = int(round(100 * available_weight / (1 - WEIGHTS["data_quality"])))
        completeness = clamp_int(completeness, 0, 100)

        snapshot_stop_lift = snapshot_stop_update_lift(match, snapshot_trend_signal)
        if available_weight < 0.50:
            if snapshot_stop_lift:
                model_recommendation = recommendation_from_score(weighted_score)
                model_confidence = confidence_from_score(weighted_score, completeness, model_recommendation)
                warnings.append(
                    "临场数据停更且可用信号权重偏低；模型方向仍按综合分阈值计算，并请结合本地快照趋势理解"
                )
            else:
                model_recommendation = "观望"
                model_confidence = clamp_int(int(35 * completeness / 100), 0, 45)
                warnings.append("可用信号不足，未达到最低分析权重")
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
        decision_signals = [*signals, score_momentum_signal]
        purchase_decision = purchase_decision_from_signals(
            match=match,
            weighted_score=weighted_score,
            completeness=completeness,
            available_weight=available_weight,
            model_recommendation=model_recommendation,
            signals=decision_signals,
        )
        return AnalysisResult(
            match=match,
            recommendation=purchase_decision.side,
            score=weighted_score,
            confidence=purchase_decision.confidence,
            completeness=completeness,
            upper_team=upper_team,
            lower_team=lower_team,
            signals=[*decision_signals, data_quality_signal],
            warnings=warnings,
            model_recommendation=model_recommendation,
            model_confidence=model_confidence,
            purchase_score=purchase_decision.score,
            decision_reason=purchase_decision.reason,
            is_reversed=purchase_decision.is_reversed,
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

    def _handicap_rows(self, match: Match, warnings: list[str]) -> list[HandicapRow]:
        if match.raw.get("_source") == "newspdex":
            return sorted(fallback_handicap_rows_from_base(match), key=bookmaker_priority)
        try:
            rows = self.client.handicap_list(match.event_id, match.asian_line)
        except DataError as exc:
            warnings.append(str(exc))
            rows = fallback_handicap_rows_from_base(match)
        if not rows:
            rows = fallback_handicap_rows_from_base(match)
        return sorted(rows, key=bookmaker_priority)

    def _snapshot_context(self, match: Match) -> SnapshotContext | None:
        if self.snapshot_store is None:
            return None
        records = self.snapshot_store.load_event(match.event_id)
        if not records:
            return None
        first_metrics = snapshot_metrics(records[0])
        last_metrics = snapshot_metrics(records[-1])
        signal_history_score, signal_history_reason = score_snapshot_signal_history(records)
        return SnapshotContext(
            records=records,
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
            upper_odds = float(raw.get(f"BfOdds{upper_key}", 0.0))
            lower_odds = float(raw.get(f"BfOdds{lower_key}", 0.0))
        except (TypeError, ValueError):
            return unavailable_signal("必发指数", WEIGHTS["bifa"], "必发字段解析失败")

        if upper_index == 0 and lower_index == 0 and upper_amount == 0 and lower_amount == 0:
            return unavailable_signal("必发指数", WEIGHTS["bifa"], "必发指数和成交量为空")

        index_edge, amount_edge = bifa_index_amount_edges(match, upper_team, lower_team)
        payout_edge = clamp((lower_payout - upper_payout) / 100.0, -1, 1)
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
        reason = (
            f"{upper_team} 必发指数 {upper_index:.1f} vs {lower_team} {lower_index:.1f}，"
            f"成交额 {upper_amount:,.0f} vs {lower_amount:,.0f}，"
            f"盈亏 {upper_payout:.1f} vs {lower_payout:.1f}，"
            f"必发赔率 {upper_odds:.2f} vs {lower_odds:.2f}"
        )
        split_reason = bifa_heat_split_reason(index_edge, amount_edge, upper_team, lower_team)
        if split_reason:
            reason += f"，{split_reason}"
        if hot_divergence_penalty:
            reason += f"，大热未获赔率/盈亏确认 扣分 {hot_divergence_penalty:.2f}"
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
            if match.raw.get("_source") == "newspdex":
                upper_points = self.client.newspdex_tradeflow(match.event_id, upper_selection)
                lower_points = self.client.newspdex_tradeflow(match.event_id, lower_selection)
            else:
                upper_points = self.client.price_volume(match.event_id, upper_selection)
                lower_points = self.client.price_volume(match.event_id, lower_selection)
        except DataError as exc:
            warnings.append(str(exc))
            return unavailable_signal("必发成交走势", WEIGHTS["bifa_trade"], "成交走势接口不可用")

        upper_score, upper_reason, upper_meta = score_price_volume(upper_points)
        lower_score, lower_reason, lower_meta = score_price_volume(lower_points)
        if upper_score is None or lower_score is None:
            return unavailable_signal("必发成交走势", WEIGHTS["bifa_trade"], "近1小时成交走势不足")

        signal_score = clamp((upper_score - lower_score) / 2.0, -1, 1)
        # 澳客明细等：两侧都是「时间趋势」且单边综合分差很小 → 用 raw_trend 对比强化相对强弱（少被同向价格项稀释）
        if (
            upper_meta
            and lower_meta
            and upper_meta.get("branch") == "trend"
            and lower_meta.get("branch") == "trend"
            and int(upper_meta.get("n", 0)) >= 5
            and int(lower_meta.get("n", 0)) >= 5
            and abs(upper_score - lower_score) < 0.20
        ):
            rt_u = float(upper_meta.get("raw_trend") or 0.0)
            rt_l = float(lower_meta.get("raw_trend") or 0.0)
            contrast = clamp((rt_u - rt_l) / 2.0, -1, 1)
            signal_score = clamp(0.42 * signal_score + 0.58 * contrast, -1, 1)

        return Signal(
            "必发成交走势",
            signal_score,
            WEIGHTS["bifa_trade"],
            True,
            f"{upper_team}: {upper_reason}；{lower_team}: {lower_reason}",
        )

    def _handicap_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        rows: list[HandicapRow],
        snapshot_context: SnapshotContext | None,
    ) -> Signal:
        if not rows:
            return unavailable_signal("亚盘水位", WEIGHTS["asian_handicap"], "该盘口暂无公司数据")

        selected_rows = rows
        row_scores: list[float] = []
        reasons: list[str] = []
        fallback_only = all(row.source == "fallback" for row in selected_rows)
        for row in selected_rows:
            row_score = score_handicap_row(match, row, upper_team)
            if row.source == "fallback":
                row_score *= 0.45
            row_scores.append(row_score)
            if len(reasons) < 3:
                reasons.append(
                    f"{row.name} 当前 {row.sec_a:.3g}/{row.sec_b:.3g} 初盘 {row.init_sec_a:.3g}/{row.init_sec_b:.3g}"
                )

        score = clamp(sum(row_scores) / len(row_scores), -1, 1)
        upper_init_avg, upper_now_avg, upper_move = average_upper_water_movement(selected_rows, match, upper_team)
        if upper_move > 0.035:
            move_penalty = clamp(upper_move / 0.20, 0, 0.35)
            score = clamp(score - move_penalty, -1, 1)
            reasons.append(
                f"上盘平均水位上升 {upper_init_avg:.3g}->{upper_now_avg:.3g}，扣分 {move_penalty:.2f}"
            )
        elif upper_move < -0.035:
            reasons.append(f"上盘平均水位下降 {upper_init_avg:.3g}->{upper_now_avg:.3g}")
        if snapshot_context and snapshot_context.available:
            if snapshot_context.heat_delta > 0.06 and snapshot_context.upper_water_delta > 0.025:
                history_penalty = clamp(
                    0.12 + snapshot_context.heat_delta * 0.65 + snapshot_context.upper_water_delta * 1.5,
                    0,
                    0.35,
                )
                score = clamp(score - history_penalty, -1, 1)
                reasons.append(
                    f"历史热度升高但上盘水位也升高，扣分 {history_penalty:.2f}"
                )
            elif snapshot_context.heat_delta > 0.06 and snapshot_context.upper_water_delta < -0.025:
                reasons.append("历史热度升高且上盘降水")
        bifa_heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        divergence_penalty = score_heat_handicap_divergence_penalty(bifa_heat_edge, score)
        if fallback_only:
            divergence_penalty *= 0.35
        if divergence_penalty:
            score = clamp(score - divergence_penalty, -1, 1)
            reasons.append(f"必发热度与亚盘水位背离 扣分 {divergence_penalty:.2f}")
        if fallback_only:
            reasons.append("静态亚盘均值兜底，已降权")
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
        if match.raw.get("_source") == "newspdex":
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
        kelly_edge = clamp((upper_kelly_drop - lower_kelly_drop) / 8.0, -1, 1)
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
        has_upper_water = upper_water > 0
        gap = fair_depth - actual_depth
        heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        index_edge, amount_edge = bifa_index_amount_edges(match, upper_team, lower_team)
        payout_edge = score_bifa_payout_edge(match, upper_team, lower_team)
        heat_split = index_edge * amount_edge < 0 and abs(index_edge) >= 0.04 and abs(amount_edge) >= 0.18
        hot_upper_pressure = max(heat_edge, amount_edge, 0.0)
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
            score = clamp(score - penalty, -1, 1)
            bifa_reasons.append(f"必发资金偏上盘但盘口未升深/低水防守，扣分 {penalty:.2f}")
        if payout_edge < -0.10 and gap > 0.12:
            penalty = clamp(0.05 + abs(payout_edge) * 0.20, 0, 0.18)
            score = clamp(score - penalty, -1, 1)
            bifa_reasons.append(f"必发盈亏不支持上盘，扣分 {penalty:.2f}")
        elif payout_edge > 0.15 and score > 0:
            bonus = clamp(payout_edge * 0.10, 0, 0.06)
            score = clamp(score + bonus, -1, 1)
            bifa_reasons.append(f"必发盈亏确认上盘，补强 {bonus:.2f}")
        if bifa_reasons:
            interpretation += "；" + "；".join(bifa_reasons)
        return Signal(
            "盘口合理性",
            score,
            WEIGHTS["fair_line"],
            True,
            (
                f"价格估算合理盘口约 {fair_depth:.2f}，实际盘口 {actual_depth:.2f}，"
                f"上盘均水 {upper_water:.3g}；{probability_reason}；{interpretation}"
            ),
        )

    def _bookmaker_consensus_signal(
        self, match: Match, upper_team: str, rows: list[HandicapRow]
    ) -> Signal:
        live_rows = [row for row in rows if row.source != "fallback"]
        if len(live_rows) < 2:
            return unavailable_signal("公司一致性", WEIGHTS["bookmaker_consensus"], "主流公司盘口点不足")

        selected_rows = sorted(live_rows, key=bookmaker_priority)[:8]
        scores = [score_handicap_row(match, row, upper_team) for row in selected_rows]
        if not scores:
            return unavailable_signal("公司一致性", WEIGHTS["bookmaker_consensus"], "主流公司盘口点不足")
        positives = sum(1 for score in scores if score > 0.08)
        negatives = sum(1 for score in scores if score < -0.08)
        neutral = len(scores) - positives - negatives
        avg = sum(scores) / len(scores)
        dispersion = score_dispersion(scores)
        consistency = clamp(1.0 - dispersion / 0.55, 0.25, 1.0)
        mixed_penalty = 0.20 if positives and negatives else 0.0
        adjusted = avg * consistency
        if mixed_penalty and adjusted:
            adjusted = math.copysign(max(abs(adjusted) - mixed_penalty, 0.0), adjusted)
        score = clamp(adjusted, -1, 1)
        return Signal(
            "公司一致性",
            score,
            WEIGHTS["bookmaker_consensus"],
            True,
            f"{len(scores)}家公司：上盘{positives}，下盘{negatives}，中性{neutral}，分歧度 {dispersion:.2f}",
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
            if snapshot_context.heat_delta > 0.06 and snapshot_context.upper_water_delta > 0.025:
                penalty += 0.18
                reasons.append("历史热度升高但上盘升水")
            if depth >= 0.75 and snapshot_context.heat_delta > 0.06 and snapshot_context.line_depth_delta <= 0.05:
                penalty += 0.10
                reasons.append("历史热度升高但盘口未升深")

        if draw_risk_signal.available and draw_risk_signal.score < 0.02 and depth <= 1.25:
            penalty += 0.12
            reasons.append("中盘存在平局/小胜风险")

        if bookmaker_consensus_signal.available and bookmaker_consensus_signal.score < -0.08:
            penalty += 0.10
            reasons.append("主流公司分歧偏下盘")

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
        heat_delta = last_metrics["heat_edge"] - previous_metrics["heat_edge"]
        water_delta = last_metrics["upper_water"] - previous_metrics["upper_water"]
        line_delta = last_metrics["line_depth"] - previous_metrics["line_depth"]
        signal_trend_score = snapshot_context.signal_history_score
        signal_reason = snapshot_context.signal_history_reason
        trend_score = clamp(
            0.35 * clamp((0.60 * recent_score_delta + 0.40 * total_score_delta) / 0.25, -1, 1)
            + 0.65 * signal_trend_score,
            -1,
            1,
        )
        if heat_delta > 0.08 and water_delta > 0.04:
            trend_score = clamp(trend_score - 0.25, -1, 1)
        elif heat_delta > 0.08 and water_delta < -0.04:
            trend_score = clamp(trend_score + 0.20, -1, 1)
        if line_delta > 0.24:
            trend_score = clamp(trend_score + 0.12, -1, 1)
        elif line_delta < -0.24:
            trend_score = clamp(trend_score - 0.12, -1, 1)
        return Signal(
            "快照趋势",
            trend_score,
            WEIGHTS["snapshot_trend"],
            True,
            (
                f"本地 {len(records)} 条快照，近期score {recent_score_delta:+.3f}，"
                f"总变化 {total_score_delta:+.3f}，热度 {heat_delta:+.3f}，上盘水位 {water_delta:+.3f}；"
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
        _, _, hot_water_move = average_team_water_movement(rows, match, hot_team)
        reaction = 0.0
        reasons: list[str] = [f"{hot_team}热度 {heat_edge:+.2f}"]

        if price_confirm >= 0.18:
            reaction += 0.20
            reasons.append("必发价格有确认")
        elif price_confirm <= -0.05:
            reaction -= 0.24
            reasons.append("必发价格未确认")

        if hot_water_move < -0.035:
            reaction += 0.34
            reasons.append(f"{hot_team}水位下降 {hot_water_move:+.3f}")
        elif hot_water_move > 0.035:
            reaction -= 0.42
            reasons.append(f"{hot_team}水位上升 {hot_water_move:+.3f}")
        elif rows:
            reaction -= 0.10
            reasons.append("盘口水位反应偏弱")

        if payout_confirm < -0.12:
            reaction -= 0.15
            reasons.append("盈亏压力未支持热门方")
        elif payout_confirm > 0.12:
            reaction += 0.08
            reasons.append("盈亏压力支持热门方")

        if snapshot_context and snapshot_context.available:
            heat_delta = snapshot_context.heat_delta * hot_direction
            hot_water_delta = snapshot_context.upper_water_delta if hot_direction > 0 else snapshot_context.lower_water_delta
            depth_delta = snapshot_context.line_depth_delta * hot_direction
            if heat_delta > 0.06 and hot_water_delta > 0.025:
                reaction -= 0.22
                reasons.append("历史热度升高但热门方升水")
            elif heat_delta > 0.06 and hot_water_delta < -0.025:
                reaction += 0.18
                reasons.append("历史热度升高且热门方降水")
            if heat_delta > 0.06 and depth_delta > 0.05:
                reaction += 0.12
                reasons.append("盘口深度随热度防守")
            elif heat_delta > 0.06 and depth_delta < -0.05:
                reaction -= 0.12
                reasons.append("盘口深度逆热度放松")

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
        return Signal(
            "外部赔率/实力校验",
            score,
            WEIGHTS["external_consensus"],
            True,
            f"{upper_team} vs {lower_team}；" + "；".join(reason for _, reason in components),
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
            score = clamp(score, -0.25, 0.25)
            reasons.append("常规水位，仅做弱修正")

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

            if price_confirm >= 0.10:
                components.append(0.25 * hot_direction)
                reasons.append(f"{hot_side}热度获必发价格确认")
            else:
                components.append(-0.35 * hot_direction)
                reasons.append(f"{hot_side}热度未获必发价格确认")

            if handicap_signal.available:
                if handicap_confirm >= 0.08:
                    weight = 0.16 if handicap_is_fallback else 0.35
                    components.append(weight * hot_direction)
                    reasons.append("亚盘低水/降水确认热门方" + ("(静态均值降权)" if handicap_is_fallback else ""))
                elif handicap_confirm <= -0.08:
                    weight = 0.16 if handicap_is_fallback else 0.45
                    components.append(-weight * hot_direction)
                    reasons.append("亚盘升水或分歧，热门方买入更危险" + ("(静态均值降权)" if handicap_is_fallback else ""))
                else:
                    weight = 0.05 if handicap_is_fallback else 0.15
                    components.append(-weight * hot_direction)
                    reasons.append("亚盘对热门方防守不足" + ("(静态均值降权)" if handicap_is_fallback else ""))

            if trade_signal.available:
                if trade_confirm >= 0.10:
                    components.append(0.20 * hot_direction)
                    reasons.append("成交走势顺热度")
                elif trade_confirm <= -0.10:
                    components.append(-0.25 * hot_direction)
                    reasons.append("成交走势反热度")

            if euro_kelly_signal.available:
                if euro_confirm >= 0.10:
                    components.append(0.15 * hot_direction)
                    reasons.append("欧赔/Kelly同步")
                elif euro_confirm <= -0.10:
                    components.append(-0.15 * hot_direction)
                    reasons.append("欧赔/Kelly背离")

            if fair_line_signal.available:
                if fair_confirm >= 0.10:
                    components.append(0.12 * hot_direction)
                    reasons.append("盘口深度与价格匹配")
                elif fair_confirm <= -0.10:
                    components.append(-0.16 * hot_direction)
                    reasons.append("盘口相对价格偏深/偏危险")

            if depth_profile_signal.available:
                if depth_confirm >= 0.10:
                    components.append((0.10 if depth <= 0.5 else 0.16) * hot_direction)
                    reasons.append("盘口深度模型确认")
                elif depth_confirm <= -0.10:
                    components.append((-0.12 if depth <= 0.5 else -0.22) * hot_direction)
                    reasons.append("盘口深度模型背离")

            if market_elasticity_signal.available:
                if elasticity_confirm >= 0.12:
                    components.append(0.16 * hot_direction)
                    reasons.append("资金/盘口弹性确认热门方")
                elif elasticity_confirm <= -0.12:
                    components.append(-0.20 * hot_direction)
                    reasons.append("资金/盘口弹性背离热门方")

            if external_consensus_signal.available:
                if external_confirm >= 0.12:
                    components.append(0.12 * hot_direction)
                    reasons.append("外部赔率/实力同步")
                elif external_confirm <= -0.12:
                    components.append(-0.14 * hot_direction)
                    reasons.append("外部赔率/实力背离")

            if water_value_signal.available:
                if water_value_confirm >= 0.30:
                    components.append(0.10 * hot_direction)
                    reasons.append("高低水价值确认热门方")
                elif water_value_confirm <= -0.30:
                    components.append(-0.14 * hot_direction)
                    reasons.append("高低水价值背离热门方")

            if draw_risk_signal.available and depth <= 0.5 and hot_direction > 0 and draw_edge <= -0.18:
                components.append(draw_edge * 0.45)
                reasons.append("浅盘热门存在平局风险")

            if snapshot_context and snapshot_context.available and hot_direction > 0:
                if snapshot_context.heat_delta > 0.06 and snapshot_context.upper_water_delta > 0.025:
                    components.append(-0.22 * hot_direction)
                    reasons.append("历史热度升高但盘口未防守热门方")
                elif snapshot_context.heat_delta > 0.06 and snapshot_context.upper_water_delta < -0.025:
                    components.append(0.12 * hot_direction)
                    reasons.append("历史热度升高且盘口降水确认")
                if snapshot_context.line_depth_delta <= 0.05 and depth >= 0.75 and snapshot_context.heat_delta > 0.06:
                    components.append(-0.10 * hot_direction)
                    reasons.append("历史热度升高但盘口未升深")
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
            if external_consensus_signal.available and abs(external_edge) >= 0.18:
                components.append(0.12 * math.copysign(1.0, external_edge))
                reasons.append(f"热度不高但外部校验偏{direction_label(external_edge)}")
            if water_value_signal.available and abs(water_value_edge) >= 0.30:
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
        same_direction_count = count_same_direction(
            [
                bifa_signal,
                trade_signal,
                handicap_signal,
                euro_kelly_signal,
                fair_line_signal,
                depth_profile_signal,
                market_elasticity_signal,
                external_consensus_signal,
                water_value_signal,
            ]
        )
        conflicts = signal_conflict_count(
            [
                bifa_signal,
                trade_signal,
                handicap_signal,
                euro_kelly_signal,
                fair_line_signal,
                depth_profile_signal,
                market_elasticity_signal,
                external_consensus_signal,
                water_value_signal,
            ]
        )
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


def parse_handicap(item: dict[str, Any], bookmaker_id: int | None = None) -> HandicapRow:
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
        )
    ]


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
        upper_amount = float(match.raw.get(f"BfAmount{upper_key}", 0.0))
        lower_amount = float(match.raw.get(f"BfAmount{lower_key}", 0.0))
    except (TypeError, ValueError):
        return 0.0, 0.0
    index_edge = clamp((upper_index - lower_index) / 100.0, -1, 1)
    amount_total = upper_amount + lower_amount
    amount_edge = 0.0 if amount_total <= 0 else clamp((upper_amount - lower_amount) / amount_total, -1, 1)
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
    except (TypeError, ValueError):
        return 0.0
    return clamp((lower_payout - upper_payout) / 100.0, -1, 1)


def score_heat_handicap_divergence_penalty(heat_edge: float, handicap_score: float) -> float:
    """Penalize a very hot side if the Asian handicap signal points the other way."""
    if abs(heat_edge) < 0.25:
        return 0.0
    same_direction_handicap = math.copysign(1, heat_edge) * handicap_score
    if same_direction_handicap >= -0.05:
        return 0.0
    return clamp(0.15 + 0.35 * abs(heat_edge) + 0.25 * abs(same_direction_handicap), 0, 0.50)


def model_edge_for_water_value(signals: list[Signal]) -> float:
    weights = {
        "必发指数": 0.22,
        "必发成交走势": 0.12,
        "欧赔/Kelly": 0.15,
        "盘口合理性": 0.14,
        "盘口深度/打穿能力": 0.10,
        "快照趋势": 0.12,
        "资金/盘口弹性": 0.08,
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


def score_handicap_row(match: Match, row: HandicapRow, upper_team: str) -> float:
    upper_is_home = upper_team == match.home
    upper_now = row.sec_a if upper_is_home else row.sec_b
    lower_now = row.sec_b if upper_is_home else row.sec_a
    upper_init = row.init_sec_a if upper_is_home else row.init_sec_b
    lower_init = row.init_sec_b if upper_is_home else row.init_sec_a

    current_water_edge = clamp((lower_now - upper_now) / 0.45, -1, 1)
    relative_movement_edge = clamp(((upper_init - upper_now) - (lower_init - lower_now)) / 0.35, -1, 1)
    upper_water_direction = clamp((upper_init - upper_now) / 0.18, -1, 1)
    payout_quality = clamp((row.payout - 0.92) / 0.08, 0, 1)
    return clamp(
        (0.35 * current_water_edge + 0.25 * relative_movement_edge + 0.40 * upper_water_direction)
        * (0.70 + 0.30 * payout_quality),
        -1,
        1,
    )


def bookmaker_priority(row: HandicapRow) -> tuple[int, str]:
    try:
        return (TOP_BOOKMAKERS.index(row.name), row.name)
    except ValueError:
        return (len(TOP_BOOKMAKERS), row.name)


def recommendation_from_score(score: float) -> str:
    if score > UPPER_THRESHOLD:
        return "上盘"
    if score < LOWER_THRESHOLD:
        return "下盘"
    return "观望"


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

    reason = (
        f"当前score {current_score:+.3f}，上一快照 {previous_score:+.3f}，"
        f"近期变化 {recent_delta:+.3f}，总变化 {total_delta:+.3f}"
    )
    if notes:
        reason += "；" + "；".join(notes)
    return Signal("临场score变化", momentum, 0.0, True, reason)


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
        reasons.append("可用信号不足，观望不买")
        confidence = purchase_confidence_from_score(
            purchase_score=adjusted_score,
            completeness=completeness,
            available_weight=available_weight,
            model_recommendation=model_recommendation,
            final_side="观望",
            raw_score=weighted_score,
        )
        return PurchaseDecision(
            side="观望",
            score=adjusted_score,
            confidence=confidence,
            reason="；".join(reasons),
            is_reversed=False,
        )

    if available_weight < 0.50 and snapshot_stop_lift:
        reasons.append("临场停更且可用权重偏低；继续应用购买门控（快照趋势已参与加权）")

    secondary_enabled = abs(weighted_score) < 0.15 or model_recommendation == "观望"
    if secondary_enabled:
        reasons.append("原始优势较弱，启用购买门控")

    handicap = lookup.get("亚盘水位")
    bookmaker_consensus = lookup.get("公司一致性")
    market_balance = lookup.get("市场平衡/背离")
    strong_upper_consensus = (
        weighted_score > 0.10
        and signal_value(handicap) >= 0.40
        and signal_value(bookmaker_consensus) >= 0.30
    )

    cover_risk = lookup.get("赢盘门槛风险")
    if (
        secondary_enabled
        and cover_risk
        and cover_risk.available
        and cover_risk.score < -0.05
        and weighted_score > -LEAN_THRESHOLD
    ):
        risk_shift = clamp(abs(cover_risk.score) * 0.35, 0.02, 0.20)
        if strong_upper_consensus:
            risk_shift *= 0.30
            reasons.append("强亚盘/公司共识保护，门槛风险仅降权不反向")
        adjusted_score -= risk_shift
        reasons.append(f"上盘赢盘门槛风险向下修正 {risk_shift:.2f}")
        if cover_risk.score <= -0.35 and weighted_score > 0 and not strong_upper_consensus:
            adjusted_score = max(adjusted_score, weighted_score * 0.35)
            reasons.append("门槛风险较高，仅缩小上盘优势")

    score_momentum = lookup.get("临场score变化")
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
        adjusted_score += trend_shift
        if abs(trend_shift) >= 0.02:
            reasons.append(f"快照趋势二次修正 {trend_shift:+.2f}")

    if secondary_enabled and market_balance and market_balance.available and abs(market_balance.score) >= 0.25:
        market_shift = 0.08 * market_balance.score
        adjusted_score += market_shift
        reasons.append(f"盘口防守/背离修正 {market_shift:+.2f}")

    market_elasticity = lookup.get("资金/盘口弹性")
    if secondary_enabled and market_elasticity and market_elasticity.available and abs(market_elasticity.score) >= 0.12:
        elasticity_shift = 0.12 * market_elasticity.score
        adjusted_score += elasticity_shift
        reasons.append(f"资金/盘口弹性修正 {elasticity_shift:+.2f}")

    if secondary_enabled and handicap and handicap.available and abs(handicap.score) >= 0.15:
        water_shift = 0.10 * handicap.score
        adjusted_score += water_shift
        reasons.append(f"亚盘水位二次修正 {water_shift:+.2f}")

    water_value = lookup.get("高低水价值")
    if secondary_enabled and water_value and water_value.available and abs(water_value.score) >= 0.12:
        value_shift = 0.11 * water_value.score
        if strong_upper_consensus and value_shift < 0:
            value_shift *= 0.40
            reasons.append("强亚盘/公司共识保护，低水价值负修正降权")
        adjusted_score += value_shift
        reasons.append(f"高低水价值修正 {value_shift:+.2f}")

    external_consensus = lookup.get("外部赔率/实力校验")
    if secondary_enabled and external_consensus and external_consensus.available and abs(external_consensus.score) >= 0.12:
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

    if secondary_enabled and bookmaker_consensus and bookmaker_consensus.available and bookmaker_consensus.score < -0.12:
        adjusted_score -= 0.05
        reasons.append("主流公司一致性偏下盘")

    if strong_upper_consensus and adjusted_score < 0:
        adjusted_score = max(min(weighted_score * 0.50, 0.08), 0.03)
        reasons.append("强亚盘/公司共识保护，不允许二次门控反向")

    if (
        weighted_score > 0.10
        and signal_value(market_balance) > 0.45
        and signal_value(handicap) > 0.30
        and signal_value(cover_risk) > -0.55
    ):
        adjusted_score = max(adjusted_score, 0.06)
        reasons.append("盘口防守和亚盘同向，保留正向")

    adjusted_score = clamp(adjusted_score, -1, 1)
    reference_side = reference_side_from_model(weighted_score, model_recommendation)
    if reference_side == "无明显倾向" and abs(adjusted_score) < 0.06:
        side = "观望"
        reasons.append("方向和购买优势都不足，观望不买")
    else:
        side = "上盘" if adjusted_score > 0 else "下盘"
    attempted_reverse = (
        reference_side in ("上盘", "下盘")
        and side != reference_side
        and side != "观望"
        and abs(weighted_score) >= LEAN_THRESHOLD
    )
    if attempted_reverse and (
        model_recommendation == "观望"
        or abs(adjusted_score) < 0.10
        or (completeness < 60 and not snapshot_stop_lift)
    ):
        reasons.append(f"二次门控尝试由{reference_side}反向到{side}，但优势/置信不足，观望不买")
        side = "观望"
    elif attempted_reverse:
        reasons.append(f"最终购买方向由{reference_side}反向到{side}")
    elif model_recommendation == "观望":
        reasons.append(f"模型阈值为观望，低优势选择{side}")
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
    if final_side == "观望":
        base = 28 + min(abs(raw_score), 0.35) * 55
        if abs(purchase_score) < 0.08:
            base -= 4
        if cap_weight < 0.25:
            base = min(base, 22)
        elif cap_weight < 0.50:
            base = min(base, 32)
        elif cap_weight < 0.65:
            base = min(base, 45)
        quality_factor = 0.50 + 0.50 * (eff_completeness / 100)
        return clamp_int(int(base * quality_factor), 0, 55)
    strength = min(abs(purchase_score), 0.70)
    base = 32 + strength * 72
    if model_recommendation == final_side:
        base += 8
    elif model_recommendation == "观望":
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
    if recommendation == "观望":
        return clamp_int(int((28 + abs(score) * 40) * completeness / 100), 0, 55)
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
        ("T-4h 进入重点观察", match_time - timedelta(hours=4)),
        ("T-3h 追踪热度/盘口修正", match_time - timedelta(hours=3)),
        ("T-2h 追踪临场资金变化", match_time - timedelta(hours=2)),
        ("T-60m 首次正式推荐", match_time - timedelta(minutes=60)),
        ("T-30m 复核", match_time - timedelta(minutes=30)),
        ("T-15m 最终确认", match_time - timedelta(minutes=15)),
    ]
    future = [f"{label}: {format_local(dt)}" for label, dt in windows if dt >= now]
    return future or ["当前已进入临场窗口，建议立即拉取并在 T-15m 前复核"]


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
    if result.model_recommendation in ("上盘", "下盘"):
        model_text = f"{result.model_recommendation}({side_team(result.model_recommendation, result.upper_team, result.lower_team)})"
    elif result.lean == "无明显倾向":
        model_text = "观望(无明显倾向)"
    else:
        model_text = f"观望(倾向{result.lean}:{result.lean_team})"
    print(
        f"{match.event_id} | {format_local(match.match_time)} | {match.home} vs {match.away} | "
        f"盘口 {match.asian_line} | 推荐 {purchase_display_text(result)} | "
        f"模型 {model_text} | 置信度 {result.confidence}% | 完整度 {result.completeness}% | "
        f"score {result.score:+.3f} | purchase {result.purchase_score:+.3f}"
    )
    print(f"  [DECISION] {result.decision_reason}")
    for signal in result.signals:
        if verbose or signal.available:
            mark = "OK" if signal.available else "NA"
            print(f"  [{mark}] {signal.name}: {signal.score:+.3f} - {signal.reason}")
    for warning in result.warnings:
        print(f"  [WARN] {warning}")


def purchase_display_text(result: AnalysisResult) -> str:
    if result.purchase_side == "观望":
        if result.lean == "无明显倾向":
            return "观望(无明显倾向)"
        return f"观望(倾向{result.lean}:{result.lean_team})"
    if abs(result.purchase_score) < 0.06:
        return f"{result.purchase_side}(低优势:{result.purchase_team})"
    return f"{result.purchase_side}({result.purchase_team})"


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
        f"({score_delta:+.3f}) | 当前购买方 {result_info.get('purchase_side', result_info.get('recommendation', '未知'))} "
        f"| 模型 {result_info.get('model_recommendation', result_info.get('recommendation', '未知'))}"
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
    upper_water = float_or_zero(raw.get(f"AsianAvr{upper_key}"))
    lower_water = float_or_zero(raw.get(f"AsianAvr{lower_key}"))
    upper_euro = float_or_zero(raw.get(f"EuroAvr{upper_key}"))
    lower_euro = float_or_zero(raw.get(f"EuroAvr{lower_key}"))
    amount_total = upper_amount + lower_amount
    amount_edge = 0.0 if amount_total <= 0 else clamp((upper_amount - lower_amount) / amount_total, -1, 1)
    index_edge = clamp((upper_index - lower_index) / 100.0, -1, 1)
    return {
        "score": float_or_zero(result_info.get("score")),
        "heat_edge": clamp(0.55 * index_edge + 0.45 * amount_edge, -1, 1),
        "amount_edge": amount_edge,
        "payout_edge": clamp((lower_payout - upper_payout) / 100.0, -1, 1),
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
    plain = SpdexClient(use_env_auth=False)
    authed = SpdexClient(use_env_auth=True)
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
            "  python3 worldcup_ah_cli.py predict --all --limit 10\n"
            "      批量分析未开赛世界杯比赛。\n\n"
            "  python3 worldcup_ah_cli.py predict --event-id 35035283 --json\n"
            "      输出 JSON，方便后续保存或回测。\n\n"
            "  python3 worldcup_ah_cli.py snapshot --event-id 35035283\n"
            "      抓取单场当前数据并追加到本地 .spdex_snapshots/，用于趋势判断。\n\n"
            "  python3 worldcup_ah_cli.py trend --event-id 35035283\n"
            "      根据本地快照比较 score、必发热度、盈亏压力、盘口/水位变化。\n\n"
            "  python3 worldcup_ah_cli.py watch --limit 20\n"
            "      常驻运行，按 T-24h/T-8h/T-4h/T-3h/T-2h/T-60m/T-30m/T-15m 自动快照并输出预测。\n\n"
            "  python3 worldcup_ah_cli.py auth-probe --event-id 35035286\n"
            "      对比无鉴权与 .env 鉴权下 match_detail 返回字段是否不同（会员 Cookie 是否作用于 app.spdex.com）。\n\n"
            "  python3 worldcup_ah_cli.py auth-cookie --cookie-stdin --event-id 35035286\n"
            "      从标准输入读取浏览器 Cookie，验证通过后写入 .env 的 SPDEX_COOKIE。\n\n"
            "  python3 worldcup_ah_cli.py sources\n"
            "      查看后续可接入的公开数据源。\n\n"
            "输出说明:\n"
            "  推荐 上盘/下盘: 分数超过购买阈值，给出购买方。\n"
            "  推荐 观望(倾向...): 有方向倾向，但置信度或信号一致性不足。\n"
            "  推荐 观望(无明显倾向): |score| < 0.05，方向信号太弱。\n"
            "  score > 0 偏上盘，score < 0 偏下盘；默认阈值为 +/-0.12。\n"
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
    subparsers = parser.add_subparsers(dest="command", required=True)

    upcoming_parser = subparsers.add_parser(
        "upcoming",
        help="列出未开赛世界杯赛程",
        description="列出 SPDEX 当前可见的未开赛世界杯比赛，并给出 T-24h/T-8h/T-4h/T-3h/T-2h/T-60m 等建议拉取时间。",
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
            "常驻运行：每次检查未来窗口内的世界杯比赛，并按 T-24h/T-8h/T-4h/T-3h/T-2h/T-60m/T-30m/T-15m "
            "自动保存快照并打印预测；T-24h 到 T-2h 为预判，T-60m/T-30m/T-15m 为正式复核/确认。"
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
        try:
            if args.all:
                matches = upcoming_matches(client)[: args.limit]
            else:
                matches = [client.find_match(args.event_id)]
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
        try:
            if args.all:
                matches = upcoming_matches(client)[: args.limit]
            else:
                matches = [client.find_match(args.event_id)]
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
