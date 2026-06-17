"""Fetch and parse 澳客竞彩「必发盈亏」页 HTML → Predictor ``Bf*`` 1X2 字段.

页面示例: https://www.okooo.cn/jingcai/shuju/betfa/
必发成交明细页: ``https://www.okooo.cn/soccer/match/{okooo_id}/exchanges/detail/``（HTML；需 ``OKOOO_COOKIE``；``fetch_okooo_exchanges_detail_series`` 供 Titan007 ``price_volume`` 使用）

编码为 GB18030；球探 ``ScheduleID`` 与澳客 ``okooo_id`` 无官方 API，可配置 **显式映射**
（``OKOOO_TITAN_MAP_PATH`` / ``TITAN007_OKOOO_IDS``），否则按 **队名 + 开赛日（北京时间）** 启发式匹配。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from worldcup_ah_cli import DataError, PriceVolumePoint, is_ssl_verify_error

OKOOO_BETFA_DEFAULT = "https://www.okooo.cn/jingcai/shuju/betfa/"
OKOOO_EXCHANGES_DETAIL_TMPL = "https://www.okooo.cn/soccer/match/{okooo_id}/exchanges/detail/"


@dataclass(frozen=True)
class OkoooBifa1x2:
    """一行 主胜 / 平局 / 客胜 的表格列（澳客列顺序经页面抽样固定）。"""

    amount: float
    cold_index: float
    market_index: float
    odds: float
    ratio_pct: float
    payout: float


@dataclass(frozen=True)
class OkoooBifaMatch:
    okooo_id: int
    home: str
    away: str
    kickoff_md: str  # MM-DD HH:MM 北京时间，不含年
    league_tag: str
    jc_code: str
    home_sel: OkoooBifa1x2
    draw_sel: OkoooBifa1x2
    away_sel: OkoooBifa1x2


def _http_bytes(url: str, *, timeout: float, cookie: str | None) -> bytes:
    headers = {
        "User-Agent": "worldcup-titan007-okooo/1.0 (+https://github.com/)",
        "Accept": "text/html,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    last: BaseException | None = None
    for ctx in (None, ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            if ctx is None and is_ssl_verify_error(exc):
                continue
            raise DataError(f"Okooo GET failed {url}: {exc}") from exc
    raise DataError(f"Okooo GET failed {url}: {last}")


def fetch_okooo_betfa_html(
    *,
    url: str | None = None,
    timeout: float = 22.0,
    cookie: str | None = None,
) -> str:
    u = (url or os.environ.get("OKOOO_BETFA_URL") or OKOOO_BETFA_DEFAULT).strip()
    raw = _http_bytes(u, timeout=timeout, cookie=cookie)
    for enc in ("gb18030", "gbk", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def fetch_okooo_exchanges_detail_series(
    okooo_id: int,
    *,
    timeout: float = 22.0,
    cookie: str | None = None,
    referer: str | None = None,
    max_pages: int = 5,
) -> dict[str, list[PriceVolumePoint]]:
    """GET 澳客必发成交明细 ``/soccer/match/{okooo_id}/exchanges/detail/``（及分页链接）。

    与浏览器访问一致：需 **Cookie**（推荐 ``OKOOO_COOKIE`` 写入 ``.env``，与 betfa 列表同源）。
    HTML 解析复用 ``okooo_ah_cli`` 内与 ``OkoooClient._fetch_exchanges_detail`` 相同的逻辑，避免重复维护表格规则。

    Returns:
        ``{\"home\"|\"draw\"|\"away\": [PriceVolumePoint, ...]}`` — 与 ``Predictor._trade_signal`` 所用 ``selection`` 键一致。
    """
    # okooo_ah_cli 不 import okooo_bifa，此处延迟 import 避免循环依赖。
    from okooo_ah_cli import (  # noqa: PLC0415
        DETAIL_HEADERS,
        decode_okooo,
        http_bytes,
        merge_price_volume_points,
        parse_detail_page_links,
        parse_exchanges_detail_html,
    )

    ck = (cookie or "").strip()
    if not ck:
        raise DataError("OKOOO_COOKIE is empty; Okooo exchanges/detail requires a browser cookie")

    base = OKOOO_EXCHANGES_DETAIL_TMPL.format(okooo_id=okooo_id)
    ref = (referer or os.environ.get("OKOOO_BETFA_URL") or OKOOO_BETFA_DEFAULT).strip()
    mp = max(1, int(max_pages))

    def fetch_html(url: str) -> str:
        raw = http_bytes(url, timeout=timeout, cookie=ck, referer=ref, extra_headers=DETAIL_HEADERS)
        text = decode_okooo(raw)
        sample = text[:4000].lower()
        if (
            "<title>405</title>" in sample
            or "your request has been blocked" in sample
            or "访问被阻断" in text[:8000]
        ):
            raise DataError("Okooo exchanges/detail returned WAF block page; refresh OKOOO_COOKIE from browser")
        return text

    pages: list[str] = [base]
    merged: dict[str, list[PriceVolumePoint]] = {"home": [], "draw": [], "away": []}
    first = fetch_html(base)
    merged = merge_price_volume_points(merged, parse_exchanges_detail_html(first))
    for link in parse_detail_page_links(first, okooo_id):
        if link not in pages:
            pages.append(link)
    for url in pages[1:mp]:
        merged = merge_price_volume_points(merged, parse_exchanges_detail_html(fetch_html(url)))
    return merged


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")


def _norm_team(s: str) -> str:
    t = _strip_tags(s).strip()
    t = re.sub(r"\s+", "", t)
    return t


def _parse_sel_tds(tds: list[str]) -> OkoooBifa1x2:
    def f(i: int, default: float = 0.0) -> float:
        if i >= len(tds):
            return default
        x = tds[i].strip().replace(",", "")
        if not x:
            return default
        if x.endswith("%"):
            try:
                return float(x[:-1])
            except ValueError:
                return default
        try:
            return float(x)
        except ValueError:
            return default

    return OkoooBifa1x2(
        amount=f(5),
        cold_index=f(6),
        market_index=f(7),
        odds=f(8),
        ratio_pct=f(9),
        payout=f(13),
    )


def _parse_row_tr(tr_html: str) -> list[str] | None:
    tds = re.findall(r"<td[^>]*>(.*?)</td>", tr_html, flags=re.S | re.I)
    if not tds:
        return None
    return [_strip_tags(td) for td in tds]


def _tr_block_for_outcome_label(block: str, label: str) -> str | None:
    """Return a single ``<tr>...</tr>`` whose first cell text equals ``label`` (主胜/平局/客胜)."""
    for m in re.finditer(r"<tr\b[^>]*>.*?</tr>", block, flags=re.S | re.I):
        chunk = m.group(0)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", chunk, flags=re.S | re.I)
        if not tds:
            continue
        if _strip_tags(tds[0]).strip() == label:
            return chunk
    return None


def parse_okooo_betfa_html(html: str) -> list[OkoooBifaMatch]:
    """Parse server-rendered betfa listing HTML."""
    out: list[OkoooBifaMatch] = []
    for block in html.split("container_wrapper betfa"):
        m_id = re.search(r"/soccer/match/(\d+)/odds/", block)
        if not m_id:
            continue
        oid = int(m_id.group(1))
        tm = re.search(r"<span>([^<]+)</span>.*?<strong>VS</strong><b>([^<]+)</b>", block, flags=re.S | re.I)
        if not tm:
            continue
        home, away = _norm_team(tm.group(1)), _norm_team(tm.group(2))
        tit = re.search(
            r"<div class=\"magazineDateTit[^\"]*\"[^>]*>\s*<p[^>]*>.*?<b>([^<]+)</b><b>([^<]+)</b><b>(\d{2}-\d{2}\s+\d{2}:\d{2})</b>",
            block,
            flags=re.S | re.I,
        )
        jc_code, league_tag, kick_md = ("", "", "")
        if tit:
            jc_code, league_tag, kick_md = tit.group(1).strip(), tit.group(2).strip(), tit.group(3).strip()
        rows_map: dict[str, OkoooBifa1x2] = {}
        for label in ("主胜", "平局", "客胜"):
            tr_html = _tr_block_for_outcome_label(block, label)
            if not tr_html:
                break
            tds = _parse_row_tr(tr_html)
            if not tds or len(tds) < 10:
                break
            rows_map[label] = _parse_sel_tds(tds)
        if len(rows_map) != 3:
            continue
        out.append(
            OkoooBifaMatch(
                okooo_id=oid,
                home=home,
                away=away,
                kickoff_md=kick_md,
                league_tag=league_tag,
                jc_code=jc_code,
                home_sel=rows_map["主胜"],
                draw_sel=rows_map["平局"],
                away_sel=rows_map["客胜"],
            )
        )
    return out


def _mdh_match(mt: datetime, kick_md: str, tz: ZoneInfo) -> bool:
    """``kick_md`` is MM-DD HH:MM in ``tz`` wall time; year from ``mt``."""
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2})$", kick_md.strip())
    if not m:
        return False
    month, day, hour, minute = (int(m.group(i)) for i in range(1, 5))
    local = mt.astimezone(tz)
    try:
        ref = datetime(local.year, month, day, hour, minute, tzinfo=tz)
    except ValueError:
        return False
    delta = abs((local - ref).total_seconds())
    return delta < 90 * 60


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def swapped_for_titan_vs_okooo(titan_home: str, titan_away: str, pick: OkoooBifaMatch) -> bool:
    """Whether Titan007 主/客应对调澳客表内主胜/客胜行（同 ``best_okooo_bifa_match`` 规则）。"""
    hn, an = _norm_team(titan_home), _norm_team(titan_away)
    s1 = min(_sim(hn, pick.home), _sim(an, pick.away))
    s2 = min(_sim(hn, pick.away), _sim(an, pick.home))
    return bool(s2 > s1)


def load_titan_to_okooo_id_map() -> dict[int, int]:
    """Load ``ScheduleID -> okooo soccer match id`` from JSON file and/or env.

    * ``OKOOO_TITAN_MAP_PATH``: JSON object, keys/values may be string or int
      (e.g. ``{{\"2906745\": 1316319}}``).
    * ``TITAN007_OKOOO_IDS``: comma-separated ``titan=okooo`` or ``titan:okooo``;
      entries override the file for the same ``titan`` key.
    """
    out: dict[int, int] = {}
    path = (os.environ.get("OKOOO_TITAN_MAP_PATH") or "").strip()
    if path:
        p = Path(path).expanduser()
        if not p.is_file():
            raise DataError(f"OKOOO_TITAN_MAP_PATH is not a file: {p}")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DataError(f"invalid OKOOO_TITAN_MAP_PATH JSON {p}: {exc}") from exc
        if isinstance(data, dict):
            for k, v in data.items():
                try:
                    out[int(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        else:
            raise DataError(f"OKOOO_TITAN_MAP_PATH must be a JSON object, got {type(data).__name__}")
    blob = (os.environ.get("TITAN007_OKOOO_IDS") or "").strip()
    if blob:
        for part in blob.split(","):
            part = part.strip()
            if not part:
                continue
            sep = "=" if "=" in part else (":" if ":" in part else "")
            if not sep:
                continue
            a, b = part.split(sep, 1)
            try:
                out[int(a.strip())] = int(b.strip())
            except ValueError:
                continue
    return out


def resolve_okooo_bifa_match(
    schedule_id: int,
    home: str,
    away: str,
    match_time_utc: datetime,
    candidates: list[OkoooBifaMatch],
    *,
    schedule_tz: ZoneInfo,
    titan_to_okooo: dict[int, int] | None = None,
    min_sim: float = 0.55,
) -> tuple[OkoooBifaMatch, bool, str] | None:
    """Pick one ``OkoooBifaMatch``: **id map first**, then name/time heuristic.

    Returns ``(pick, swapped, source)`` with ``source`` ``\"id_map\"`` or ``\"heuristic\"``,
    or ``None`` if no row applies.
    """
    tmap = titan_to_okooo or {}
    if schedule_id in tmap:
        want = tmap[schedule_id]
        for c in candidates:
            if c.okooo_id == want:
                return c, swapped_for_titan_vs_okooo(home, away, c), "id_map"
        return None
    bk = best_okooo_bifa_match(home, away, match_time_utc, candidates, schedule_tz=schedule_tz, min_sim=min_sim)
    if bk:
        return bk[0], bk[1], "heuristic"
    return None


def best_okooo_bifa_match(
    home: str,
    away: str,
    match_time_utc: datetime,
    candidates: list[OkoooBifaMatch],
    *,
    schedule_tz: ZoneInfo,
    min_sim: float = 0.55,
) -> tuple[OkoooBifaMatch, bool] | None:
    """Returns ``(row, swapped)`` if Titan007 home aligns with okooo's right column."""
    hn, an = _norm_team(home), _norm_team(away)
    best: tuple[float, OkoooBifaMatch, bool] | None = None
    for c in candidates:
        if not _mdh_match(match_time_utc, c.kickoff_md, schedule_tz):
            continue
        s1 = min(_sim(hn, c.home), _sim(an, c.away))
        s2 = min(_sim(hn, c.away), _sim(an, c.home))
        use_swapped = swapped_for_titan_vs_okooo(home, away, c)
        score = max(s1, s2)
        if score < min_sim:
            continue
        if best is None or score > best[0]:
            best = (score, c, use_swapped)
    if best is None:
        return None
    return best[1], best[2]


def merge_okooo_bifa_into_raw(raw: dict[str, Any], pick: OkoooBifaMatch, *, swapped: bool) -> None:
    """Write 1X2 必发 fields: ``Bf*Home`` = Titan007 主队胜平负对应 ok 主胜/平/客胜行。"""
    if not swapped:
        win_home, dr, win_away = pick.home_sel, pick.draw_sel, pick.away_sel
    else:
        win_home, dr, win_away = pick.away_sel, pick.draw_sel, pick.home_sel
    raw["BfIndexHome"] = win_home.ratio_pct
    raw["BfIndexDraw"] = dr.ratio_pct
    raw["BfIndexAway"] = win_away.ratio_pct
    raw["BfAmountHome"] = win_home.amount
    raw["BfAmountDraw"] = dr.amount
    raw["BfAmountAway"] = win_away.amount
    raw["BfPayoutHome"] = win_home.payout
    raw["BfPayoutDraw"] = dr.payout
    raw["BfPayoutAway"] = win_away.payout
    raw["BfOddsHome"] = win_home.odds
    raw["BfOddsDraw"] = dr.odds
    raw["BfOddsAway"] = win_away.odds
    raw["_okooo_match_id"] = pick.okooo_id
    raw["_okooo_bifa_swapped"] = swapped
    raw["_okooo_cold_home"] = win_home.cold_index
    raw["_okooo_market_home"] = win_home.market_index
    raw["_okooo_cold_draw"] = dr.cold_index
    raw["_okooo_market_draw"] = dr.market_index
    raw["_okooo_cold_away"] = win_away.cold_index
    raw["_okooo_market_away"] = win_away.market_index
