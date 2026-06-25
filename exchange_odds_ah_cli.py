#!/usr/bin/env python3
"""Hybrid Asian-handicap helper using The Odds API + Betfair Exchange.

This is a separate integration layer from ``worldcup_ah_cli.py``.  It keeps the
same Predictor and signal model, but replaces SPDEX-only data with:

* The Odds API: fixtures, h2h odds, spreads/handicap prices.
* Betfair Exchange API: traded volume and exchange order-book pressure.

Both providers are optional at runtime.  Without API keys, ``selftest`` still
runs a deterministic fixture through the full adapter and Predictor stack.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from worldcup_ah_cli import (
    DataError,
    EuroTrendPoint,
    HandicapRow,
    Match,
    Predictor,
    PriceVolumePoint,
    SnapshotStore,
    default_env_file_path,
    load_dotenv_file,
    normalize_line_for_spdex,
    parse_datetime_or_none,
    print_analysis,
    side_key,
    upper_lower_teams,
)


THE_ODDS_BASE_URL = "https://api.the-odds-api.com/v4"
BETFAIR_JSON_RPC = "https://api.betfair.com/exchange/betting/json-rpc/v1"
FOOTBALL_EVENT_TYPE_ID = "1"
DEFAULT_SPORT_KEY = "soccer_fifa_world_cup"
DEFAULT_REGIONS = "uk,eu,us,au"
DEFAULT_MARKETS = "h2h,spreads"
DEFAULT_BOOKMAKER_PRIORITY = (
    "pinnacle",
    "betfair_ex_uk",
    "betfair_ex_eu",
    "matchbook",
    "bet365",
    "williamhill",
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def stable_event_id(provider_id: str) -> int:
    """Map provider event ids to positive 31-bit ints accepted by Match."""
    digest = hashlib.blake2b(provider_id.encode("utf-8"), digest_size=8).hexdigest()
    return int(digest, 16) % 2_000_000_000 + 100_000_000


def canonical_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def fuzzy_team_match(left: str, right: str) -> bool:
    a = canonical_name(left)
    b = canonical_name(right)
    if not a or not b:
        return False
    return a in b or b in a


def avg(values: list[float]) -> float:
    valid = [value for value in values if value > 0]
    return sum(valid) / len(valid) if valid else 0.0


def decimal_price(value: Any) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(price) or price <= 0:
        return 0.0
    return price


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def json_request(
    url: str,
    *,
    method: str = "GET",
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: float = 20.0,
) -> Any:
    if params:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        url = f"{url}?{query}"
    payload = None
    final_headers = {
        "Accept": "application/json",
        "User-Agent": "worldcup-exchange-odds-cli/1.0",
    }
    if headers:
        final_headers.update(headers)
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, method=method, headers=final_headers)
    last_exc: BaseException | None = None
    for context in (None, ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as resp:
                raw = resp.read().decode("utf-8")
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if context is None and "CERTIFICATE_VERIFY_FAILED" in str(exc):
                continue
            if isinstance(exc, urllib.error.HTTPError):
                preview = exc.read().decode("utf-8", errors="replace")[:300]
                raise DataError(f"HTTP {exc.code} {url}: {preview}") from exc
            raise DataError(f"HTTP request failed {url}: {exc}") from exc
    else:
        raise DataError(f"HTTP request failed {url}: {last_exc}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataError(f"non-JSON response from {url}: {raw[:220]!r}") from exc


@dataclass(frozen=True)
class OddsApiEvent:
    provider_id: str
    sport_key: str
    commence_time: datetime
    home_team: str
    away_team: str
    bookmakers: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def event_id(self) -> int:
        return stable_event_id(self.provider_id)


@dataclass(frozen=True)
class BetfairRunnerSnapshot:
    selection_id: int
    name: str
    last_price: float
    total_matched: float
    available_to_back: list[tuple[float, float]]
    available_to_lay: list[tuple[float, float]]
    traded_volume: list[tuple[float, float]]


@dataclass(frozen=True)
class BetfairMarketSnapshot:
    market_id: str
    event_id: str
    event_name: str
    market_start_time: datetime | None
    runners: list[BetfairRunnerSnapshot]
    total_matched: float
    publish_time: datetime | None = None


@dataclass
class PreparedEvent:
    event: OddsApiEvent
    match: Match
    handicap_rows: list[HandicapRow]
    euro_points: list[EuroTrendPoint]
    price_points: dict[str, list[PriceVolumePoint]] = field(default_factory=dict)
    betfair_market: BetfairMarketSnapshot | None = None


class TheOddsApiClient:
    def __init__(self, api_key: str | None, *, timeout: float = 20.0) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.api_key:
            raise DataError("THE_ODDS_API_KEY is not configured")
        merged = dict(params or {})
        merged["apiKey"] = self.api_key
        return json_request(f"{THE_ODDS_BASE_URL}{path}", params=merged, timeout=self.timeout)

    def sports(self) -> list[dict[str, Any]]:
        payload = self._get("/sports/")
        if not isinstance(payload, list):
            raise DataError("The Odds API /sports returned unexpected shape")
        return [item for item in payload if isinstance(item, dict)]

    def odds(
        self,
        *,
        sport_key: str,
        regions: str,
        markets: str = DEFAULT_MARKETS,
        event_ids: list[str] | None = None,
        commence_from: datetime | None = None,
        commence_to: datetime | None = None,
        bookmakers: str | None = None,
    ) -> list[OddsApiEvent]:
        params: dict[str, Any] = {
            "regions": regions,
            "markets": markets,
            "oddsFormat": "decimal",
            "dateFormat": "iso",
            "eventIds": ",".join(event_ids) if event_ids else None,
            "commenceTimeFrom": iso_z(commence_from) if commence_from else None,
            "commenceTimeTo": iso_z(commence_to) if commence_to else None,
            "bookmakers": bookmakers,
        }
        payload = self._get(f"/sports/{urllib.parse.quote(sport_key)}/odds", params)
        if not isinstance(payload, list):
            raise DataError("The Odds API /odds returned unexpected shape")
        return [parse_odds_api_event(item, sport_key) for item in payload if isinstance(item, dict)]

    def event_odds(
        self,
        *,
        sport_key: str,
        event_id: str,
        regions: str,
        markets: str = DEFAULT_MARKETS,
        bookmakers: str | None = None,
    ) -> OddsApiEvent:
        events = self.odds(
            sport_key=sport_key,
            regions=regions,
            markets=markets,
            event_ids=[event_id],
            bookmakers=bookmakers,
        )
        if not events:
            raise DataError(f"The Odds API event not found: {event_id}")
        return events[0]


class BetfairExchangeClient:
    """Small JSON-RPC wrapper for Betfair Exchange market data.

    Requires ``BETFAIR_APP_KEY`` and either ``BETFAIR_SESSION_TOKEN`` or
    ``BETFAIR_SSOID``.  Certificate login is intentionally out of scope for this
    helper; create the session token outside the script.
    """

    def __init__(
        self,
        *,
        app_key: str | None,
        session_token: str | None,
        timeout: float = 20.0,
    ) -> None:
        self.app_key = (app_key or "").strip()
        self.session_token = (session_token or "").strip()
        self.timeout = timeout
        self._rpc_id = 0

    @property
    def configured(self) -> bool:
        return bool(self.app_key and self.session_token)

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        if not self.configured:
            raise DataError("BETFAIR_APP_KEY and BETFAIR_SESSION_TOKEN are required")
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": f"SportsAPING/v1.0/{method}",
            "params": params,
            "id": self._rpc_id,
        }
        headers = {
            "X-Application": self.app_key,
            "X-Authentication": self.session_token,
        }
        data = json_request(
            BETFAIR_JSON_RPC,
            method="POST",
            headers=headers,
            body=payload,
            timeout=self.timeout,
        )
        if isinstance(data, dict) and data.get("error"):
            raise DataError(f"Betfair {method} error: {data['error']}")
        if not isinstance(data, dict) or "result" not in data:
            raise DataError(f"Betfair {method} returned unexpected shape")
        return data["result"]

    def list_market_catalogue(
        self,
        *,
        from_time: datetime,
        to_time: datetime,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        params = {
            "filter": {
                "eventTypeIds": [FOOTBALL_EVENT_TYPE_ID],
                "marketTypeCodes": ["MATCH_ODDS"],
                "marketStartTime": {
                    "from": iso_z(from_time),
                    "to": iso_z(to_time),
                },
            },
            "marketProjection": ["EVENT", "RUNNER_METADATA", "MARKET_START_TIME"],
            "sort": "FIRST_TO_START",
            "maxResults": str(max_results),
        }
        result = self._rpc("listMarketCatalogue", params)
        if not isinstance(result, list):
            raise DataError("Betfair listMarketCatalogue returned unexpected shape")
        return [item for item in result if isinstance(item, dict)]

    def list_market_book(self, market_id: str) -> BetfairMarketSnapshot:
        params = {
            "marketIds": [market_id],
            "priceProjection": {
                "priceData": ["EX_BEST_OFFERS", "EX_ALL_OFFERS", "EX_TRADED"],
                "virtualise": True,
                "rolloverStakes": False,
            },
        }
        result = self._rpc("listMarketBook", params)
        if not isinstance(result, list) or not result:
            raise DataError("Betfair listMarketBook returned no market")
        return parse_betfair_market_book(result[0], catalogue=None)

    def find_match_odds_market(
        self,
        event: OddsApiEvent,
        *,
        search_window: timedelta = timedelta(hours=8),
    ) -> BetfairMarketSnapshot | None:
        start = event.commence_time - search_window
        end = event.commence_time + search_window
        catalogues = self.list_market_catalogue(from_time=start, to_time=end)
        best: dict[str, Any] | None = None
        best_score = -1.0
        for catalogue in catalogues:
            score = score_betfair_catalogue_match(catalogue, event)
            if score > best_score:
                best = catalogue
                best_score = score
        if not best or best_score < 0.65:
            return None
        market_id = str(best.get("marketId", ""))
        if not market_id:
            return None
        book = self.list_market_book(market_id)
        return merge_betfair_catalogue(book, best)


def parse_odds_api_event(item: dict[str, Any], sport_key: str) -> OddsApiEvent:
    provider_id = str(item.get("id", "")).strip()
    if not provider_id:
        raise DataError("The Odds API event missing id")
    commence_time = parse_datetime_or_none(item.get("commence_time"))
    if commence_time is None:
        raise DataError(f"The Odds API event missing commence_time: {provider_id}")
    return OddsApiEvent(
        provider_id=provider_id,
        sport_key=sport_key,
        commence_time=commence_time,
        home_team=str(item.get("home_team", "")),
        away_team=str(item.get("away_team", "")),
        bookmakers=[b for b in item.get("bookmakers", []) if isinstance(b, dict)],
        raw=item,
    )


def iter_market_outcomes(event: OddsApiEvent, market_key: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for bookmaker in event.bookmakers:
        markets = bookmaker.get("markets", [])
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict) or market.get("key") != market_key:
                continue
            for outcome in market.get("outcomes", []):
                if isinstance(outcome, dict):
                    rows.append((bookmaker, outcome))
    return rows


def average_h2h_prices(event: OddsApiEvent) -> tuple[float, float, float]:
    home_prices: list[float] = []
    draw_prices: list[float] = []
    away_prices: list[float] = []
    for _bookmaker, outcome in iter_market_outcomes(event, "h2h"):
        name = str(outcome.get("name", ""))
        price = decimal_price(outcome.get("price"))
        if not price:
            continue
        if fuzzy_team_match(name, event.home_team):
            home_prices.append(price)
        elif fuzzy_team_match(name, event.away_team):
            away_prices.append(price)
        elif name.lower() in ("draw", "tie"):
            draw_prices.append(price)
    return avg(home_prices), avg(draw_prices), avg(away_prices)


def spread_rows(event: OddsApiEvent) -> list[tuple[dict[str, Any], float, float, float]]:
    rows: list[tuple[dict[str, Any], float, float, float]] = []
    for bookmaker in event.bookmakers:
        by_point: dict[float, dict[str, float]] = {}
        for bm, outcome in iter_market_outcomes(event, "spreads"):
            if bm is not bookmaker:
                continue
            name = str(outcome.get("name", ""))
            point_raw = outcome.get("point")
            try:
                point = float(point_raw)
            except (TypeError, ValueError):
                continue
            price = decimal_price(outcome.get("price"))
            if not price:
                continue
            if fuzzy_team_match(name, event.home_team):
                by_point.setdefault(point, {})["home"] = price
            elif fuzzy_team_match(name, event.away_team):
                by_point.setdefault(-point, {})["away"] = price
        for home_point, prices in by_point.items():
            home_price = prices.get("home", 0.0)
            away_price = prices.get("away", 0.0)
            if home_price > 0 and away_price > 0:
                rows.append((bookmaker, home_point, home_price, away_price))
                break
    return rows


def choose_main_spread_line(event: OddsApiEvent) -> float:
    rows = spread_rows(event)
    if not rows:
        return 0.0
    priority = {key: index for index, key in enumerate(DEFAULT_BOOKMAKER_PRIORITY)}
    rows.sort(key=lambda row: (priority.get(str(row[0].get("key", "")), 99), abs(row[1])))
    return rows[0][1]


def odds_api_handicap_rows(event: OddsApiEvent, asian_line: str) -> list[HandicapRow]:
    target = line_to_float(asian_line)
    out: list[HandicapRow] = []
    for index, (bookmaker, home_point, home_price, away_price) in enumerate(spread_rows(event)):
        if abs(home_point - target) > 0.001:
            continue
        title = str(bookmaker.get("title") or bookmaker.get("key") or f"bookmaker-{index + 1}")
        out.append(
            HandicapRow(
                bookmaker_id=index + 1,
                name=title,
                sec_a=home_price,
                sec_b=away_price,
                init_sec_a=home_price,
                init_sec_b=away_price,
                payout=0.0,
                update_time=parse_datetime_or_none(bookmaker.get("last_update")),
                source="external",
                init_line=home_point,
                latest_line=home_point,
                init_line_known=True,
                latest_line_known=True,
            )
        )
    return out


def line_to_float(value: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def format_line(value: float) -> str:
    return normalize_line_for_spdex(f"{value:.3f}".rstrip("0").rstrip("."))


def normalized_two_way_prob(price_a: float, price_b: float) -> tuple[float, float]:
    pa = 1.0 / price_a if price_a > 0 else 0.0
    pb = 1.0 / price_b if price_b > 0 else 0.0
    total = pa + pb
    if total <= 0:
        return 0.0, 0.0
    return pa / total, pb / total


def normalize_three_way(home: float, draw: float, away: float) -> tuple[float, float, float]:
    probs = [1.0 / value if value > 0 else 0.0 for value in (home, draw, away)]
    total = sum(probs)
    if total <= 0:
        return 0.0, 0.0, 0.0
    return probs[0] / total, probs[1] / total, probs[2] / total


def fair_line_depth_from_h2h(home: float, draw: float, away: float, line: float) -> float:
    home_prob, draw_prob, away_prob = normalize_three_way(home, draw, away)
    upper_prob = home_prob if line <= 0 else away_prob
    lower_prob = away_prob if line <= 0 else home_prob
    dominance = max(upper_prob - lower_prob, 0.0)
    draw_drag = clamp((draw_prob - 0.18) / 0.18, 0, 1)
    return clamp((dominance * 2.1) * (1 - 0.30 * draw_drag), 0.0, 2.5)


def build_match_from_odds_event(
    event: OddsApiEvent,
    *,
    betfair_market: BetfairMarketSnapshot | None = None,
) -> tuple[Match, list[HandicapRow], list[EuroTrendPoint], dict[str, list[PriceVolumePoint]]]:
    line = choose_main_spread_line(event)
    asian_line = format_line(line)
    rows = odds_api_handicap_rows(event, asian_line)
    home_spread_price = avg([row.sec_a for row in rows])
    away_spread_price = avg([row.sec_b for row in rows])
    home_h2h, draw_h2h, away_h2h = average_h2h_prices(event)

    raw: dict[str, Any] = {
        "_source": "exchange_odds",
        "ExternalProvider": "the-odds-api",
        "ExternalEventId": event.provider_id,
        "SportKey": event.sport_key,
        "EventId": event.event_id,
        "HomeTeam": event.home_team,
        "AwayTeam": event.away_team,
        "MatchTime": event.commence_time.isoformat(),
        "AsianAvrLet": asian_line,
        "AsianAvrHome": home_spread_price,
        "AsianAvrAway": away_spread_price,
        "EuroAvrHome": home_h2h,
        "EuroAvrDraw": draw_h2h,
        "EuroAvrAway": away_h2h,
        "KellyHome": home_h2h,
        "KellyDraw": draw_h2h,
        "KellyAway": away_h2h,
        "OddsApiSpreadHomePrice": home_spread_price,
        "OddsApiSpreadAwayPrice": away_spread_price,
        "OddsApiH2hHomePrice": home_h2h,
        "OddsApiH2hDrawPrice": draw_h2h,
        "OddsApiH2hAwayPrice": away_h2h,
        "ExternalFairLineDepth": fair_line_depth_from_h2h(home_h2h, draw_h2h, away_h2h, line),
    }
    match = Match(
        event_id=event.event_id,
        match_time=event.commence_time,
        home=event.home_team,
        away=event.away_team,
        league_id=None,
        league_name=event.sport_key,
        asian_line=asian_line,
        is_stop_update=False,
        raw=raw,
    )
    match = add_external_upper_lower_fields(match)
    price_points: dict[str, list[PriceVolumePoint]] = {}
    if betfair_market:
        match, price_points = augment_match_with_betfair(match, betfair_market)
    euro_points = []
    if home_h2h > 0 and away_h2h > 0:
        euro_points = [
            EuroTrendPoint(
                refresh_time=None,
                home_price=home_h2h,
                draw_price=draw_h2h,
                away_price=away_h2h,
                home_kelly=home_h2h,
                draw_kelly=draw_h2h,
                away_kelly=away_h2h,
            )
        ]
    return match, rows, euro_points, price_points


def add_external_upper_lower_fields(match: Match) -> Match:
    upper, lower = upper_lower_teams(match)
    raw = dict(match.raw)
    upper_key = side_key(match, upper)
    lower_key = side_key(match, lower)
    raw["OddsApiSpreadUpperPrice"] = raw.get(f"OddsApiSpread{upper_key}Price", 0.0)
    raw["OddsApiSpreadLowerPrice"] = raw.get(f"OddsApiSpread{lower_key}Price", 0.0)
    raw["OddsApiH2hUpperPrice"] = raw.get(f"OddsApiH2h{upper_key}Price", 0.0)
    raw["OddsApiH2hLowerPrice"] = raw.get(f"OddsApiH2h{lower_key}Price", 0.0)
    if raw.get("EuroAvrHome", 0.0) and raw.get("EuroAvrAway", 0.0):
        home_prob, _draw_prob, away_prob = normalize_three_way(
            float(raw.get("EuroAvrHome", 0.0)),
            float(raw.get("EuroAvrDraw", 0.0)),
            float(raw.get("EuroAvrAway", 0.0)),
        )
        raw["ExternalPowerEdge"] = clamp((home_prob - away_prob) * (1 if upper_key == "Home" else -1) * 2.0, -1, 1)
    return Match(
        event_id=match.event_id,
        match_time=match.match_time,
        home=match.home,
        away=match.away,
        league_id=match.league_id,
        league_name=match.league_name,
        asian_line=match.asian_line,
        is_stop_update=match.is_stop_update,
        raw=raw,
    )


def score_betfair_catalogue_match(catalogue: dict[str, Any], event: OddsApiEvent) -> float:
    event_info = catalogue.get("event") if isinstance(catalogue.get("event"), dict) else {}
    name = str(event_info.get("name") or catalogue.get("marketName") or "")
    runner_names = [str(r.get("runnerName", "")) for r in catalogue.get("runners", []) if isinstance(r, dict)]
    names = [name, *runner_names]
    home_hit = any(fuzzy_team_match(event.home_team, candidate) for candidate in names)
    away_hit = any(fuzzy_team_match(event.away_team, candidate) for candidate in names)
    score = 0.0
    if home_hit:
        score += 0.45
    if away_hit:
        score += 0.45
    start = parse_datetime_or_none(catalogue.get("marketStartTime"))
    if start:
        delta_hours = abs((start - event.commence_time).total_seconds()) / 3600.0
        score += max(0.0, 0.10 - delta_hours * 0.02)
    return score


def parse_price_size_rows(rows: Any) -> list[tuple[float, float]]:
    if not isinstance(rows, list):
        return []
    out: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = decimal_price(row.get("price"))
        size = decimal_price(row.get("size"))
        if price > 0 and size > 0:
            out.append((price, size))
    return out


def parse_betfair_market_book(
    market_book: dict[str, Any],
    catalogue: dict[str, Any] | None,
) -> BetfairMarketSnapshot:
    catalogue_runners: dict[int, str] = {}
    if catalogue:
        for runner in catalogue.get("runners", []):
            if not isinstance(runner, dict):
                continue
            try:
                selection_id = int(runner.get("selectionId"))
            except (TypeError, ValueError):
                continue
            catalogue_runners[selection_id] = str(runner.get("runnerName", ""))

    runners: list[BetfairRunnerSnapshot] = []
    for runner in market_book.get("runners", []):
        if not isinstance(runner, dict):
            continue
        try:
            selection_id = int(runner.get("selectionId"))
        except (TypeError, ValueError):
            continue
        ex = runner.get("ex") if isinstance(runner.get("ex"), dict) else {}
        traded = parse_price_size_rows(ex.get("tradedVolume"))
        runners.append(
            BetfairRunnerSnapshot(
                selection_id=selection_id,
                name=catalogue_runners.get(selection_id, str(selection_id)),
                last_price=decimal_price(runner.get("lastPriceTraded")),
                total_matched=sum(size for _price, size in traded),
                available_to_back=parse_price_size_rows(ex.get("availableToBack")),
                available_to_lay=parse_price_size_rows(ex.get("availableToLay")),
                traded_volume=traded,
            )
        )

    event_info = catalogue.get("event") if catalogue and isinstance(catalogue.get("event"), dict) else {}
    publish_time = None
    if market_book.get("publishTime"):
        try:
            publish_time = datetime.fromtimestamp(float(market_book["publishTime"]) / 1000.0, tz=timezone.utc)
        except (TypeError, ValueError):
            publish_time = None
    return BetfairMarketSnapshot(
        market_id=str(market_book.get("marketId", "")),
        event_id=str(event_info.get("id", "")),
        event_name=str(event_info.get("name", "")),
        market_start_time=parse_datetime_or_none(catalogue.get("marketStartTime")) if catalogue else None,
        runners=runners,
        total_matched=decimal_price(market_book.get("totalMatched")),
        publish_time=publish_time,
    )


def merge_betfair_catalogue(book: BetfairMarketSnapshot, catalogue: dict[str, Any]) -> BetfairMarketSnapshot:
    enriched = parse_betfair_market_book(
        {
            "marketId": book.market_id,
            "runners": [
                {
                    "selectionId": runner.selection_id,
                    "lastPriceTraded": runner.last_price,
                    "ex": {
                        "availableToBack": [
                            {"price": price, "size": size} for price, size in runner.available_to_back
                        ],
                        "availableToLay": [
                            {"price": price, "size": size} for price, size in runner.available_to_lay
                        ],
                        "tradedVolume": [
                            {"price": price, "size": size} for price, size in runner.traded_volume
                        ],
                    },
                }
                for runner in book.runners
            ],
            "totalMatched": book.total_matched,
            "publishTime": int(book.publish_time.timestamp() * 1000) if book.publish_time else None,
        },
        catalogue,
    )
    return enriched


def runner_for_team(market: BetfairMarketSnapshot, team: str) -> BetfairRunnerSnapshot | None:
    for runner in market.runners:
        if fuzzy_team_match(team, runner.name):
            return runner
    return None


def runner_best_price(runner: BetfairRunnerSnapshot | None) -> float:
    if not runner:
        return 0.0
    if runner.available_to_back:
        return runner.available_to_back[0][0]
    return runner.last_price


def price_points_from_runner(runner: BetfairRunnerSnapshot, publish_time: datetime | None) -> list[PriceVolumePoint]:
    points: list[PriceVolumePoint] = []
    for price, size in runner.available_to_back[:3]:
        points.append(PriceVolumePoint(price=price, volume=size, update_time=publish_time, attr="买"))
    for price, size in runner.available_to_lay[:3]:
        points.append(PriceVolumePoint(price=price, volume=size, update_time=publish_time, attr="卖"))
    if len(points) < 2:
        for price, size in runner.traded_volume[:6]:
            points.append(PriceVolumePoint(price=price, volume=size, update_time=publish_time, attr=None))
    return points


def augment_match_with_betfair(
    match: Match,
    market: BetfairMarketSnapshot,
) -> tuple[Match, dict[str, list[PriceVolumePoint]]]:
    home = runner_for_team(market, match.home)
    away = runner_for_team(market, match.away)
    draw = next((runner for runner in market.runners if runner.name.lower() in ("the draw", "draw")), None)
    total = sum(r.total_matched for r in (home, away, draw) if r)
    raw = dict(match.raw)
    raw["BetfairMarketId"] = market.market_id
    raw["BetfairTotalMatched"] = market.total_matched
    if home:
        raw["BfAmountHome"] = home.total_matched
        raw["BfOddsHome"] = runner_best_price(home)
        raw["BfIndexHome"] = (home.total_matched / total * 100.0) if total > 0 else 0.0
    if away:
        raw["BfAmountAway"] = away.total_matched
        raw["BfOddsAway"] = runner_best_price(away)
        raw["BfIndexAway"] = (away.total_matched / total * 100.0) if total > 0 else 0.0
    if draw:
        raw["BfAmountDraw"] = draw.total_matched
        raw["BfOddsDraw"] = runner_best_price(draw)
        raw["BfIndexDraw"] = (draw.total_matched / total * 100.0) if total > 0 else 0.0
    raw.setdefault("BfPayoutHome", 0.0)
    raw.setdefault("BfPayoutAway", 0.0)
    raw.setdefault("BfPayoutDraw", 0.0)

    points: dict[str, list[PriceVolumePoint]] = {}
    if home:
        points["home"] = price_points_from_runner(home, market.publish_time)
    if away:
        points["away"] = price_points_from_runner(away, market.publish_time)

    return (
        Match(
            event_id=match.event_id,
            match_time=match.match_time,
            home=match.home,
            away=match.away,
            league_id=match.league_id,
            league_name=match.league_name,
            asian_line=match.asian_line,
            is_stop_update=match.is_stop_update,
            raw=raw,
        ),
        points,
    )


class HybridExchangeOddsClient:
    def __init__(
        self,
        odds_client: TheOddsApiClient | None = None,
        betfair_client: BetfairExchangeClient | None = None,
        *,
        use_betfair: bool = True,
    ) -> None:
        self.odds_client = odds_client
        self.betfair_client = betfair_client
        self.use_betfair = use_betfair
        self.prepared: dict[int, PreparedEvent] = {}

    def upcoming(
        self,
        *,
        sport_key: str,
        regions: str,
        hours: float,
        limit: int,
        bookmakers: str | None = None,
    ) -> list[OddsApiEvent]:
        if not self.odds_client:
            raise DataError("The Odds API client is not configured")
        now = datetime.now(timezone.utc)
        events = self.odds_client.odds(
            sport_key=sport_key,
            regions=regions,
            commence_from=now,
            commence_to=now + timedelta(hours=hours),
            bookmakers=bookmakers,
        )
        return sorted(events, key=lambda event: event.commence_time)[:limit]

    def prepare_event(
        self,
        event: OddsApiEvent,
        *,
        search_betfair: bool = True,
    ) -> PreparedEvent:
        market = None
        if self.use_betfair and search_betfair and self.betfair_client and self.betfair_client.configured:
            try:
                market = self.betfair_client.find_match_odds_market(event)
            except DataError:
                market = None
        match, rows, euro_points, price_points = build_match_from_odds_event(event, betfair_market=market)
        prepared = PreparedEvent(
            event=event,
            match=match,
            handicap_rows=rows,
            euro_points=euro_points,
            price_points=price_points,
            betfair_market=market,
        )
        self.prepared[match.event_id] = prepared
        return prepared

    def event_by_provider_id(
        self,
        *,
        sport_key: str,
        provider_event_id: str,
        regions: str,
        bookmakers: str | None = None,
    ) -> OddsApiEvent:
        if not self.odds_client:
            raise DataError("The Odds API client is not configured")
        return self.odds_client.event_odds(
            sport_key=sport_key,
            event_id=provider_event_id,
            regions=regions,
            bookmakers=bookmakers,
        )

    def handicap_list(self, event_id: int, _asian_line: str) -> list[HandicapRow]:
        prepared = self.prepared.get(event_id)
        return list(prepared.handicap_rows) if prepared else []

    def handicap_detail(self, _event_id: int, _asian_line: str, _bookmaker_id: int) -> list[HandicapRow]:
        return []

    def euro_trend(self, event_id: int) -> list[EuroTrendPoint]:
        prepared = self.prepared.get(event_id)
        return list(prepared.euro_points) if prepared else []

    def price_volume(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        prepared = self.prepared.get(event_id)
        if not prepared:
            raise DataError("hybrid event is not prepared")
        points = prepared.price_points.get(selection, [])
        if not points:
            raise DataError("Betfair Exchange traded/order-book data unavailable")
        return list(points)


def print_event(event: OddsApiEvent) -> None:
    line = choose_main_spread_line(event)
    h2h_home, h2h_draw, h2h_away = average_h2h_prices(event)
    print(
        f"{event.provider_id} | {event.event_id} | {event.commence_time.astimezone().strftime('%Y-%m-%d %H:%M')} | "
        f"{event.home_team} vs {event.away_team} | spread {format_line(line)} | "
        f"h2h {h2h_home:.2f}/{h2h_draw:.2f}/{h2h_away:.2f}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The Odds API + Betfair Exchange 版亚盘助手，复用 worldcup_ah_cli.Predictor。",
    )
    parser.add_argument("--env-file", default=None, help="加载 .env（默认仓库根 .env）")
    parser.add_argument("--no-dotenv", action="store_true", help="不加载 .env")
    parser.add_argument("--timeout", type=float, default=20.0, help="HTTP 超时秒")
    parser.add_argument("--sport", default=DEFAULT_SPORT_KEY, help=f"The Odds API sport key，默认 {DEFAULT_SPORT_KEY}")
    parser.add_argument("--regions", default=DEFAULT_REGIONS, help=f"The Odds API regions，默认 {DEFAULT_REGIONS}")
    parser.add_argument("--bookmakers", default=None, help="The Odds API bookmakers 过滤，例如 pinnacle,betfair_ex_uk")
    parser.add_argument("--no-betfair", action="store_true", help="不查 Betfair，只用 The Odds API")
    parser.add_argument("--snapshot-dir", default=".exchange_odds_snapshots", help="快照目录")

    sub = parser.add_subparsers(dest="command", required=True)

    sources = sub.add_parser("sources", help="检查配置和数据源可用性")
    sources.set_defaults(func=cmd_sources)

    upcoming = sub.add_parser("upcoming", help="列出未来比赛")
    upcoming.add_argument("--hours", type=float, default=48.0)
    upcoming.add_argument("--limit", type=int, default=20)
    upcoming.set_defaults(func=cmd_upcoming)

    predict = sub.add_parser("predict", help="按 The Odds API event id 预测")
    predict.add_argument("--event-id", required=True, help="The Odds API event id")
    predict.add_argument("--json", action="store_true", help="输出 JSON")
    predict.add_argument("--verbose", action="store_true")
    predict.set_defaults(func=cmd_predict)

    selftest = sub.add_parser("selftest", help="不用外部 API，运行内置端到端自检")
    selftest.add_argument("--verbose", action="store_true")
    selftest.set_defaults(func=cmd_selftest)
    return parser


def build_clients(args: argparse.Namespace) -> HybridExchangeOddsClient:
    odds_client = TheOddsApiClient(os.environ.get("THE_ODDS_API_KEY"), timeout=args.timeout)
    betfair_client = BetfairExchangeClient(
        app_key=os.environ.get("BETFAIR_APP_KEY"),
        session_token=os.environ.get("BETFAIR_SESSION_TOKEN") or os.environ.get("BETFAIR_SSOID"),
        timeout=args.timeout,
    )
    return HybridExchangeOddsClient(
        odds_client=odds_client,
        betfair_client=betfair_client,
        use_betfair=not args.no_betfair,
    )


def cmd_sources(args: argparse.Namespace) -> int:
    client = build_clients(args)
    odds_status = "configured" if client.odds_client and client.odds_client.configured else "missing THE_ODDS_API_KEY"
    bf_status = (
        "configured"
        if client.betfair_client and client.betfair_client.configured
        else "missing BETFAIR_APP_KEY/BETFAIR_SESSION_TOKEN"
    )
    print(f"The Odds API: {odds_status}")
    print(f"Betfair Exchange: {bf_status}")
    if client.odds_client and client.odds_client.configured:
        sports = client.odds_client.sports()
        matching = [s for s in sports if args.sport in (s.get("key"), s.get("group"))]
        print(f"The Odds API sports: {len(sports)} loaded; selected={args.sport}; matches={len(matching)}")
    else:
        print("The Odds API live check skipped.")
    if client.betfair_client and client.betfair_client.configured:
        now = datetime.now(timezone.utc)
        markets = client.betfair_client.list_market_catalogue(
            from_time=now,
            to_time=now + timedelta(hours=24),
            max_results=5,
        )
        print(f"Betfair football MATCH_ODDS markets next 24h: {len(markets)}")
    else:
        print("Betfair live check skipped.")
    return 0


def cmd_upcoming(args: argparse.Namespace) -> int:
    client = build_clients(args)
    events = client.upcoming(
        sport_key=args.sport,
        regions=args.regions,
        hours=args.hours,
        limit=args.limit,
        bookmakers=args.bookmakers,
    )
    if not events:
        print("没有找到比赛。")
        return 0
    for event in events:
        print_event(event)
    return 0


def cmd_predict(args: argparse.Namespace) -> int:
    client = build_clients(args)
    event = client.event_by_provider_id(
        sport_key=args.sport,
        provider_event_id=args.event_id,
        regions=args.regions,
        bookmakers=args.bookmakers,
    )
    prepared = client.prepare_event(event)
    predictor = Predictor(client, SnapshotStore(args.snapshot_dir))
    result = predictor.analyze(prepared.match)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_analysis(result, verbose=args.verbose)
        if prepared.betfair_market:
            print(f"  [INFO] Betfair market_id={prepared.betfair_market.market_id}")
        else:
            print("  [INFO] 未匹配 Betfair market，成交走势不可用或仅用 The Odds API。")
    return 0


def sample_odds_event() -> OddsApiEvent:
    raw = {
        "id": "fixture-brazil-morocco",
        "sport_key": "soccer_fifa_world_cup",
        "commence_time": "2026-06-20T12:00:00Z",
        "home_team": "Brazil",
        "away_team": "Morocco",
        "bookmakers": [
            {
                "key": "pinnacle",
                "title": "Pinnacle",
                "last_update": "2026-06-20T08:00:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Brazil", "price": 1.55},
                            {"name": "Draw", "price": 4.25},
                            {"name": "Morocco", "price": 6.80},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Brazil", "price": 1.91, "point": -1.0},
                            {"name": "Morocco", "price": 1.97, "point": 1.0},
                        ],
                    },
                ],
            },
            {
                "key": "bet365",
                "title": "Bet365",
                "last_update": "2026-06-20T08:02:00Z",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Brazil", "price": 1.57},
                            {"name": "Draw", "price": 4.10},
                            {"name": "Morocco", "price": 6.60},
                        ],
                    },
                    {
                        "key": "spreads",
                        "outcomes": [
                            {"name": "Brazil", "price": 1.88, "point": -1.0},
                            {"name": "Morocco", "price": 2.00, "point": 1.0},
                        ],
                    },
                ],
            },
        ],
    }
    return parse_odds_api_event(raw, "soccer_fifa_world_cup")


def sample_betfair_market() -> BetfairMarketSnapshot:
    publish_time = datetime(2026, 6, 20, 8, 5, tzinfo=timezone.utc)
    return BetfairMarketSnapshot(
        market_id="1.234567890",
        event_id="bf-fixture-1",
        event_name="Brazil v Morocco",
        market_start_time=datetime(2026, 6, 20, 12, 0, tzinfo=timezone.utc),
        publish_time=publish_time,
        total_matched=1_500_000.0,
        runners=[
            BetfairRunnerSnapshot(
                selection_id=10,
                name="Brazil",
                last_price=1.56,
                total_matched=980_000.0,
                available_to_back=[(1.55, 80_000.0), (1.54, 42_000.0)],
                available_to_lay=[(1.57, 26_000.0), (1.58, 18_000.0)],
                traded_volume=[(1.50, 200_000.0), (1.55, 350_000.0)],
            ),
            BetfairRunnerSnapshot(
                selection_id=20,
                name="The Draw",
                last_price=4.20,
                total_matched=310_000.0,
                available_to_back=[(4.10, 20_000.0)],
                available_to_lay=[(4.30, 22_000.0)],
                traded_volume=[(4.00, 80_000.0), (4.20, 110_000.0)],
            ),
            BetfairRunnerSnapshot(
                selection_id=30,
                name="Morocco",
                last_price=6.80,
                total_matched=210_000.0,
                available_to_back=[(6.60, 18_000.0), (6.40, 10_000.0)],
                available_to_lay=[(7.00, 58_000.0), (7.20, 35_000.0)],
                traded_volume=[(6.20, 40_000.0), (6.80, 70_000.0)],
            ),
        ],
    )


def run_selftest(verbose: bool = False) -> None:
    event = sample_odds_event()
    market = sample_betfair_market()
    client = HybridExchangeOddsClient(use_betfair=False)
    match, rows, euro_points, price_points = build_match_from_odds_event(event, betfair_market=market)
    prepared = PreparedEvent(
        event=event,
        match=match,
        handicap_rows=rows,
        euro_points=euro_points,
        price_points=price_points,
        betfair_market=market,
    )
    client.prepared[match.event_id] = prepared
    result = Predictor(client, None).analyze(match)
    if match.asian_line != "-1":
        raise AssertionError(f"expected -1 line, got {match.asian_line}")
    if not rows or len(rows) < 2:
        raise AssertionError("expected at least two external spread rows")
    if not result.signals:
        raise AssertionError("expected predictor signals")
    trade_signal = next(signal for signal in result.signals if signal.name == "必发成交走势")
    external_signal = next(signal for signal in result.signals if signal.name == "外部赔率/实力校验")
    if not trade_signal.available:
        raise AssertionError("expected Betfair trade/order-book signal to be available")
    if not external_signal.available:
        raise AssertionError("expected The Odds API external consensus signal to be available")
    degraded_match, degraded_rows, degraded_euro, degraded_points = build_match_from_odds_event(event)
    degraded = PreparedEvent(event, degraded_match, degraded_rows, degraded_euro, degraded_points)
    client.prepared[degraded_match.event_id] = degraded
    degraded_result = Predictor(client, None).analyze(degraded_match)
    degraded_trade = next(signal for signal in degraded_result.signals if signal.name == "必发成交走势")
    if degraded_trade.available:
        raise AssertionError("expected trade signal to be unavailable without Betfair")
    if verbose:
        print_analysis(result, verbose=True)
        print("\n--- no Betfair degradation ---")
        print_analysis(degraded_result, verbose=True)


def cmd_selftest(args: argparse.Namespace) -> int:
    run_selftest(verbose=args.verbose)
    print("selftest OK: The Odds API adapter + Betfair adapter + Predictor all ran.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv_file(Path(args.env_file) if args.env_file else default_env_file_path())
    try:
        return args.func(args)
    except DataError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
