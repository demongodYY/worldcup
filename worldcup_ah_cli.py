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
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any


SPDEX_BASE_URL = "https://app.spdex.com/spdexapi"
WORLD_CUP_LEAGUE_ID = 911

WEIGHTS = {
    "bifa": 0.30,
    "bifa_trade": 0.15,
    "asian_handicap": 0.20,
    "euro_kelly": 0.10,
    "market_balance": 0.20,
    "data_quality": 0.05,
}

UPPER_THRESHOLD = 0.18
LOWER_THRESHOLD = -0.18

TOP_BOOKMAKERS = ("PinnacleSports", "Bet365", "Singbet", "IBC", "Ysb88")
MATCH_LIST_HOT_MODES = (1,)


class DataError(RuntimeError):
    """Raised when a data source cannot return usable data."""


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.match.event_id,
            "match": f"{self.match.home} vs {self.match.away}",
            "match_time": self.match.match_time.isoformat(),
            "asian_line": self.match.asian_line,
            "upper_team": self.upper_team,
            "lower_team": self.lower_team,
            "recommendation": self.recommendation,
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


class SpdexClient:
    """Small public SPDEX client using only the Python standard library."""

    def __init__(
        self,
        base_url: str = SPDEX_BASE_URL,
        timeout: float = 8.0,
        ssl_fallback: bool = True,
        retries: int = 1,
        curl_fallback: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.ssl_fallback = ssl_fallback
        self.retries = max(0, retries)
        self.curl_fallback = curl_fallback
        self.ssl_fallback_used = False
        self.curl_fallback_used = False

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        query.setdefault("app", "a")
        query.setdefault("version", "1.01")
        query.setdefault("dateformat", "iso8601")
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "worldcup-ah-cli/1.0",
                "Accept": "application/json,text/plain,*/*",
            },
        )
        body: str | None = None
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
                    break
                if attempt < self.retries:
                    time.sleep(0.35 * (attempt + 1))
        if body is None:
            if self.curl_fallback:
                try:
                    body = self._curl_text(url)
                    self.curl_fallback_used = True
                except DataError as curl_exc:
                    raise DataError(f"SPDEX request failed: {url}: {last_exc}; curl fallback: {curl_exc}") from last_exc
            else:
                raise DataError(f"SPDEX request failed: {url}: {last_exc}") from last_exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise DataError(f"SPDEX returned non-JSON data: {url}") from exc

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
            url,
        ]
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
        if not isinstance(data, list):
            raise DataError("SPDEX match_list returned an unexpected shape")
        return [parse_match(item) for item in data if isinstance(item, dict)]

    def match_detail(self, keyword: str) -> Match | None:
        data = self._get_json(
            "/spdex/match_detail",
            {"keyword": keyword, "product_id": 0, "tutorial": 0},
        )
        candidates: list[dict[str, Any]]
        if isinstance(data, list):
            candidates = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict):
            nested = data.get("data")
            if isinstance(nested, list):
                candidates = [item for item in nested if isinstance(item, dict)]
            elif isinstance(nested, dict):
                candidates = [nested]
            else:
                candidates = [data]
        else:
            candidates = []
        for item in candidates:
            try:
                return parse_match(item)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    def find_match(self, event_id: int) -> Match:
        for match in self.world_cup_matches():
            if match.event_id == event_id:
                return match
        detail = self.match_detail(str(event_id))
        if detail:
            return detail
        raise DataError(f"cannot find event_id={event_id} in SPDEX")

    def world_cup_matches(self) -> list[Match]:
        seen: set[int] = set()
        matches: list[Match] = []
        successful_requests = 0
        last_error: DataError | None = None
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
        if not isinstance(data, list):
            raise DataError("SPDEX handicap list returned an unexpected shape")
        return [parse_handicap(item) for item in data if isinstance(item, dict)]

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
        if not isinstance(data, list):
            raise DataError("SPDEX handicap detail returned an unexpected shape")
        return [parse_handicap(item, bookmaker_id=bookmaker_id) for item in data if isinstance(item, dict)]

    def price_volume(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        data = self._get_json(
            "/spdex/price/volumn",
            {"eventid": event_id, "hour": -1, "selection": selection},
        )
        if not isinstance(data, list):
            raise DataError("SPDEX price/volumn returned an unexpected shape")
        return [parse_price_volume(item) for item in data if isinstance(item, dict)]

    def euro_trend(self, event_id: int) -> list[EuroTrendPoint]:
        data = self._get_json(
            "/spdex/odds/1x2/trend",
            {"hour": -1, "eid": event_id},
        )
        if not isinstance(data, list):
            raise DataError("SPDEX euro trend returned an unexpected shape")
        return [parse_euro_trend(item) for item in data if isinstance(item, dict)]


class Predictor:
    def __init__(self, client: SpdexClient):
        self.client = client

    def analyze(self, match: Match) -> AnalysisResult:
        upper_team, lower_team = upper_lower_teams(match)
        warnings: list[str] = []

        bifa_signal = self._bifa_signal(match, upper_team, lower_team)
        trade_signal = self._trade_signal(match, upper_team, lower_team, warnings)
        handicap_signal = self._handicap_signal(match, upper_team, lower_team, warnings)
        euro_kelly_signal = self._euro_kelly_signal(match, upper_team, lower_team, warnings)
        market_balance_signal = self._market_balance_signal(
            match,
            upper_team,
            lower_team,
            bifa_signal,
            trade_signal,
            handicap_signal,
            euro_kelly_signal,
        )

        signals: list[Signal] = [
            bifa_signal,
            trade_signal,
            handicap_signal,
            euro_kelly_signal,
            market_balance_signal,
        ]

        available_weight = sum(s.weight for s in signals if s.available)
        weighted_score = 0.0
        if available_weight > 0:
            weighted_score = sum(s.score * s.weight for s in signals if s.available)
            weighted_score = weighted_score / available_weight

        completeness = int(round(100 * available_weight / (1 - WEIGHTS["data_quality"])))
        completeness = clamp_int(completeness, 0, 100)

        if available_weight < 0.50:
            recommendation = "观望"
            confidence = clamp_int(int(35 * completeness / 100), 0, 45)
            warnings.append("可用信号不足，未达到最低分析权重")
        else:
            recommendation = recommendation_from_score(weighted_score)
            confidence = confidence_from_score(weighted_score, completeness, recommendation)
        if match.is_stop_update:
            warnings.append("SPDEX 标记该场已停更，推荐仅供复盘，不代表可实时购买")

        data_quality_signal = Signal(
            name="数据质量",
            score=(completeness / 100.0) * (1 if weighted_score >= 0 else -1),
            weight=WEIGHTS["data_quality"],
            available=True,
            reason=f"可用权重 {available_weight:.2f}，完整度 {completeness}%",
        )
        return AnalysisResult(
            match=match,
            recommendation=recommendation,
            score=weighted_score,
            confidence=confidence,
            completeness=completeness,
            upper_team=upper_team,
            lower_team=lower_team,
            signals=[*signals, data_quality_signal],
            warnings=warnings,
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

        index_edge = clamp((upper_index - lower_index) / 100.0, -1, 1)
        amount_total = upper_amount + lower_amount
        amount_edge = 0.0 if amount_total <= 0 else clamp((upper_amount - lower_amount) / amount_total, -1, 1)
        payout_edge = clamp((lower_payout - upper_payout) / 100.0, -1, 1)
        odds_edge = score_bifa_odds_confirmation(upper_odds, lower_odds)
        hot_divergence_penalty = score_hot_divergence_penalty(
            heat_edge=0.55 * index_edge + 0.45 * amount_edge,
            confirmation_edge=odds_edge,
            payout_edge=payout_edge,
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
        if hot_divergence_penalty:
            reason += f"，大热未获赔率/盈亏确认 扣分 {hot_divergence_penalty:.2f}"
        return Signal("必发指数", score, WEIGHTS["bifa"], True, reason)

    def _trade_signal(
        self, match: Match, upper_team: str, lower_team: str, warnings: list[str]
    ) -> Signal:
        try:
            upper_selection = selection_for_team(match, upper_team)
            lower_selection = selection_for_team(match, lower_team)
            upper_points = self.client.price_volume(match.event_id, upper_selection)
            lower_points = self.client.price_volume(match.event_id, lower_selection)
        except DataError as exc:
            warnings.append(str(exc))
            return unavailable_signal("必发成交走势", WEIGHTS["bifa_trade"], "成交走势接口不可用")

        upper_score, upper_reason = score_price_volume(upper_points)
        lower_score, lower_reason = score_price_volume(lower_points)
        if upper_score is None or lower_score is None:
            return unavailable_signal("必发成交走势", WEIGHTS["bifa_trade"], "近1小时成交走势不足")

        score = clamp((upper_score - lower_score) / 2.0, -1, 1)
        return Signal(
            "必发成交走势",
            score,
            WEIGHTS["bifa_trade"],
            True,
            f"{upper_team}: {upper_reason}；{lower_team}: {lower_reason}",
        )

    def _handicap_signal(
        self, match: Match, upper_team: str, lower_team: str, warnings: list[str]
    ) -> Signal:
        try:
            rows = self.client.handicap_list(match.event_id, match.asian_line)
        except DataError as exc:
            warnings.append(str(exc))
            rows = fallback_handicap_rows_from_base(match)

        if not rows:
            rows = fallback_handicap_rows_from_base(match)
        if not rows:
            return unavailable_signal("亚盘水位", WEIGHTS["asian_handicap"], "该盘口暂无公司数据")

        selected_rows = sorted(rows, key=bookmaker_priority)
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

    def _market_balance_signal(
        self,
        match: Match,
        upper_team: str,
        lower_team: str,
        bifa_signal: Signal,
        trade_signal: Signal,
        handicap_signal: Signal,
        euro_kelly_signal: Signal,
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

        components: list[float] = []
        reasons: list[str] = []
        hot_side = upper_team if heat_edge >= 0 else lower_team
        hot_direction = math.copysign(1.0, heat_edge) if heat_edge else 0.0

        if abs(heat_edge) >= 0.22:
            price_confirm = hot_direction * bifa_price_edge
            handicap_confirm = hot_direction * handicap_edge
            trade_confirm = hot_direction * trade_edge
            euro_confirm = hot_direction * euro_edge

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
                    reasons.append("亚盘同步提高热门方买入成本" + ("(静态均值降权)" if handicap_is_fallback else ""))
                elif handicap_confirm <= -0.08:
                    weight = 0.16 if handicap_is_fallback else 0.45
                    components.append(-weight * hot_direction)
                    reasons.append("亚盘反而让热门方更好买" + ("(静态均值降权)" if handicap_is_fallback else ""))
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
        else:
            if handicap_signal.available and abs(handicap_edge) >= 0.20:
                components.append(0.45 * math.copysign(1.0, handicap_edge))
                reasons.append("热度不高但亚盘主动防守")
            if trade_signal.available and abs(trade_edge) >= 0.25:
                components.append(0.25 * math.copysign(1.0, trade_edge))
                reasons.append("热度不高但成交走势有方向")
            if euro_kelly_signal.available and abs(euro_edge) >= 0.20:
                components.append(0.15 * math.copysign(1.0, euro_edge))
                reasons.append("热度不高但欧赔/Kelly有方向")

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
            [bifa_signal, trade_signal, handicap_signal, euro_kelly_signal]
        )
        if same_direction_count >= 3:
            score = clamp(score * 1.10, -1, 1)
            reasons.append("多信号同向")
        elif same_direction_count <= -3:
            score = clamp(score * 1.10, -1, 1)
            reasons.append("多信号同向偏下盘")
        elif signal_conflict_count([bifa_signal, trade_signal, handicap_signal, euro_kelly_signal]) >= 3:
            score = clamp(score * 0.65, -1, 1)
            reasons.append("信号互相矛盾，降权")

        return Signal(
            "市场平衡/背离",
            score,
            WEIGHTS["market_balance"],
            True,
            "；".join(reasons),
        )


def parse_match(item: dict[str, Any]) -> Match:
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


def upper_lower_teams(match: Match) -> tuple[str, str]:
    value = line_value(match.asian_line)
    if value < 0:
        return match.home, match.away
    if value > 0:
        return match.away, match.home
    return match.home, match.away


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


def score_price_volume(points: list[PriceVolumePoint]) -> tuple[float | None, str]:
    usable = [point for point in points if point.price > 0]
    if len(usable) < 2:
        return None, "走势点不足"
    buy_volume = sum(point.volume for point in usable if point.attr and "买" in point.attr)
    sell_volume = sum(point.volume for point in usable if point.attr and "卖" in point.attr)
    total_volume = buy_volume + sell_volume
    volume_score = 0.0 if total_volume <= 0 else (buy_volume - sell_volume) / total_volume
    first_price = usable[0].price
    last_price = usable[-1].price
    price_score = clamp((first_price - last_price) / max(first_price * 0.08, 0.08), -1, 1)
    score = clamp(0.65 * volume_score + 0.35 * price_score, -1, 1)
    reason = (
        f"买量 {buy_volume:,.0f} / 卖量 {sell_volume:,.0f}，"
        f"价格 {first_price:.2f}->{last_price:.2f}"
    )
    return score, reason


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
    return clamp(0.20 + 0.35 * heat_size + 0.25 * divergence + 0.20 * payout_warning, 0, 0.55)


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
    upper_key = side_key(match, upper_team)
    lower_key = side_key(match, lower_team)
    if upper_key not in ("Home", "Away") or lower_key not in ("Home", "Away"):
        return 0.0
    try:
        upper_index = float(match.raw.get(f"BfIndex{upper_key}", 0.0))
        lower_index = float(match.raw.get(f"BfIndex{lower_key}", 0.0))
        upper_amount = float(match.raw.get(f"BfAmount{upper_key}", 0.0))
        lower_amount = float(match.raw.get(f"BfAmount{lower_key}", 0.0))
    except (TypeError, ValueError):
        return 0.0
    index_edge = clamp((upper_index - lower_index) / 100.0, -1, 1)
    amount_total = upper_amount + lower_amount
    amount_edge = 0.0 if amount_total <= 0 else clamp((upper_amount - lower_amount) / amount_total, -1, 1)
    return clamp(0.55 * index_edge + 0.45 * amount_edge, -1, 1)


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


def score_heat_handicap_divergence_penalty(heat_edge: float, handicap_score: float) -> float:
    """Penalize a very hot side if the Asian handicap signal points the other way."""
    if abs(heat_edge) < 0.25:
        return 0.0
    same_direction_handicap = math.copysign(1, heat_edge) * handicap_score
    if same_direction_handicap >= -0.05:
        return 0.0
    return clamp(0.15 + 0.35 * abs(heat_edge) + 0.25 * abs(same_direction_handicap), 0, 0.50)


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
    movement_edge = clamp(((upper_init - upper_now) - (lower_init - lower_now)) / 0.35, -1, 1)
    payout_quality = clamp((row.payout - 0.92) / 0.08, 0, 1)
    return clamp((0.55 * current_water_edge + 0.45 * movement_edge) * (0.70 + 0.30 * payout_quality), -1, 1)


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
    target = (
        result.upper_team
        if result.recommendation == "上盘"
        else result.lower_team
        if result.recommendation == "下盘"
        else f"倾向{result.lean}:{result.lean_team}"
    )
    print(
        f"{match.event_id} | {format_local(match.match_time)} | {match.home} vs {match.away} | "
        f"盘口 {match.asian_line} | 推荐 {result.recommendation}({target}) | "
        f"置信度 {result.confidence}% | 完整度 {result.completeness}% | score {result.score:+.3f}"
    )
    for signal in result.signals:
        if verbose or signal.available:
            mark = "OK" if signal.available else "NA"
            print(f"  [{mark}] {signal.name}: {signal.score:+.3f} - {signal.reason}")
    for warning in result.warnings:
        print(f"  [WARN] {warning}")


PUBLIC_SOURCES = [
    {
        "name": "SPDEX",
        "url": "https://app.spdex.com/pstand/",
        "role": "默认主源：世界杯赛程、必发指数、成交量、盈亏指数、亚盘、欧赔/Kelly",
        "auth": "公开 Web 接口；非正式 API，结构可能变化",
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
            "  python3 worldcup_ah_cli.py sources\n"
            "      查看后续可接入的公开数据源。\n\n"
            "输出说明:\n"
            "  推荐 上盘/下盘: 分数超过购买阈值，给出购买方。\n"
            "  推荐 观望(倾向...): 有方向倾向，但置信度或信号一致性不足。\n"
            "  score > 0 偏上盘，score < 0 偏下盘；默认阈值为 +/-0.18。\n"
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
    subparsers = parser.add_subparsers(dest="command", required=True)

    upcoming_parser = subparsers.add_parser(
        "upcoming",
        help="列出未开赛世界杯赛程",
        description="列出 SPDEX 当前可见的未开赛世界杯比赛，并给出 T-24h/T-8h/T-4h/T-60m 等建议拉取时间。",
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

    subparsers.add_parser(
        "sources",
        help="列出可后续接入的公开数据源",
        description="列出 SPDEX、The Odds API、football-data.org、Betfair Exchange API 等可选数据源。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
        return 0

    if args.command == "predict":
        predictor = Predictor(client)
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
        return 0

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


if __name__ == "__main__":
    raise SystemExit(main())
