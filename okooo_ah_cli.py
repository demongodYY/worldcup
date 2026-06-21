#!/usr/bin/env python3
"""Okooo / 澳客 data-source CLI for the World Cup Asian-handicap predictor.

The program intentionally keeps Okooo crawling/parsing in this file and reuses
``worldcup_ah_cli.Predictor`` for the recommendation algorithm.

子命令 ``validate-snapshots``：只读 ``.okooo_snapshots`` 下 jsonl，用当前 ``Predictor``
重放并对照 ``tools/okooo_validate_scores.json`` 中的已知比分（可用 ``--scores-json`` 覆盖），
不访问澳客网（与 ``tools/backtest_okooo_snapshots.py --replay`` 一致）。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import ssl
import sys
import unicodedata
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

# 本脚本所在目录即仓库根；默认快照与比分表相对此目录解析，避免当前工作目录不在仓库根时找不到路径。
_REPO_ROOT = Path(__file__).resolve().parent

from worldcup_ah_cli import (
    DataError,
    EuroTrendPoint,
    HandicapRow,
    Match,
    OkoooSnapshotReplayClient,
    Predictor,
    PriceVolumePoint,
    SCHEDULE_WINDOWS,
    ScheduledTask,
    SnapshotStore,
    clamp,
    default_env_file_path,
    euro_trend_point_to_dict,
    format_local,
    handicap_row_to_dict,
    is_ssl_verify_error,
    line_depth,
    line_value,
    load_dotenv_file,
    match_from_dict,
    normalize_line_for_spdex,
    price_volume_point_to_dict,
    print_analysis,
    print_snapshot_saved,
    print_snapshot_trend,
    side_key,
    upper_lower_teams,
)


OKOOO_BASE_URL = "https://www.okooo.cn"
OKOOO_DEFAULT_ISSUE = "dqjc"
OKOOO_TZ = ZoneInfo("Asia/Shanghai")
OKOOO_SNAPSHOT_DIR = str(_REPO_ROOT / ".okooo_snapshots")

# 默认复盘比分表；编辑该 JSON 即可增删赛果，无需改代码
DEFAULT_OKOOO_VALIDATE_SCORES_PATH = _REPO_ROOT / "tools" / "okooo_validate_scores.json"


def load_okooo_validate_scores(path: Path | None) -> dict[int, tuple[int, int]]:
    """加载 ``event_id -> (主队进球, 客队进球)``。``path`` 为 ``None`` 时使用 ``DEFAULT_OKOOO_VALIDATE_SCORES_PATH``。"""
    effective = path if path is not None else DEFAULT_OKOOO_VALIDATE_SCORES_PATH
    if not effective.is_file():
        raise FileNotFoundError(f"比分表文件不存在: {effective}")
    data = json.loads(effective.read_text(encoding="utf-8"))
    out: dict[int, tuple[int, int]] = {}
    for k, v in data.items():
        if str(k).startswith("_"):
            continue
        eid = int(k)
        if isinstance(v, (list, tuple)) and len(v) >= 2:
            out[eid] = (int(v[0]), int(v[1]))
    return out


def _replay_margin_for_upper(h: int, a: int, line: float, upper: str, home: str, away: str) -> float:
    if upper == home:
        return float(h - a) + line
    if upper == away:
        return float(a - h) - line
    raise ValueError(f"upper_team {upper!r} not in {home!r} / {away!r}")


def _replay_recommendation_outcome(
    rec: str, upper: str, lower: str, home: str, away: str, line_txt: str, hg: int, ag: int
) -> tuple[str, float]:
    if rec == "观望":
        return "na", 0.0
    line = line_value(line_txt)
    m = _replay_margin_for_upper(hg, ag, line, upper, home, away)
    eps = 1e-9
    if rec == "上盘":
        if m > eps:
            return "hit", m
        if m < -eps:
            return "miss", m
        return "push", m
    if rec == "下盘":
        if m < -eps:
            return "hit", m
        if m > eps:
            return "miss", m
        return "push", m
    return "na", m


def _text_display_width(s: str) -> int:
    """终端常见等宽字体下的大致显示宽度（中日韩等宽为 2）。

    对 east_asian_width 为 A（模糊）的字符按 2 计，贴近多数中文环境下终端对「·、」等符号的实际占位。
    """
    w = 0
    for ch in s:
        ea = unicodedata.east_asian_width(ch)
        if ea in ("F", "W"):
            w += 2
        elif ea == "A":
            w += 2
        else:
            w += 1
    return w


def _pad_display_cell(s: str, width: int, *, align: str = "left") -> str:
    pad = max(0, width - _text_display_width(s))
    if align == "right":
        return " " * pad + s
    return s + " " * pad


def _truncate_display(s: str, max_width: int) -> str:
    if _text_display_width(s) <= max_width:
        return s
    out: list[str] = []
    w = 0
    for ch in s:
        ea = unicodedata.east_asian_width(ch)
        if ea in ("F", "W"):
            cw = 2
        elif ea == "A":
            cw = 2
        else:
            cw = 1
        if w + cw > max_width - 1:
            break
        out.append(ch)
        w += cw
    return "".join(out) + "…"


# (列内容显示宽度, 数据对齐)；表头左对齐垫满列宽。列间用 | 便于扫读。
_VALIDATE_SNAPSHOT_ROWSPEC: tuple[tuple[int, str], ...] = (
    (10, "right"),  # event_id
    (54, "left"),  # match
    (8, "right"),  # line
    (6, "left"),  # rec
    (8, "left"),  # strength
    (10, "right"),  # score
    (9, "right"),  # scoreline
    (6, "left"),  # out
)
_VALIDATE_SNAPSHOT_COL_SEP = " | "


def _validate_snapshot_rule_line() -> str:
    """与表体同总显示宽度的一条横线（仅 ASCII，宽度与表一致）。"""
    tw = sum(w for w, _ in _VALIDATE_SNAPSHOT_ROWSPEC)
    n = len(_VALIDATE_SNAPSHOT_ROWSPEC)
    # 与 ``| `` + (cell + `` | ``) * … + `` |`` 的布局一致：首尾各 2，列间 `` | `` 为 3 宽。
    return "-" * (tw + 3 * (n - 1) + 4)


def _format_validate_snapshot_header() -> str:
    titles = ("event_id", "match", "line", "rec", "strength", "score", "scoreline", "out")
    cells = [_pad_display_cell(t, w, align="left") for t, (w, _) in zip(titles, _VALIDATE_SNAPSHOT_ROWSPEC)]
    return "| " + _VALIDATE_SNAPSHOT_COL_SEP.join(cells) + " |"


def _format_validate_snapshot_row(values: tuple[str, ...]) -> str:
    cells: list[str] = []
    for i, (v, (w, a)) in enumerate(zip(values, _VALIDATE_SNAPSHOT_ROWSPEC)):
        s = _truncate_display(v, w) if i == 1 and _text_display_width(v) > w else v
        cells.append(_pad_display_cell(s, w, align=a))
    return "| " + _VALIDATE_SNAPSHOT_COL_SEP.join(cells) + " |"


class ValidateReplaySnapshotStore(SnapshotStore):
    def __init__(self, root: Path, current_event_id: int, history_records: list[dict[str, Any]]):
        super().__init__(root)
        self.current_event_id = current_event_id
        self.history_records = list(history_records)

    def load_event(self, event_id: int) -> list[dict[str, Any]]:
        if event_id == self.current_event_id:
            return list(self.history_records)
        return super().load_event(event_id)


def run_validate_snapshots_from_dir(
    snapshot_root: Path, scores: dict[int, tuple[int, int]], *, fail_on_miss: bool = True
) -> int:
    """仅用 ``snapshot_root`` 下各场 jsonl 的 match 基础数据 + ``Predictor`` 重放，对照已知比分（不访问澳客网）。"""
    hit = miss = na = 0
    print(_format_validate_snapshot_header())
    print(_validate_snapshot_rule_line())
    for eid, (hg, ag) in sorted(scores.items()):
        path = snapshot_root / f"{eid}.jsonl"
        if not path.is_file():
            print(
                _format_validate_snapshot_row(
                    (str(eid), f"(missing {path.name})", "", "", "", "", "", "")
                )
            )
            continue
        lines = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if not lines:
            continue
        match = match_from_dict(lines[-1]["match"])
        client = OkoooSnapshotReplayClient(lines)
        store = ValidateReplaySnapshotStore(snapshot_root, eid, lines[:-1])
        res = Predictor(client, store).analyze(match)
        out, _margin = _replay_recommendation_outcome(
            res.recommendation,
            res.upper_team,
            res.lower_team,
            match.home,
            match.away,
            match.asian_line,
            hg,
            ag,
        )
        if out == "hit":
            hit += 1
        elif out == "miss":
            miss += 1
        else:
            na += 1
        print(
            _format_validate_snapshot_row(
                (
                    str(eid),
                    f"{match.home} vs {match.away}",
                    str(match.asian_line),
                    res.recommendation,
                    res.strength,
                    f"{res.score:+.3f}",
                    f"{hg}-{ag}",
                    out,
                )
            )
        )
    denom = hit + miss
    if denom:
        print(
            f"\n重放：有方向 {denom} 场 命中 {hit} 未中 {miss} 观望/走水 {na} ；命中率 {hit / denom:.0%}"
        )
    else:
        print("\n重放：无有效方向场次")
    return 0 if (not fail_on_miss or miss == 0) else 1


PAGE_KINDS = {
    "betfa": "betfa",
    "zhishu": "zhishu",
    "pankou": "pankou",
    "peilv": "peilv",
    "chayi": "chayi",
}

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

DETAIL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "max-age=0",
    "Priority": "u=0, i",
    "Sec-CH-UA": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

TOP_HANDICAP_BOOKS = (
    "Bet365",
    "澳门彩票",
    "皇冠",
    "韦德国际",
    "立博",
    "Interwetten",
    "SNAI",
    "Mansion 88",
)


@dataclass(frozen=True)
class BifaSelection:
    amount: float = 0.0
    cold_index: float = 0.0
    market_index: float = 0.0
    bf_odds: float = 0.0
    bf_ratio_pct: float = 0.0
    euro_avg: float = 0.0
    euro_prob_pct: float = 0.0
    payout: float = 0.0


@dataclass
class OkoooBaseMatch:
    okooo_id: int
    jc_code: str
    league: str
    kickoff: datetime
    home: str
    away: str
    lottery_handicap: str
    home_sel: BifaSelection
    draw_sel: BifaSelection
    away_sel: BifaSelection
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OkoooHandicap:
    rows: list[HandicapRow]
    consensus_line: str
    avg_home_water: float
    avg_away_water: float
    line_samples: list[float] = field(default_factory=list)


@dataclass
class OkoooEuroKelly:
    current_home: float
    current_draw: float
    current_away: float
    initial_home: float
    initial_draw: float
    initial_away: float
    kelly_home: float
    kelly_draw: float
    kelly_away: float
    points: list[EuroTrendPoint]
    bookmaker_count: int


@dataclass
class OkoooZhishu:
    initial_home: float
    initial_draw: float
    initial_away: float
    current_home: float
    current_draw: float
    current_away: float
    tips: str


@dataclass
class OkoooChayi:
    popularity_home: float
    popularity_draw: float
    popularity_away: float
    official_home: float
    official_draw: float
    official_away: float
    probability_home: float
    probability_draw: float
    probability_away: float
    diff_home: float
    diff_draw: float
    diff_away: float
    tips: str


def okooo_page_url(kind: str, issue: str) -> str:
    path = PAGE_KINDS[kind]
    return f"{OKOOO_BASE_URL}/jingcai/shuju/{path}/{issue}/"


def http_bytes(
    url: str,
    *,
    timeout: float,
    cookie: str | None = None,
    referer: str | None = None,
    method: str = "GET",
    data: bytes | None = None,
    accept: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> bytes:
    headers = dict(HTTP_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    if accept:
        headers["Accept"] = accept
    if cookie:
        headers["Cookie"] = cookie
    if referer:
        headers["Referer"] = referer
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, headers=headers, method=method, data=data)
    last: BaseException | None = None
    for ctx in (None, ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if ctx is None and is_ssl_verify_error(exc):
                continue
            body = ""
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    body = exc.read().decode("utf-8", errors="replace")[:160]
                except Exception:
                    body = ""
            extra = f"; body={body!r}" if body else ""
            raise DataError(f"Okooo GET failed {url}: {exc}{extra}") from exc
    raise DataError(f"Okooo GET failed {url}: {last}")


def decode_okooo(raw: bytes) -> str:
    for enc in ("gb18030", "gbk", "gb2312", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_okooo_html(kind: str, issue: str, *, timeout: float, cookie: str | None) -> str:
    url = okooo_page_url(kind, issue)
    return decode_okooo(http_bytes(url, timeout=timeout, cookie=cookie, referer=OKOOO_BASE_URL + "/"))


def sanitize_broken_attrs(value: str) -> str:
    """Okooo injects literal ``<span>`` tags inside data-Name attributes."""
    return re.sub(r"\sdata-Name=(\".*?\"|'.*?')", "", value or "", flags=re.I | re.S)


def clean_html_text(value: str) -> str:
    value = sanitize_broken_attrs(value)
    value = re.sub(
        r"<span\b[^>]*font-size\s*:\s*0[^>]*>.*?</span>",
        "",
        value or "",
        flags=re.I | re.S,
    )
    value = re.sub(r"<script\b.*?</script>", "", value, flags=re.I | re.S)
    value = re.sub(r"<style\b.*?</style>", "", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", "", value)
    value = html.unescape(value)
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def td_htmls(row_html: str) -> list[str]:
    row_html = sanitize_broken_attrs(row_html)
    return re.findall(r"<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)


def tr_htmls(block_html: str) -> list[str]:
    return re.findall(r"<tr\b[^>]*>.*?</tr>", block_html, flags=re.I | re.S)


def row_texts(row_html: str) -> list[str]:
    return [clean_html_text(td) for td in td_htmls(row_html)]


def parse_first_number(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = html.unescape(str(value)).replace(",", "").replace("%", "").strip()
    m = re.search(r"[+-]?\d+(?:\.\d+)?", text)
    if not m:
        return default
    try:
        return float(m.group(0))
    except ValueError:
        return default


def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(parse_first_number(value, float(default)))
    except (TypeError, ValueError):
        return default


def first_match_id(block_html: str) -> int | None:
    for pattern in (
        r"/soccer/match/(\d+)/",
        r"\btable(\d{5,})\b",
        r"data-matchId=[\"'](\d+)[\"']",
        r"matchId=(\d+)",
    ):
        m = re.search(pattern, block_html, flags=re.I)
        if m:
            return int(m.group(1))
    return None


def signed_lottery_handicap(text: str) -> str:
    m = re.search(r"\(([+-]?\d+(?:\.\d+)?)\)", text or "")
    if not m:
        return "0"
    return normalize_line_for_spdex(m.group(1))


def parse_kickoff_md(kick_md: str, *, now: datetime | None = None) -> datetime:
    m = re.match(r"^\s*(\d{2})-(\d{2})\s+(\d{2}):(\d{2})\s*$", kick_md)
    if not m:
        raise DataError(f"invalid Okooo kickoff date: {kick_md!r}")
    now = now or datetime.now(OKOOO_TZ)
    month, day, hour, minute = (int(m.group(i)) for i in range(1, 5))
    local = datetime(now.year, month, day, hour, minute, tzinfo=OKOOO_TZ)
    if local - now > timedelta(days=180):
        local = local.replace(year=local.year - 1)
    elif now - local > timedelta(days=180):
        local = local.replace(year=local.year + 1)
    return local.astimezone(timezone.utc)


def parse_detail_time(value: str) -> datetime | None:
    m = re.match(r"^\s*(\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})\s*$", value or "")
    if not m:
        return None
    year2, month, day, hour, minute, second = (int(m.group(i)) for i in range(1, 7))
    year = 2000 + year2 if year2 < 70 else 1900 + year2
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=OKOOO_TZ).astimezone(timezone.utc)
    except ValueError:
        return None


def match_meta(block_html: str) -> tuple[str, str, datetime, str, str, str]:
    title = re.search(
        r"<p[^>]*class=[\"']float_l[\"'][^>]*>\s*<b>(.*?)</b>\s*<b>(.*?)</b>\s*<b>(.*?)</b>",
        block_html,
        flags=re.I | re.S,
    )
    if not title:
        raise DataError("cannot parse Okooo match title")
    jc_code = clean_html_text(title.group(1))
    league = clean_html_text(title.group(2))
    kickoff = parse_kickoff_md(clean_html_text(title.group(3)))

    # 未赛：…<span>主队</span>…<strong>VS</strong><b>客队</b>；已赛复盘：…<span>主队</span><em>(-1)</em><strong>1-1</strong><b>客队</b>
    teams = re.search(
        r"<span>(?P<home>.*?)</span>\s*(?P<hc><em\b[^>]*>.*?</em>)?\s*(?:<strong>\s*VS\s*</strong>\s*<b>(?P<away_vs>.*?)</b>"
        r"|<strong>\s*\d+\s*-\s*\d+\s*</strong>\s*<b>(?P<away_sc>.*?)</b>)",
        block_html,
        flags=re.I | re.S,
    )
    if not teams:
        raise DataError("cannot parse Okooo teams")
    home = clean_html_text(teams.group("home"))
    handicap = signed_lottery_handicap(clean_html_text(teams.group("hc") or ""))
    away_raw = teams.group("away_vs") or teams.group("away_sc")
    if not away_raw:
        raise DataError("cannot parse Okooo away team")
    away = clean_html_text(away_raw)
    return jc_code, league, kickoff, home, away, handicap


def split_match_blocks(html_text: str, marker: str) -> list[str]:
    chunks = re.split(
        rf"(?=<div\b[^>]*class=[\"'][^\"']*container_wrapper\s+{re.escape(marker)}\b)",
        html_text,
        flags=re.I,
    )
    return [chunk for chunk in chunks if f"container_wrapper {marker}" in chunk[:300]]


def parse_bifa_selection(tds: list[str]) -> BifaSelection:
    return BifaSelection(
        amount=parse_first_number(tds[5]) if len(tds) > 5 else 0.0,
        cold_index=parse_first_number(tds[6]) if len(tds) > 6 else 0.0,
        market_index=parse_first_number(tds[7]) if len(tds) > 7 else 0.0,
        bf_odds=parse_first_number(tds[8]) if len(tds) > 8 else 0.0,
        bf_ratio_pct=parse_first_number(tds[9]) if len(tds) > 9 else 0.0,
        euro_avg=parse_first_number(tds[10]) if len(tds) > 10 else 0.0,
        euro_prob_pct=parse_first_number(tds[11]) if len(tds) > 11 else 0.0,
        payout=parse_first_number(tds[13]) if len(tds) > 13 else 0.0,
    )


def parse_betfa_html(html_text: str) -> dict[int, OkoooBaseMatch]:
    out: dict[int, OkoooBaseMatch] = {}
    for block in split_match_blocks(html_text, "betfa"):
        oid = first_match_id(block)
        if oid is None:
            continue
        try:
            jc_code, league, kickoff, home, away, handicap = match_meta(block)
        except DataError:
            continue
        selections: dict[str, BifaSelection] = {}
        for row in tr_htmls(block):
            tds = row_texts(row)
            if not tds:
                continue
            label = tds[0]
            if label in ("主胜", "平局", "客胜"):
                selections[label] = parse_bifa_selection(tds)
        if set(selections) != {"主胜", "平局", "客胜"}:
            continue
        home_sel = selections["主胜"]
        draw_sel = selections["平局"]
        away_sel = selections["客胜"]
        raw: dict[str, Any] = {
            "_source": "okooo",
            "_okooo_match_id": oid,
            "_okooo_jc_code": jc_code,
            "_okooo_lottery_handicap": handicap,
            "EventId": oid,
            "MatchTime": kickoff.isoformat(),
            "HomeTeam": home,
            "AwayTeam": away,
            "SortName": league,
            "LeagueName": league,
            "MatchPath": league,
            "BfIndexHome": home_sel.bf_ratio_pct,
            "BfIndexDraw": draw_sel.bf_ratio_pct,
            "BfIndexAway": away_sel.bf_ratio_pct,
            "BfAmountHome": home_sel.amount,
            "BfAmountDraw": draw_sel.amount,
            "BfAmountAway": away_sel.amount,
            "BfPayoutHome": home_sel.payout,
            "BfPayoutDraw": draw_sel.payout,
            "BfPayoutAway": away_sel.payout,
            "BfOddsHome": home_sel.bf_odds,
            "BfOddsDraw": draw_sel.bf_odds,
            "BfOddsAway": away_sel.bf_odds,
            "EuroAvrHome": home_sel.euro_avg,
            "EuroAvrDraw": draw_sel.euro_avg,
            "EuroAvrAway": away_sel.euro_avg,
            "PolyIndexHome": home_sel.market_index,
            "PolyIndexDraw": draw_sel.market_index,
            "PolyIndexAway": away_sel.market_index,
            "_okooo_cold_home": home_sel.cold_index,
            "_okooo_cold_draw": draw_sel.cold_index,
            "_okooo_cold_away": away_sel.cold_index,
            "_okooo_euro_prob_home": home_sel.euro_prob_pct,
            "_okooo_euro_prob_draw": draw_sel.euro_prob_pct,
            "_okooo_euro_prob_away": away_sel.euro_prob_pct,
        }
        out[oid] = OkoooBaseMatch(
            okooo_id=oid,
            jc_code=jc_code,
            league=league,
            kickoff=kickoff,
            home=home,
            away=away,
            lottery_handicap=handicap,
            home_sel=home_sel,
            draw_sel=draw_sel,
            away_sel=away_sel,
            raw=raw,
        )
    return out


def asian_line_number(text: str) -> float:
    t = clean_html_text(text)
    if not t or t in ("-", "封", "未开"):
        return 0.0
    receive = "受" in t
    t = t.replace("受", "").replace("让", "")
    if "平手" in t or t == "平":
        base = 0.0
    elif "半/一" in t:
        base = 0.75
    elif "一/球半" in t:
        base = 1.25
    elif "球半/两" in t:
        base = 1.75
    elif "两/两半" in t:
        base = 2.25
    elif "两半/三" in t:
        base = 2.75
    elif "三/三半" in t:
        base = 3.25
    elif "三半/四" in t:
        base = 3.75
    elif "四/四半" in t:
        base = 4.25
    elif "平/半" in t:
        base = 0.25
    elif "半球" in t or t == "半":
        base = 0.5
    elif "球半" in t:
        base = 1.5
    elif "两半" in t:
        base = 2.5
    elif "三半" in t:
        base = 3.5
    elif "四半" in t:
        base = 4.5
    elif "一球" in t or t == "一":
        base = 1.0
    elif "两球" in t or t == "两":
        base = 2.0
    elif "三球" in t or t == "三":
        base = 3.0
    elif "四球" in t or t == "四":
        base = 4.0
    else:
        base = parse_first_number(t, 0.0)
    if base == 0:
        return 0.0
    return base if receive else -base


def parse_bookmaker_id(row_html: str) -> int:
    m = re.search(r"data-ajaxData=[\"']\d+,(\d+),2,N", row_html, flags=re.I)
    if m:
        return int(m.group(1))
    return 0


def average(values: list[float]) -> float:
    usable = [value for value in values if value > 0]
    if not usable:
        return 0.0
    return sum(usable) / len(usable)


def parse_pankou_html(html_text: str) -> dict[int, OkoooHandicap]:
    out: dict[int, OkoooHandicap] = {}
    for block in split_match_blocks(html_text, "pankoudata"):
        if "初始盘口" not in block or "最新盘口" not in block:
            continue
        oid = first_match_id(block)
        if oid is None:
            continue
        rows: list[HandicapRow] = []
        line_values: list[float] = []
        home_waters: list[float] = []
        away_waters: list[float] = []
        for row_html in tr_htmls(block):
            tds = row_texts(row_html)
            if len(tds) < 20:
                continue
            name = tds[0]
            if not name or name == "公司名":
                continue
            init_home = parse_first_number(tds[1])
            init_line = asian_line_number(tds[2])
            init_away = parse_first_number(tds[3])
            latest_home = parse_first_number(tds[17])
            latest_line = asian_line_number(tds[18])
            latest_away = parse_first_number(tds[19])
            if latest_home <= 0 or latest_away <= 0:
                continue
            rows.append(
                HandicapRow(
                    bookmaker_id=parse_bookmaker_id(row_html),
                    name=name,
                    sec_a=latest_home,
                    sec_b=latest_away,
                    init_sec_a=init_home or latest_home,
                    init_sec_b=init_away or latest_away,
                    payout=0.97,
                    update_time=None,
                    source="okooo",
                    init_line=init_line,
                    latest_line=latest_line,
                    priority_hint=okooo_bookmaker_priority_index(name),
                )
            )
            if latest_line:
                line_values.append(latest_line)
            home_waters.append(latest_home)
            away_waters.append(latest_away)
        if rows:
            if line_values:
                consensus_value = median(line_values)
                consensus = normalize_line_for_spdex(str(consensus_value))
                avg_rows = [row for row in rows if abs(row.latest_line - consensus_value) <= 0.26]
            else:
                consensus_value = 0.0
                consensus = "0"
                avg_rows = rows
            if not avg_rows:
                avg_rows = rows
            out[oid] = OkoooHandicap(
                rows=rows,
                consensus_line=consensus,
                avg_home_water=average([row.sec_a for row in avg_rows]),
                avg_away_water=average([row.sec_b for row in avg_rows]),
                line_samples=line_values,
            )
    return out


def parse_peilv_html(html_text: str) -> dict[int, OkoooEuroKelly]:
    out: dict[int, OkoooEuroKelly] = {}
    for block in split_match_blocks(html_text, "pankoudata"):
        if "最新凯利指数" not in block or "初始指数" not in block:
            continue
        oid = first_match_id(block)
        if oid is None:
            continue
        init_h: list[float] = []
        init_d: list[float] = []
        init_a: list[float] = []
        cur_h: list[float] = []
        cur_d: list[float] = []
        cur_a: list[float] = []
        kel_h: list[float] = []
        kel_d: list[float] = []
        kel_a: list[float] = []
        init_kel_h: list[float] = []
        init_kel_d: list[float] = []
        init_kel_a: list[float] = []
        for row_html in tr_htmls(block):
            tds = row_texts(row_html)
            if len(tds) < 15:
                continue
            if not tds[0] or tds[0] in ("公司名",) or "方差" in tds[0] or "离散度" in tds[0]:
                continue
            h0, d0, a0 = parse_first_number(tds[1]), parse_first_number(tds[2]), parse_first_number(tds[3])
            h1, d1, a1 = parse_first_number(tds[5]), parse_first_number(tds[6]), parse_first_number(tds[7])
            kh, kd, ka = parse_first_number(tds[9]), parse_first_number(tds[10]), parse_first_number(tds[11])
            if min(h0, d0, a0, h1, d1, a1) <= 0:
                continue
            init_h.append(h0)
            init_d.append(d0)
            init_a.append(a0)
            cur_h.append(h1)
            cur_d.append(d1)
            cur_a.append(a1)
            if min(kh, kd, ka) > 0:
                kel_h.append(kh)
                kel_d.append(kd)
                kel_a.append(ka)
            # Optional 初盘凯利（澳客部分模板在 12–14 列；缺失则不在此处填充）
            if len(tds) >= 15:
                ikh, ikd, ika = (
                    parse_first_number(tds[12]),
                    parse_first_number(tds[13]),
                    parse_first_number(tds[14]),
                )
                if 0.70 < min(ikh, ikd, ika) and max(ikh, ikd, ika) < 1.15:
                    init_kel_h.append(ikh)
                    init_kel_d.append(ikd)
                    init_kel_a.append(ika)
        if cur_h:
            ih, idr, ia = average(init_h), average(init_d), average(init_a)
            ch, cd, ca = average(cur_h), average(cur_d), average(cur_a)
            kh, kd, ka = average(kel_h), average(kel_d), average(kel_a)
            if init_kel_h:
                ikh, ikd, ika = average(init_kel_h), average(init_kel_d), average(init_kel_a)
            else:
                ikh, ikd, ika = kh, kd, ka
            points = [
                EuroTrendPoint(
                    refresh_time=None,
                    home_price=ih,
                    draw_price=idr,
                    away_price=ia,
                    home_kelly=ikh,
                    draw_kelly=ikd,
                    away_kelly=ika,
                ),
                EuroTrendPoint(
                    refresh_time=None,
                    home_price=ch,
                    draw_price=cd,
                    away_price=ca,
                    home_kelly=kh,
                    draw_kelly=kd,
                    away_kelly=ka,
                ),
            ]
            out[oid] = OkoooEuroKelly(
                current_home=ch,
                current_draw=cd,
                current_away=ca,
                initial_home=ih,
                initial_draw=idr,
                initial_away=ia,
                kelly_home=kh,
                kelly_draw=kd,
                kelly_away=ka,
                points=points,
                bookmaker_count=len(cur_h),
            )
    return out


def parse_zhishu_html(html_text: str) -> dict[int, OkoooZhishu]:
    out: dict[int, OkoooZhishu] = {}
    for row_html in tr_htmls(html_text):
        oid = first_match_id(row_html)
        if oid is None:
            continue
        tds = row_texts(row_html)
        if len(tds) < 12:
            continue
        init_h, init_d, init_a = parse_first_number(tds[6]), parse_first_number(tds[7]), parse_first_number(tds[8])
        cur_h, cur_d, cur_a = parse_first_number(tds[9]), parse_first_number(tds[10]), parse_first_number(tds[11])
        if max(cur_h, cur_d, cur_a) <= 0:
            continue
        out[oid] = OkoooZhishu(
            initial_home=init_h,
            initial_draw=init_d,
            initial_away=init_a,
            current_home=cur_h,
            current_draw=cur_d,
            current_away=cur_a,
            tips=tds[12] if len(tds) > 12 else "",
        )
    return out


def parse_chayi_html(html_text: str) -> dict[int, OkoooChayi]:
    out: dict[int, OkoooChayi] = {}
    for block in split_match_blocks(html_text, "pankoudata"):
        if "投注比差异判断法" not in block:
            continue
        oid = first_match_id(block)
        if oid is None:
            continue
        rows = [row_texts(row) for row in tr_htmls(block)]
        values: dict[str, tuple[float, float, float, float]] = {}
        tips = ""
        for tds in rows:
            if len(tds) < 5:
                continue
            label = tds[0].replace(" ", "")
            if label in ("胜", "平", "负"):
                popularity = parse_first_number(tds[1])
                official = parse_first_number(tds[2])
                probability = parse_first_number(tds[3])
                diff = parse_first_number(tds[4])
                values[label] = (popularity, official, probability, diff)
                if len(tds) > 5 and tds[5]:
                    tips = tds[5]
        if set(values) >= {"胜", "平", "负"}:
            home_v = values["胜"]
            draw_v = values["平"]
            away_v = values["负"]
            out[oid] = OkoooChayi(
                popularity_home=home_v[0],
                popularity_draw=draw_v[0],
                popularity_away=away_v[0],
                official_home=home_v[1],
                official_draw=draw_v[1],
                official_away=away_v[1],
                probability_home=home_v[2],
                probability_draw=draw_v[2],
                probability_away=away_v[2],
                diff_home=home_v[3],
                diff_draw=draw_v[3],
                diff_away=away_v[3],
                tips=tips,
            )
    return out


class OkoooClient:
    def __init__(
        self,
        *,
        issue: str = OKOOO_DEFAULT_ISSUE,
        timeout: float = 20.0,
        cookie: str | None = None,
        trade_trend: bool = True,
        detail_max_pages: int = 5,
    ) -> None:
        self.issue = issue
        self.timeout = timeout
        self.cookie = cookie
        self.trade_trend = trade_trend
        self.detail_max_pages = max(1, detail_max_pages)
        self.base_matches: dict[int, OkoooBaseMatch] = {}
        self.handicap: dict[int, OkoooHandicap] = {}
        self.euro_kelly: dict[int, OkoooEuroKelly] = {}
        self.zhishu: dict[int, OkoooZhishu] = {}
        self.chayi: dict[int, OkoooChayi] = {}
        self._trend_cache: dict[int, dict[str, list[PriceVolumePoint]]] = {}
        self._detail_errors: dict[int, str] = {}
        self.page_errors: dict[str, str] = {}

    def refresh(self, *, core_only: bool = False) -> None:
        self.page_errors.clear()
        self.base_matches = self._fetch_parse("betfa", parse_betfa_html)
        if core_only:
            return
        self.handicap = self._fetch_parse("pankou", parse_pankou_html)
        self.euro_kelly = self._fetch_parse("peilv", parse_peilv_html)
        self.zhishu = self._fetch_parse("zhishu", parse_zhishu_html)
        self.chayi = self._fetch_parse("chayi", parse_chayi_html)

    def _fetch_parse(self, kind: str, parser: Any) -> Any:
        try:
            text = fetch_okooo_html(kind, self.issue, timeout=self.timeout, cookie=self.cookie)
            return parser(text)
        except Exception as exc:
            self.page_errors[kind] = str(exc)
            if kind == "betfa":
                raise
            return {}

    def schedule_ids(self) -> list[int]:
        return sorted(self.base_matches)

    def build_match(self, match_id: int) -> Match:
        if not self.base_matches:
            self.refresh()
        base = self.base_matches.get(match_id)
        if base is None:
            raise DataError(f"Okooo match id not found in {self.issue}: {match_id}")

        raw = dict(base.raw)
        asian_line = "0"
        handicap = self.handicap.get(match_id)
        if handicap:
            asian_line = handicap.consensus_line
            raw["AsianAvrLet"] = asian_line
            raw["AsianAvrHome"] = handicap.avg_home_water
            raw["AsianAvrAway"] = handicap.avg_away_water
            raw["_okooo_handicap_rows"] = len(handicap.rows)
            raw["_okooo_handicap_row_data"] = [handicap_row_to_dict(row) for row in handicap.rows]
            raw["_okooo_handicap_line_samples"] = handicap.line_samples[:8]
        else:
            raw["_okooo_missing_asian_line"] = True

        euro = self.euro_kelly.get(match_id)
        if euro:
            raw["EuroAvrHome"] = euro.current_home or raw.get("EuroAvrHome", 0)
            raw["EuroAvrDraw"] = euro.current_draw or raw.get("EuroAvrDraw", 0)
            raw["EuroAvrAway"] = euro.current_away or raw.get("EuroAvrAway", 0)
            raw["KellyHome"] = euro.kelly_home
            raw["KellyDraw"] = euro.kelly_draw
            raw["KellyAway"] = euro.kelly_away
            raw["_okooo_euro_bookmakers"] = euro.bookmaker_count
            raw["_okooo_euro_trend_points"] = [euro_trend_point_to_dict(point) for point in euro.points]

        zhishu = self.zhishu.get(match_id)
        if zhishu:
            raw["_okooo_zhishu_initial_home"] = zhishu.initial_home
            raw["_okooo_zhishu_initial_draw"] = zhishu.initial_draw
            raw["_okooo_zhishu_initial_away"] = zhishu.initial_away
            raw["_okooo_zhishu_home"] = zhishu.current_home
            raw["_okooo_zhishu_draw"] = zhishu.current_draw
            raw["_okooo_zhishu_away"] = zhishu.current_away
            raw["_okooo_zhishu_tips"] = zhishu.tips

        chayi = self.chayi.get(match_id)
        if chayi:
            raw["_okooo_popularity_home"] = chayi.popularity_home
            raw["_okooo_popularity_draw"] = chayi.popularity_draw
            raw["_okooo_popularity_away"] = chayi.popularity_away
            raw["_okooo_probability_home"] = chayi.probability_home
            raw["_okooo_probability_draw"] = chayi.probability_draw
            raw["_okooo_probability_away"] = chayi.probability_away
            raw["_okooo_diff_home"] = chayi.diff_home
            raw["_okooo_diff_draw"] = chayi.diff_draw
            raw["_okooo_diff_away"] = chayi.diff_away
            raw["_okooo_chayi_tips"] = chayi.tips

        match = Match(
            event_id=base.okooo_id,
            match_time=base.kickoff,
            home=base.home,
            away=base.away,
            league_id=None,
            league_name=base.league,
            asian_line=asian_line,
            is_stop_update=False,
            raw=raw,
        )
        self._fill_external_fields(match)
        return match

    def _fill_external_fields(self, match: Match) -> None:
        raw = match.raw
        upper_team, lower_team = upper_lower_teams(match)
        upper_key = side_key(match, upper_team)
        lower_key = side_key(match, lower_team)

        chayi = self.chayi.get(match.event_id)
        if chayi and upper_key in ("Home", "Away") and lower_key in ("Home", "Away"):
            official_by_key = {
                "Home": chayi.official_home,
                "Draw": chayi.official_draw,
                "Away": chayi.official_away,
            }
            raw["ExternalH2hUpperPrice"] = official_by_key.get(upper_key, 0.0)
            raw["ExternalH2hLowerPrice"] = official_by_key.get(lower_key, 0.0)

        zhishu = self.zhishu.get(match.event_id)
        if zhishu and upper_key in ("Home", "Away") and lower_key in ("Home", "Away"):
            z_by_key = {
                "Home": zhishu.current_home,
                "Draw": zhishu.current_draw,
                "Away": zhishu.current_away,
            }
            upper_z = z_by_key.get(upper_key, 0.0)
            lower_z = z_by_key.get(lower_key, 0.0)
            if upper_z > 0 and lower_z > 0:
                raw["ModelPowerEdge"] = clamp((upper_z - lower_z) / 0.35, -1, 1)

        handicap = self.handicap.get(match.event_id)
        if handicap:
            depth = line_depth(match.asian_line)
            if depth > 0:
                raw["ModelFairLineDepth"] = depth

    def handicap_list(self, event_id: int, _asian_line: str) -> list[HandicapRow]:
        data = self.handicap.get(event_id)
        if not data:
            return []
        return sorted(data.rows, key=okooo_bookmaker_priority)

    def euro_trend(self, event_id: int) -> list[EuroTrendPoint]:
        data = self.euro_kelly.get(event_id)
        return list(data.points) if data else []

    def price_volume(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        if not self.trade_trend:
            return []
        if event_id not in self._trend_cache:
            detail_points = self._fetch_exchanges_detail(event_id) if self.cookie else {"home": [], "draw": [], "away": []}
            if any(detail_points.values()):
                self._trend_cache[event_id] = detail_points
            else:
                self._trend_cache[event_id] = self._fetch_trade_trend(event_id)
        return list(self._trend_cache[event_id].get(selection, []))

    def attach_snapshot_replay_fields(self, match: Match) -> None:
        trend = self._trend_cache.get(match.event_id)
        if trend:
            match.raw["_okooo_price_volume_points"] = {
                selection: [price_volume_point_to_dict(point) for point in trend.get(selection, [])]
                for selection in ("home", "draw", "away")
            }

    def _fetch_exchanges_detail(self, event_id: int) -> dict[str, list[PriceVolumePoint]]:
        base_url = f"{OKOOO_BASE_URL}/soccer/match/{event_id}/exchanges/detail/"
        pages = [base_url]
        merged: dict[str, list[PriceVolumePoint]] = {"home": [], "draw": [], "away": []}
        try:
            first_html = self._fetch_exchanges_detail_html(base_url)
            merged = merge_price_volume_points(merged, parse_exchanges_detail_html(first_html))
            for link in parse_detail_page_links(first_html, event_id):
                if link not in pages:
                    pages.append(link)
            for url in pages[1 : self.detail_max_pages]:
                page_html = self._fetch_exchanges_detail_html(url)
                merged = merge_price_volume_points(merged, parse_exchanges_detail_html(page_html))
        except DataError as exc:
            self._detail_errors[event_id] = str(exc)
            return {"home": [], "draw": [], "away": []}
        return merged

    def _fetch_exchanges_detail_html(self, url: str) -> str:
        raw = http_bytes(
            url,
            timeout=self.timeout,
            cookie=self.cookie,
            referer=okooo_page_url("betfa", self.issue),
            extra_headers=DETAIL_HEADERS,
        )
        text = decode_okooo(raw)
        sample = text[:3000].lower()
        if "<title>405</title>" in sample or "your request has been blocked" in sample or "访问被阻断" in text[:5000]:
            raise DataError("Okooo exchanges/detail returned WAF block page; refresh OKOOO_COOKIE from browser")
        return text

    def _fetch_trade_trend(self, event_id: int) -> dict[str, list[PriceVolumePoint]]:
        url = f"{OKOOO_BASE_URL}/danchang/shuju/ajax/?action=trend"
        body = urllib.parse.urlencode({"matchId": str(event_id)}).encode("ascii")
        try:
            raw = http_bytes(
                url,
                timeout=self.timeout,
                cookie=self.cookie,
                referer=okooo_page_url("betfa", self.issue),
                method="POST",
                data=body,
                accept="application/json, text/javascript, */*; q=0.01",
            )
            payload = json.loads(decode_okooo(raw))
        except (DataError, json.JSONDecodeError):
            return {"home": [], "draw": [], "away": []}
        trend = payload.get("trend") if isinstance(payload, dict) else None
        if not isinstance(trend, list):
            return {"home": [], "draw": [], "away": []}
        return parse_trade_trend_payload(trend)


def okooo_bookmaker_priority(row: HandicapRow) -> tuple[int, str]:
    hint = row.priority_hint
    if hint is not None:
        return (hint, row.name)
    return (okooo_bookmaker_priority_index(row.name), row.name)


def okooo_bookmaker_priority_index(bookmaker_name: str) -> int:
    clean = re.sub(r"\s+", "", bookmaker_name)
    for idx, preferred_name in enumerate(TOP_HANDICAP_BOOKS):
        if re.sub(r"\s+", "", preferred_name) in clean:
            return idx
    return 100


def parse_trade_trend_payload(trend: list[Any]) -> dict[str, list[PriceVolumePoint]]:
    by_selection: dict[str, list[PriceVolumePoint]] = {"home": [], "draw": [], "away": []}
    index_by_sel = {"home": 0, "draw": 1, "away": 2}
    code_by_sel = {"home": 3, "draw": 1, "away": 0}
    for row in trend:
        if not isinstance(row, list) or len(row) < 2:
            continue
        ts = parse_first_number(row[0])
        odds = row[1] if len(row) > 1 and isinstance(row[1], list) else []
        trade = row[2] if len(row) > 2 and isinstance(row[2], list) else []
        trade_code = parse_int(trade[0], default=-999) if trade else -999
        trade_amount = parse_first_number(trade[1]) if len(trade) > 1 else 0.0
        dt = datetime.fromtimestamp(ts, timezone.utc) if ts > 0 else None
        for sel, idx in index_by_sel.items():
            price = parse_first_number(odds[idx]) if len(odds) > idx else 0.0
            volume = trade_amount if trade_code == code_by_sel[sel] else 0.0
            if 1.01 <= price <= 100:
                by_selection[sel].append(PriceVolumePoint(price=price, volume=volume, update_time=dt, attr=None))
    return by_selection


def parse_detail_page_links(html_text: str, event_id: int) -> list[str]:
    links: list[str] = []
    for href in re.findall(r"href=['\"]([^'\"]*exchanges/detail/[^'\"]*)['\"]", html_text, flags=re.I):
        if "PageID=" not in href:
            continue
        if f"/{event_id}/" not in href and f"match/{event_id}/" not in href:
            continue
        links.append(urllib.parse.urljoin(OKOOO_BASE_URL, html.unescape(href)))
    return list(dict.fromkeys(links))


def detail_rows_to_points(rows: list[dict[str, Any]]) -> list[PriceVolumePoint]:
    rows = sorted(rows, key=lambda item: item["time"])
    points: list[PriceVolumePoint] = []
    previous_total: float | None = None
    for row in rows:
        total = float(row.get("total", 0.0))
        single = float(row.get("single", 0.0))
        if single > 0:
            delta = single
        elif previous_total is None:
            # The first visible total is already cumulative before this row; do not treat it as fresh flow.
            delta = 0.0
        else:
            delta = max(total - previous_total, 0.0)
        previous_total = total if previous_total is None else max(previous_total, total)
        attr = str(row.get("attr") or "").strip() or None
        points.append(
            PriceVolumePoint(
                price=float(row.get("price", 0.0)),
                volume=delta,
                update_time=row.get("time"),
                attr=attr,
            )
        )
    return points


def parse_exchanges_detail_html(html_text: str) -> dict[str, list[PriceVolumePoint]]:
    raw_rows: dict[str, list[dict[str, Any]]] = {"home": [], "draw": [], "away": []}
    offsets = {"home": 1, "draw": 5, "away": 9}
    # 每 outcome 四列：价位、总成交量、单笔成交量、下一格（部分模板为买/卖，多数仅为数字或空）
    for row_html in tr_htmls(html_text):
        tds = row_texts(row_html)
        if len(tds) < 13:
            continue
        dt = parse_detail_time(tds[0])
        if dt is None:
            continue
        for selection, offset in offsets.items():
            price = parse_first_number(tds[offset])
            total = parse_first_number(tds[offset + 1])
            single = parse_first_number(tds[offset + 2])
            attr = tds[offset + 3]
            if 1.01 <= price <= 100:
                raw_rows[selection].append(
                    {
                        "time": dt,
                        "price": price,
                        "total": total,
                        "single": single,
                        "attr": attr,
                    }
                )
    return {key: detail_rows_to_points(rows) for key, rows in raw_rows.items()}


def merge_price_volume_points(
    base: dict[str, list[PriceVolumePoint]],
    extra: dict[str, list[PriceVolumePoint]],
) -> dict[str, list[PriceVolumePoint]]:
    out: dict[str, list[PriceVolumePoint]] = {}
    for selection in ("home", "draw", "away"):
        seen: set[tuple[str, float]] = set()
        merged: list[PriceVolumePoint] = []
        for point in [*base.get(selection, []), *extra.get(selection, [])]:
            key = ((point.update_time.isoformat() if point.update_time else ""), point.price)
            if key in seen:
                continue
            seen.add(key)
            merged.append(point)
        out[selection] = sorted(merged, key=lambda item: item.update_time or datetime.min.replace(tzinfo=timezone.utc))
    return out


def cookie_from_env(cookie_file: str | None = None) -> str | None:
    if cookie_file:
        try:
            text = Path(cookie_file).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise DataError(f"cannot read OKOOO cookie file {cookie_file}: {exc}") from exc
        return " ".join(part.strip() for part in text.splitlines() if part.strip()) or None
    return os.environ.get("OKOOO_COOKIE", "").strip() or None


def snapshot_dir(cli_value: str | None) -> str:
    return (cli_value or os.environ.get("OKOOO_SNAPSHOT_DIR") or OKOOO_SNAPSHOT_DIR).strip()


def is_world_cup_league(league: str) -> bool:
    text = league or ""
    return "世界杯" in text or "世界盃" in text or "world cup" in text.lower()


def base_match_matches_filters(match: OkoooBaseMatch, args: argparse.Namespace) -> bool:
    if getattr(args, "world_cup", False) and not is_world_cup_league(match.league):
        return False
    league_contains = (getattr(args, "league_contains", "") or "").strip()
    if league_contains and league_contains not in match.league:
        return False
    return True


def schedule_task_key(match: Match, label: str) -> str:
    return f"{match.event_id}:{match.match_time.isoformat()}:{label}"


def build_okooo_scheduled_tasks(
    matches: list[Match],
    *,
    now: datetime,
    horizon: timedelta,
    completed: set[str],
    catch_up: bool,
) -> list[ScheduledTask]:
    horizon_end = now + horizon
    tasks: list[ScheduledTask] = []
    for match in matches:
        if match.match_time <= now or match.match_time > horizon_end:
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


def print_okooo_watch_summary(tasks: list[ScheduledTask], completed: set[str]) -> None:
    pending = [task for task in tasks if task.key not in completed]
    if not pending:
        print("未来窗口内没有待执行的澳客自动快照任务。", flush=True)
        return
    next_task = pending[0]
    print(
        f"下次任务: {format_local(next_task.run_at)} | {next_task.label} | "
        f"{next_task.match.event_id} {next_task.match.home} vs {next_task.match.away}",
        flush=True,
    )


def selected_watch_match_ids(client: OkoooClient, args: argparse.Namespace) -> list[int]:
    ids = [int(x) for x in (args.match_ids or "").split(",") if x.strip()]
    if args.world_cup:
        for match in client.base_matches.values():
            if is_world_cup_league(match.league):
                ids.append(match.okooo_id)
    ids = list(dict.fromkeys(ids))
    if not ids:
        raise SystemExit("watch requires --match-ids id1,id2,... or --world-cup")
    return ids[: args.limit]


def cmd_sources(client: OkoooClient, _store: SnapshotStore, _args: argparse.Namespace) -> int:
    client.refresh()
    print("Okooo feed health:")
    print(f"  issue: {client.issue}")
    print(f"  betfa 必发盈亏: {len(client.base_matches)} matches")
    print(f"  pankou 盘口评测: {len(client.handicap)} matches")
    print(f"  peilv 凯利方差: {len(client.euro_kelly)} matches")
    print(f"  zhishu 胜负指数: {len(client.zhishu)} matches")
    print(f"  chayi 差异分析: {len(client.chayi)} matches")
    if client.page_errors:
        print("  page errors:")
        for kind, err in client.page_errors.items():
            print(f"    {kind}: {err}")
    print("\nDocumented/list pages:")
    for kind in PAGE_KINDS:
        print(f"  {kind}: {okooo_page_url(kind, client.issue)}")
    print("  trade trend: https://www.okooo.cn/danchang/shuju/ajax/?action=trend (POST matchId=...)")
    if client.cookie:
        print(f"  exchanges detail: enabled via cookie; max_pages={client.detail_max_pages}")
    else:
        print("  exchanges detail: disabled until OKOOO_COOKIE or --cookie-file is provided")
    return 0


def cmd_upcoming(client: OkoooClient, _store: SnapshotStore, args: argparse.Namespace) -> int:
    client.refresh(core_only=not args.full)
    now = datetime.now(timezone.utc)
    upper = datetime(2100, 1, 1, tzinfo=timezone.utc) if args.all_future else now + timedelta(hours=args.hours)
    rows: list[OkoooBaseMatch] = []
    for match in client.base_matches.values():
        if not args.include_past and match.kickoff <= now:
            continue
        if match.kickoff > upper:
            continue
        if not base_match_matches_filters(match, args):
            continue
        rows.append(match)
    rows.sort(key=lambda item: item.kickoff)
    print("开赛时间(UTC)          OkoooID    对阵")
    for item in rows[: args.limit]:
        local = item.kickoff.astimezone(OKOOO_TZ).strftime("%m-%d %H:%M")
        print(
            f"{item.kickoff.isoformat()}  {item.okooo_id:>8}  "
            f"{item.home} vs {item.away}  [{item.league} {item.jc_code} {local} 竞彩{item.lottery_handicap}]"
        )
    if not rows:
        print("（筛选范围内没有比赛。）")
    return 0


def cmd_validate_snapshots(_client: OkoooClient, store: SnapshotStore, args: argparse.Namespace) -> int:
    """不抓取网络；只读本地快照与 ``worldcup_ah_cli.Predictor`` 算法。"""
    try:
        scores = load_okooo_validate_scores(args.scores_json)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    root = Path(store.root)
    if not root.is_dir():
        print(f"error: snapshot directory not found: {root}", file=sys.stderr)
        return 1
    return run_validate_snapshots_from_dir(root, scores, fail_on_miss=not args.allow_miss)


def attach_snapshot_replay_fields(client: OkoooClient, match: Match) -> None:
    attach = getattr(client, "attach_snapshot_replay_fields", None)
    if callable(attach):
        attach(match)


def cmd_predict(client: OkoooClient, store: SnapshotStore, args: argparse.Namespace) -> int:
    client.refresh()
    match = client.build_match(args.match_id)
    predictor = Predictor(client, store)
    result = predictor.analyze(match)
    if args.save_snapshot:
        attach_snapshot_replay_fields(client, result.match)
        path = store.save(result)
        if not args.json:
            print_snapshot_saved(path, result)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_analysis(result, verbose=args.verbose)
    return 0


def cmd_snapshot(client: OkoooClient, store: SnapshotStore, args: argparse.Namespace) -> int:
    client.refresh()
    predictor = Predictor(client, store)
    for match_id in args.match_ids:
        match = client.build_match(match_id)
        result = predictor.analyze(match)
        attach_snapshot_replay_fields(client, result.match)
        path = store.save(result)
        print_snapshot_saved(path, result)
    return 0


def cmd_trend(_client: OkoooClient, store: SnapshotStore, args: argparse.Namespace) -> int:
    return print_snapshot_trend(store, args.match_id)


def cmd_watch(client: OkoooClient, store: SnapshotStore, args: argparse.Namespace) -> int:
    print(
        f"启动澳客自动快照: 未来 {args.horizon_hours:g} 小时，检查间隔 {args.interval:g}s，"
        f"状态文件 {store.scheduler_state_path()}",
        flush=True,
    )
    while True:
        now = datetime.now(timezone.utc)
        tasks: list[ScheduledTask] = []
        completed: set[str] = set()
        try:
            client.refresh()
            ids = selected_watch_match_ids(client, args)
            matches = [client.build_match(match_id) for match_id in ids]
            matches = sorted(matches, key=lambda item: (item.match_time, item.event_id))
            predictor = Predictor(client, store)
            state = store.load_scheduler_state()
            completed = set((state.get("completed") or {}).keys())
            tasks = build_okooo_scheduled_tasks(
                matches,
                now=now,
                horizon=timedelta(hours=args.horizon_hours),
                completed=completed,
                catch_up=not args.no_catch_up,
            )
            due = [task for task in tasks if task.run_at <= now and task.key not in completed]
            if due:
                for task in due:
                    print(
                        f"[{format_local(now)}] 执行 {task.label}: {task.match.event_id} | "
                        f"{task.match.home} vs {task.match.away} | 开赛 {format_local(task.match.match_time)}",
                        flush=True,
                    )
                    result = predictor.analyze(task.match)
                    attach_snapshot_replay_fields(client, result.match)
                    path = store.save(result, fetched_at=now)
                    print_snapshot_saved(path, result)
                    if task.do_predict:
                        print_analysis(result, verbose=args.verbose)
                    store.mark_task_completed(task, completed_at=now)
                    completed.add(task.key)
            else:
                print_okooo_watch_summary(tasks, completed)
        except DataError as exc:
            print(f"[WARN] {exc}", file=sys.stderr, flush=True)
            if args.once:
                return 2
        if args.once:
            return 0
        next_task = next((task for task in tasks if task.key not in completed and task.run_at > datetime.now(timezone.utc)), None)
        sleep_seconds = args.interval
        if next_task:
            seconds_to_next = max(1, int((next_task.run_at - datetime.now(timezone.utc)).total_seconds()))
            sleep_seconds = min(args.interval, seconds_to_next)
        time.sleep(max(1, sleep_seconds))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="澳客 Okooo 数据源版亚盘助手：抓取 betfa/pankou/peilv/zhishu/chayi 并复用 worldcup_ah_cli.Predictor。",
    )
    p.add_argument("--issue", default=os.environ.get("OKOOO_ISSUE", OKOOO_DEFAULT_ISSUE), help="澳客期号/路径，默认 dqjc")
    p.add_argument("--timeout", type=float, default=float(os.environ.get("OKOOO_TIMEOUT", "20")), help="HTTP timeout seconds")
    p.add_argument("--snapshot-dir", default=None, help="快照目录，默认 .okooo_snapshots 或 OKOOO_SNAPSHOT_DIR")
    p.add_argument("--env-file", default=None, help="加载 .env（默认仓库根 .env）")
    p.add_argument("--no-dotenv", action="store_true", help="不加载 .env")
    p.add_argument("--cookie-file", default=None, help="从文件读取浏览器 Cookie（用于 exchanges/detail 明细页）")
    p.add_argument(
        "--detail-max-pages",
        type=int,
        default=int(os.environ.get("OKOOO_DETAIL_MAX_PAGES", "5")),
        help="必发明细分页最多抓取页数，默认 5",
    )
    p.add_argument("--no-trade-trend", action="store_true", help="不探测 Okooo 必发明细/近3小时成交接口")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sources", help="检查澳客汇总页可抓取数量")
    s.set_defaults(func=cmd_sources)

    u = sub.add_parser("upcoming", help="列出当前期比赛")
    u.add_argument("--hours", type=float, default=72.0)
    u.add_argument("--limit", type=int, default=30)
    u.add_argument("--include-past", action="store_true", help="包含已开赛/已结束场次")
    u.add_argument("--all-future", action="store_true", help="列出所有未来比赛，不按 --hours 截断")
    u.add_argument("--world-cup", action="store_true", help="仅世界杯")
    u.add_argument("--league-contains", default="", help="联赛名包含文本")
    u.add_argument("--full", action="store_true", help="同时抓取盘口/凯利等页面用于健康检查")
    u.set_defaults(func=cmd_upcoming)

    pr = sub.add_parser("predict", help="预测单场 Okooo match id")
    pr.add_argument("--match-id", type=int, required=True)
    pr.add_argument("--verbose", action="store_true")
    pr.add_argument("--json", action="store_true")
    pr.add_argument("--save-snapshot", action="store_true")
    pr.set_defaults(func=cmd_predict)

    val = sub.add_parser(
        "validate-snapshots",
        help="读取本地 jsonl 快照，用 Predictor 重放并对照已知比分（不访问网络）",
    )
    val.add_argument(
        "--scores-json",
        type=Path,
        default=None,
        help="JSON 如 {\"1315857\":[2,0]}；默认 tools/okooo_validate_scores.json",
    )
    val.add_argument(
        "--allow-miss",
        action="store_true",
        help="有「未中」方向时仍退出 0（默认：存在 miss 则退出 1，便于 CI）",
    )
    val.set_defaults(func=cmd_validate_snapshots)

    sn = sub.add_parser("snapshot", help="预测并保存一组比赛快照")
    sn.add_argument("--match-ids", type=int, nargs="+", required=True)
    sn.set_defaults(func=cmd_snapshot)

    tr = sub.add_parser("trend", help="查看本地快照趋势")
    tr.add_argument("--match-id", type=int, required=True)
    tr.set_defaults(func=cmd_trend)

    w = sub.add_parser("watch", help="按赛前窗口滚动预测并保存快照")
    w.add_argument("--match-ids", default="", help="逗号分隔 Okooo match id；可与 --world-cup 叠加")
    w.add_argument("--world-cup", action="store_true", help="自动观察当前期所有世界杯比赛")
    w.add_argument("--horizon-hours", type=float, default=24.0, help="扫描未来多少小时，默认 24")
    w.add_argument("--limit", type=int, default=20, help="最多观察多少场，默认 20")
    w.add_argument("--interval", type=float, default=float(os.environ.get("OKOOO_POLL_SEC", "120")), help="检查间隔秒数")
    w.add_argument("--once", action="store_true", help="只扫描并执行当前到期任务一次，不常驻")
    w.add_argument("--no-catch-up", action="store_true", help="不补采已错过但尚未记录的最近窗口")
    w.add_argument("--verbose", action="store_true", help="临场预测窗口显示详细信号")
    w.set_defaults(func=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.no_dotenv:
        load_dotenv_file(Path(args.env_file).expanduser() if args.env_file else default_env_file_path())
    client = OkoooClient(
        issue=args.issue,
        timeout=args.timeout,
        cookie=cookie_from_env(args.cookie_file),
        trade_trend=not args.no_trade_trend,
        detail_max_pages=args.detail_max_pages,
    )
    store = SnapshotStore(snapshot_dir(args.snapshot_dir))
    try:
        return args.func(client, store, args)
    except DataError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
