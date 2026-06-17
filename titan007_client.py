#!/usr/bin/env python3
"""HTTP client for Titan007 / 球探 live feeds → structures consumed by worldcup_ah_cli.Predictor.

See docs/titan007_feeds.md for URL matrix and Referer rules.
"""

from __future__ import annotations

import os
import re
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from okooo_bifa import (
    OKOOO_EXCHANGES_DETAIL_TMPL,
    fetch_okooo_betfa_html,
    fetch_okooo_exchanges_detail_series,
    load_titan_to_okooo_id_map,
    merge_okooo_bifa_into_raw,
    parse_okooo_betfa_html,
    resolve_okooo_bifa_match,
)
from worldcup_ah_cli import (
    DataError,
    EuroTrendPoint,
    HandicapRow,
    Match,
    PriceVolumePoint,
    SnapshotStore,
    is_ssl_verify_error,
    parse_datetime_or_none,
)

LIVE_REFERER = "https://live.titan007.com/oldIndexall.aspx"
BF_REFERER = "https://bf.titan007.com/"
LIVE_STATIC = "https://livestatic.titan007.com"
BF_BFDATA = "https://bf.titan007.com/vbsxml/bfdata.js"
LIVE_BFDATA_UT = f"{LIVE_STATIC}/vbsxml/bfdata_ut.js"
JC_BF_JC_TXT = "https://jc.titan007.com/xml/bf_jc.txt"
JC_REFERER = "https://jc.titan007.com/index.aspx"
SB_ODDS_JS = f"{LIVE_STATIC}/vbsxml/sbOddsData.js"
CH_GOAL_BF3 = f"{LIVE_STATIC}/vbsxml/ch_goalbf3.xml"
ONE_X_TWO_JS = "https://1x2d.titan007.com/{schedule_id}.js"

# bfdata 场次时间列习惯为北京时间；可用 TITAN007_SCHEDULE_TZ 覆盖（IANA 名）


def _bf_match_timezone() -> ZoneInfo:
    name = os.environ.get("TITAN007_SCHEDULE_TZ", "Asia/Shanghai").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _http_text(url: str, referer: str, timeout: float, cookie: str | None) -> str:
    headers = {
        "User-Agent": "worldcup-titan007-cli/1.0 (+https://github.com/)",
        "Accept": "*/*",
        "Referer": referer,
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(url, headers=headers)
    last_exc: BaseException | None = None
    for ctx in (None, ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ctx) as resp:
                raw = resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            last_exc = exc
            if ctx is None and is_ssl_verify_error(exc):
                continue
            raise DataError(f"Titan007 GET failed {url}: {exc}") from exc
        enc = (resp.headers.get_content_charset() if hasattr(resp, "headers") else None) or "utf-8"
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            return raw.decode("gb18030", errors="replace")
    raise DataError(f"Titan007 GET failed {url}: {last_exc}")


def _bf_js_text(
    timeout: float,
    cookie: str | None,
    url: str | None = None,
    referer: str | None = None,
) -> str:
    """Fetch bfdata.js or livestatic bfdata_ut.js (same ``A[]`` shape)."""
    fetch_url = (url or BF_BFDATA).strip()
    ref = (referer or BF_REFERER).strip()
    if "livestatic.titan007.com" in fetch_url and "?" not in fetch_url:
        fetch_url = f"{fetch_url}?r=007{int(time.time() * 1000)}"
    raw_bytes = b""
    headers = {
        "User-Agent": "worldcup-titan007-cli/1.0",
        "Referer": ref,
    }
    if cookie:
        headers["Cookie"] = cookie
    request = urllib.request.Request(fetch_url, headers=headers)
    for ctx in (None, ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=ctx) as resp:
                raw_bytes = resp.read()
            break
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            if ctx is None and is_ssl_verify_error(exc):
                continue
            raise DataError(f"Titan007 schedule JS failed {fetch_url}: {exc}") from exc
    for enc in ("gb18030", "gbk", "utf-8"):
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def _parse_bf_match_time(s: str) -> datetime:
    """Parse Titan007 comma datetime (bfdata ``A[]`` 列 12、竞彩 ``bf_jc`` 计划开赛等).

    球探与 **JavaScript ``Date`` 一致**：**月份为 0–11**（0=一月，5=六月，11=十二月），
    不是 ISO 的 1–12。例：``2026,5,17,22,00,00`` 表示 **2026-06-17** 22:00（按
    ``TITAN007_SCHEDULE_TZ``，默认北京时间）再转 UTC。
    """
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if len(parts) < 6:
        raise ValueError(s)
    year, month_js, day, hour, minute, second = (int(parts[i]) for i in range(6))
    if not 0 <= month_js <= 11:
        raise ValueError(f"month out of 0..11: {s!r}")
    month_py = month_js + 1
    local = datetime(year, month_py, day, hour, minute, second, tzinfo=_bf_match_timezone())
    return local.astimezone(timezone.utc)


def _parse_1x2_row_time(s: str) -> datetime | None:
    """Best-effort parse for 1x2d timestamp strings (formats vary)."""
    s = s.strip()
    if not s:
        return None
    return parse_datetime_or_none(s.replace(",", "-", 1))


def parse_bfdata_rows(js_text: str) -> dict[int, dict[str, Any]]:
    """Return schedule_id → row dict from bfdata.js A[i] assignments."""
    rows: dict[int, dict[str, Any]] = {}
    for m in re.finditer(r'A\[(\d+)\]\s*=\s*"([^"]*)"\.split\(\'\^\'\)', js_text):
        idx = int(m.group(1))
        parts = m.group(2).split("^")
        if len(parts) < 30:
            continue
        try:
            sid = int(parts[0])
        except ValueError:
            continue
        rows[sid] = {
            "array_index": idx,
            "parts": parts,
            "league_name": _strip_html(parts[2]),
            "home_cn": _strip_html(parts[5]),
            "away_cn": _strip_html(parts[8]),
            "home_en": _strip_html(parts[7]),
            "away_en": _strip_html(parts[10]),
            "match_time_str": parts[12],
            "asian_line_hint": parts[29].strip() if len(parts) > 29 else "",
            "league_id": int(parts[45]) if len(parts) > 45 and parts[45].isdigit() else None,
        }
    return rows


def parse_bf_jc_rows(text: str) -> dict[int, dict[str, Any]]:
    """Parse 竞彩页 ``xml/bf_jc.txt`` into the same row dict shape as ``parse_bfdata_rows``.

    jc.titan007.com/index.aspx 的 ``football.js`` 用 ``xml/bf_jc.txt``；``$`` 分隔场次，
    段首 ``!`` 分隔联赛块。单场：``sid``、计划开赛 ``parts[1]``、联赛类 ``parts[5]``、
    主队三字段 ``parts[8]``、客队 ``parts[10]``（与 bfdata ``A[]`` 列位不同）、让球参考 ``parts[22]``（若有）。
    """
    rows: dict[int, dict[str, Any]] = {}
    if not text or "$" not in text:
        return rows
    head, *match_segs = text.split("$")
    sclass_leagues: dict[int, str] = {}
    for chunk in head.split("!"):
        p = chunk.split("^")
        if len(p) < 4 or not p[0].isdigit():
            continue
        cid = int(p[0])
        raw_name = p[3] if len(p) > 3 else ""
        sclass_leagues[cid] = _strip_html(raw_name.split(",")[0] if raw_name else "")
    for idx, seg in enumerate(match_segs, start=1):
        parts = seg.split("^")
        if len(parts) < 11:
            continue
        try:
            sid = int(parts[0])
        except ValueError:
            continue
        match_time_str = parts[1].strip()
        if not re.match(r"^\d{4},\d{1,2},\d{1,2},\d{1,2},\d{1,2},\d{1,2}$", match_time_str):
            continue
        sclass_id: int | None = None
        if parts[5].isdigit():
            sclass_id = int(parts[5])
        league_name = sclass_leagues.get(sclass_id, "") if sclass_id is not None else ""
        home_triple = parts[8] if len(parts) > 8 else ""
        away_triple = parts[10] if len(parts) > 10 else ""
        home_cn = _strip_html(home_triple.split(",")[0] if home_triple else "")
        away_cn = _strip_html(away_triple.split(",")[0] if away_triple else "")
        asian_hint = parts[22].strip() if len(parts) > 22 else ""
        rows[sid] = {
            "array_index": idx,
            "parts": parts,
            "league_name": league_name,
            "home_cn": home_cn,
            "away_cn": away_cn,
            "home_en": home_cn,
            "away_en": away_cn,
            "match_time_str": match_time_str,
            "asian_line_hint": asian_hint,
            "league_id": sclass_id,
        }
    return rows


def coerce_asian_triplet(a: float, b: float, c: float) -> tuple[float, float, float] | None:
    """Normalize to (line, home_water, away_water).

    Rows may pack either ``home,line,away`` or ``line,home,away``. Asian waters are
    typically ~0.65–1.20; lines fall in ~[-4.25, 4.25] but overlap weakly at fringes.
    Prefer ``home,line,away`` when the first value looks like a water.
    """
    w_lo, w_hi = 0.65, 1.20
    if w_lo <= a <= w_hi and -4.25 <= b <= 4.25 and w_lo <= c <= w_hi:
        return b, a, c
    if -4.25 <= a <= 4.25 and w_lo <= b <= w_hi and w_lo <= c <= w_hi:
        return a, b, c
    return None


def parse_ch_goalbf3_asian(m_csv: str) -> tuple[float, float, float] | None:
    """Return (line, home_water, away_water) from first plausible triplet in <m> CSV."""
    p = m_csv.split(",")
    for i in range(2, min(len(p) - 2, 36)):
        try:
            a, b, c = float(p[i]), float(p[i + 1]), float(p[i + 2])
        except (ValueError, IndexError):
            continue
        tri = coerce_asian_triplet(a, b, c)
        if tri:
            return tri
    return None


def parse_ch_goalbf3_map(xml_text: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for m in re.finditer(r"<m>([^<]+)</m>", xml_text):
        body = m.group(1)
        head = body.split(",", 1)[0]
        if head.isdigit():
            out[int(head)] = body
    return out


def parse_sb_odds_map(js_text: str) -> dict[int, list[list[float]]]:
    """Parse ``sData[sid]=[[...],[...]]`` assignments (one ``;``-terminated statement each)."""
    out: dict[int, list[list[float]]] = {}
    for part in js_text.split(";"):
        part = part.strip()
        if not part.startswith("sData["):
            continue
        m = re.match(r"sData\[(\d+)\]\s*=\s*(.+)$", part)
        if not m:
            continue
        sid = int(m.group(1))
        rhs = m.group(2).strip()
        rows: list[list[float]] = []
        if rhs.startswith("[[") and rhs.endswith("]]"):
            inner = rhs[1:-1]  # drop outer [ ]
            for row_chunk in inner.split("],["):
                row_chunk = row_chunk.strip().strip("[]")
                nums: list[float] = []
                for token in row_chunk.split(","):
                    token = token.strip()
                    if not token or token == "undefined":
                        continue
                    try:
                        nums.append(float(token))
                    except ValueError:
                        continue
                if len(nums) >= 6:
                    rows.append(nums)
        if rows:
            out[sid] = rows
    return out


def first_plausible_handicap_from_sb(rows: list[list[float]]) -> tuple[float, float, float] | None:
    """First plausible Asian triplet as (line, home_water, away_water)."""
    for nums in rows:
        for start in range(0, len(nums) - 2, 3):
            tri = coerce_asian_triplet(nums[start], nums[start + 1], nums[start + 2])
            if tri:
                return tri
    return None


def sb_rows_to_handicap_rows(
    rows: list[list[float]], asian_line: str, _schedule_id: int
) -> list[HandicapRow]:
    target = _line_float(asian_line)
    out: list[HandicapRow] = []
    for idx, nums in enumerate(rows[:8]):
        best: tuple[float, float, float, float, float, float] | None = None
        best_dist = 1e9
        for start in range(0, len(nums) - 5, 3):
            tri = coerce_asian_triplet(nums[start], nums[start + 1], nums[start + 2])
            tri_init = coerce_asian_triplet(nums[start + 3], nums[start + 4], nums[start + 5])
            if not tri or not tri_init:
                continue
            line, h, a = tri
            iline, ih, ia = tri_init
            if not (0.4 <= h <= 1.35 and 0.4 <= a <= 1.35 and -4.25 <= line <= 4.25):
                continue
            dist = abs(line - target)
            if dist < best_dist:
                best_dist = dist
                best = (h, a, ih, ia, line, iline)
        if best is None:
            continue
        h, a, ih, ia, line, iline = best
        payout = 0.0
        if h > 0 and a > 0:
            inv = 1.0 / h + 1.0 / a
            if inv > 0:
                payout = 1.0 / inv
        out.append(
            HandicapRow(
                bookmaker_id=1000 + idx,
                name=f"公司{idx + 1}",
                sec_a=h,
                sec_b=a,
                init_sec_a=ih,
                init_sec_b=ia,
                payout=payout,
                update_time=datetime.now(timezone.utc),
                source="live",
            )
        )
    return out


def _line_float(line: str) -> float:
    try:
        return float(str(line).replace("半球", "").strip())
    except (TypeError, ValueError):
        return 0.0


def adjust_titan007_asian_line_sign(line_str: str, home_w: float, away_w: float) -> str:
    """Align ``AsianAvrLet`` sign with ``worldcup_ah_cli.upper_lower_teams`` (主让 → 负盘口).

    Titan007 ``bfdata`` / ``ch_goalbf3`` often expose **only magnitude** (e.g. ``1.0``) while
    the live Asian page lists **主队 | 盘 | 客队** so the favourite (lower water) is on the
    home side for 主让. ``line_value > 0`` would wrongly map **客队=上盘**; negate when
    home water is clearly lower than away. **Note:** on 一球/半球, **受让方低水** is common,
    so this alone can mis-fire; ``finalize_titan007_asian_line_sign_from_ml`` uses 1x2 / 必发
    胜赔 when available.

    Disable via ``TITAN007_ASIAN_SIGN_FROM_WATER=0`` if you need raw feed sign.
    """
    if not _env_bool("TITAN007_ASIAN_SIGN_FROM_WATER", True):
        return line_str
    s = str(line_str).strip().replace("+", "")
    try:
        v = float(s)
    except ValueError:
        return line_str
    if v <= 0:
        return line_str
    if home_w <= 0 or away_w <= 0:
        return line_str
    diff = away_w - home_w  # >0 → home has lower water (typical 让球方)
    thr = 0.02
    if abs(diff) < thr:
        return line_str
    if diff > thr:
        neg = -abs(v)
        t = f"{neg:.3f}".rstrip("0").rstrip(".")
        return "-0" if t in ("-0", "-0.") else t
    return line_str


def _format_titan007_line_value(v: float) -> str:
    t = f"{v:.3f}".rstrip("0").rstrip(".")
    return "-0" if t in ("-0", "-0.") else t


def _ml_clear_favorite_side(
    home_price: float, away_price: float, *, min_price: float, gap: float
) -> str | None:
    """Return ``\"home\"`` / ``\"away\"`` when one win price is clearly shorter, else ``None``."""
    if home_price < min_price or away_price < min_price:
        return None
    if away_price - home_price > gap:
        return "home"
    if home_price - away_price > gap:
        return "away"
    return None


def finalize_titan007_asian_line_sign_from_ml(raw: dict[str, Any]) -> None:
    """Align ``AsianAvrLet`` sign with 1x2 / 必发胜赔 when Asian 均水 heuristic is wrong.

    ``adjust_titan007_asian_line_sign`` uses **主队水位低于客队 → 主让 → 负盘口**. On
    半球~一球, **受让方低水** is common, so a **+1** line can remain even when the home
    side is clearly the ML favourite (主让一球). Prefer averaged **Euro** win prices from
    ``1x2d`` when present; otherwise **BfOdds*** after okooo merge.

    - ML home favourite + positive ``AsianAvrLet`` → **negative** (``upper_lower_teams`` 主让).
    - ML away favourite + negative ``AsianAvrLet`` → **positive**.

    Disable via ``TITAN007_ASIAN_SIGN_FROM_ML=0``. Intended to run after 1x2 fetch and
    optional okooo ``Bf*`` merge inside ``build_match``.
    """
    if not _env_bool("TITAN007_ASIAN_SIGN_FROM_ML", True):
        return
    let_s = str(raw.get("AsianAvrLet") or "0").strip().replace("+", "")
    try:
        v = float(let_s)
    except ValueError:
        return
    if v == 0 or abs(v) > 3.5:
        return

    eh = float(raw.get("EuroAvrHome") or 0.0)
    ea = float(raw.get("EuroAvrAway") or 0.0)
    bh = float(raw.get("BfOddsHome") or 0.0)
    ba = float(raw.get("BfOddsAway") or 0.0)

    fav: str | None = _ml_clear_favorite_side(eh, ea, min_price=1.05, gap=0.12)
    if fav is None:
        fav = _ml_clear_favorite_side(bh, ba, min_price=1.05, gap=0.22)

    if fav is None:
        return

    if v > 0 and fav == "home":
        raw["AsianAvrLet"] = _format_titan007_line_value(-abs(v))
        raw["_titan007_asian_line_signed_by_ml"] = True
    elif v < 0 and fav == "away":
        raw["AsianAvrLet"] = _format_titan007_line_value(abs(v))
        raw["_titan007_asian_line_signed_by_ml"] = True


def parse_1x2_js(js_text: str) -> tuple[list[dict[str, Any]], str, str, str]:
    """Return (game_rows, hometeam_cn, guestteam_cn, match_time_var)."""
    rows: list[dict[str, Any]] = []
    m = re.search(r"game\s*=\s*Array\((.*?)\)\s*;", js_text, re.S)
    if not m:
        return rows, "", "", ""
    inner = m.group(1)
    for quoted in re.findall(r'"((?:[^"\\]|\\.)*)"', inner):
        text = bytes(quoted, "utf-8").decode("unicode_escape")
        parts = text.split("|")
        rows.append({"parts": parts})
    hc = re.search(r"var\s+hometeam_cn\s*=\s*\"([^\"]*)\";", js_text)
    gc = re.search(r"var\s+guestteam_cn\s*=\s*\"([^\"]*)\";", js_text)
    mt = re.search(r"var\s+MatchTime\s*=\s*\"([^\"]*)\";", js_text)
    return (
        rows,
        (hc.group(1) if hc else "").strip(),
        (gc.group(1) if gc else "").strip(),
        (mt.group(1) if mt else "").strip(),
    )


def euro_rows_from_1x2(game_rows: list[dict[str, Any]]) -> list[EuroTrendPoint]:
    points: list[EuroTrendPoint] = []
    for item in game_rows[: min(40, len(game_rows))]:
        p = item["parts"]
        if len(p) < 21:
            continue
        try:
            home_i = float(p[3])
            draw_i = float(p[4])
            away_i = float(p[5])
            home_o = float(p[10])
            draw_o = float(p[11])
            away_o = float(p[12])
            hk = float(p[17]) if len(p) > 17 else 0.0
            dk = float(p[18]) if len(p) > 18 else 0.0
            ak = float(p[19]) if len(p) > 19 else 0.0
        except (ValueError, IndexError):
            continue
        rt = _parse_1x2_row_time(p[20]) if len(p) > 20 else None
        points.append(
            EuroTrendPoint(
                refresh_time=rt,
                home_price=home_i,
                draw_price=draw_i,
                away_price=away_i,
                home_kelly=hk,
                draw_kelly=dk,
                away_kelly=ak,
            )
        )
        points.append(
            EuroTrendPoint(
                refresh_time=rt,
                home_price=home_o,
                draw_price=draw_o,
                away_price=away_o,
                home_kelly=hk,
                draw_kelly=dk,
                away_kelly=ak,
            )
        )
    # de-dup by time + prices
    deduped: list[EuroTrendPoint] = []
    seen: set[tuple[Any, ...]] = set()
    for pt in points:
        key = (pt.refresh_time, pt.home_price, pt.draw_price, pt.away_price)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(pt)
    return deduped[:80]


def synthetic_price_volume_from_snapshots(
    store: SnapshotStore | None,
    event_id: int,
    selection: str,
) -> list[PriceVolumePoint]:
    """Build PriceVolumePoint series from saved JSONL snapshots (see plan: synthetic tradeflow)."""
    if store is None:
        raise DataError("no snapshot store configured for synthetic tradeflow")
    records = store.load_event(event_id)
    if len(records) < 2:
        raise DataError("need at least two snapshots for synthetic tradeflow")
    points: list[PriceVolumePoint] = []
    prev_euro: float | None = None
    prev_bf: float | None = None
    for rec in records:
        match_info = rec.get("match")
        if not isinstance(match_info, dict):
            continue
        raw = match_info.get("raw")
        if not isinstance(raw, dict):
            continue
        ts = parse_datetime_or_none(rec.get("fetched_at"))
        sel = selection.lower()
        if sel == "home":
            euro = float(raw.get("EuroAvrHome") or 0.0)
            bf = float(raw.get("BfOddsHome") or 0.0)
        elif sel == "away":
            euro = float(raw.get("EuroAvrAway") or 0.0)
            bf = float(raw.get("BfOddsAway") or 0.0)
        else:
            euro = float(raw.get("EuroAvrDraw") or 0.0)
            bf = float(raw.get("BfOddsDraw") or 0.0)
        price = bf if bf > 0 else euro
        if price <= 0:
            continue
        vol = 0.0
        if prev_euro is not None and euro > 0:
            vol += abs(euro - prev_euro) * 50000.0
        if prev_bf is not None and bf > 0:
            vol += abs(bf - prev_bf) * 20000.0
        vol = max(vol, 1.0)
        points.append(PriceVolumePoint(price=price, volume=vol, update_time=ts, attr=None))
        prev_euro, prev_bf = euro, bf
    if len(points) < 2:
        raise DataError("synthetic tradeflow produced fewer than two points")
    return points


class Titan007Client:
    """SpdexClient-compatible surface for Predictor (subset of endpoints)."""

    def __init__(
        self,
        *,
        timeout: float = 18.0,
        cookie: str | None = None,
        snapshot_store: SnapshotStore | None = None,
        referer_live: str = LIVE_REFERER,
        referer_bf: str = BF_REFERER,
        schedule_source: str | None = None,
        okooo_bifa: bool | None = None,
    ) -> None:
        """``okooo_bifa``: ``True``/``False`` 强制开关；``None`` 时读 ``TITAN007_OKOOO_BIFA``。"""
        self.timeout = timeout
        self.cookie = cookie or None
        self.snapshot_store = snapshot_store
        self.referer_live = referer_live
        self.referer_bf = referer_bf
        src = (schedule_source or os.environ.get("TITAN007_SCHEDULE_SOURCE") or "live").strip().lower()
        if src not in ("bf", "live", "jc"):
            src = "live"
        self.schedule_source = src
        self._cache_ts = str(int(time.time() * 1000))
        self._bf_rows: dict[int, dict[str, Any]] | None = None
        self._goal_map: dict[int, str] | None = None
        self._sb_map: dict[int, list[list[float]]] | None = None
        self._euro_cache: dict[int, list[EuroTrendPoint]] = {}
        self.okooo_bifa_enabled = okooo_bifa if okooo_bifa is not None else _env_bool("TITAN007_OKOOO_BIFA", False)
        self._okooo_rows: list[Any] | None = None
        self._okooo_load_error: str | None = None
        self._okooo_titan_map: dict[int, int] = {}
        self._okooo_map_load_error: str | None = None
        self._okooo_exchanges_pv_by_schedule: dict[int, dict[str, list[PriceVolumePoint]]] = {}
        self._exchanges_okooo_id_by_schedule: dict[int, int] = {}

    def _ts(self) -> str:
        return str(int(time.time() * 1000))

    def refresh_feeds(self) -> None:
        """Reload schedule (bf / live / 竞彩 jc) + ch_goalbf3 + sbOdds."""
        if self.schedule_source == "jc":
            jc_txt = _http_text(JC_BF_JC_TXT, JC_REFERER, self.timeout, self.cookie)
            self._bf_rows = parse_bf_jc_rows(jc_txt)
        elif self.schedule_source == "live":
            bf_js = _bf_js_text(self.timeout, self.cookie, LIVE_BFDATA_UT, LIVE_REFERER)
            self._bf_rows = parse_bfdata_rows(bf_js)
        else:
            bf_js = _bf_js_text(self.timeout, self.cookie, BF_BFDATA, self.referer_bf)
            self._bf_rows = parse_bfdata_rows(bf_js)
        gxml = _http_text(f"{CH_GOAL_BF3}?r=007{self._ts()}", self.referer_live, self.timeout, self.cookie)
        self._goal_map = parse_ch_goalbf3_map(gxml)
        sb_js = _http_text(f"{SB_ODDS_JS}?r=007{self._ts()}", self.referer_live, self.timeout, self.cookie)
        self._sb_map = parse_sb_odds_map(sb_js)
        self._okooo_exchanges_pv_by_schedule.clear()
        try:
            self._okooo_titan_map = load_titan_to_okooo_id_map()
            self._okooo_map_load_error = None
        except DataError as exc:
            self._okooo_titan_map = {}
            self._okooo_map_load_error = str(exc)[:200]
        if self.okooo_bifa_enabled:
            try:
                u = os.environ.get("OKOOO_BETFA_URL", "").strip() or None
                ok_cookie = (os.environ.get("OKOOO_COOKIE") or "").strip() or self.cookie
                html = fetch_okooo_betfa_html(url=u, timeout=self.timeout, cookie=ok_cookie)
                self._okooo_rows = parse_okooo_betfa_html(html)
                self._okooo_load_error = None
            except DataError as exc:
                self._okooo_rows = []
                self._okooo_load_error = str(exc)[:200]
        else:
            self._okooo_rows = None
            self._okooo_load_error = None

    def _ensure(self) -> None:
        if self._bf_rows is None:
            self.refresh_feeds()

    def schedule_ids(self) -> list[int]:
        self._ensure()
        assert self._bf_rows is not None
        return sorted(self._bf_rows.keys())

    def build_match(self, schedule_id: int, *, fetch_1x2: bool = True) -> Match:
        """Build Match from cached bfdata + goal/sb maps.

        When ``fetch_1x2`` is False (e.g. ``upcoming`` listing), skip per-match
        ``1x2d.titan007.com/{id}.js`` — otherwise hundreds of sequential HTTP calls
        will block for a long time.
        """
        self._ensure()
        assert self._bf_rows is not None and self._goal_map is not None and self._sb_map is not None
        row = self._bf_rows.get(schedule_id)
        if not row:
            raise DataError(f"unknown Titan007 schedule_id={schedule_id}")
        # bf A[]：5/8 为简中主客，6/9 为繁中主客，7/10 为英文名；8、9 不是「主 vs 客」各一列。
        home = row["home_cn"] or row["home_en"]
        away = row["away_cn"] or row["away_en"]
        mt = _parse_bf_match_time(row["match_time_str"])
        asian_line = row["asian_line_hint"] or "0"
        try:
            al_f = float(asian_line)
            if not (-4.25 <= al_f <= 4.25):
                asian_line = "0"
        except ValueError:
            asian_line = "0"
        if asian_line in ("0", "0.0", ""):
            srows = self._sb_map.get(schedule_id)
            if srows:
                tri_sb = first_plausible_handicap_from_sb(srows)
                if tri_sb:
                    asian_line = str(tri_sb[0])

        raw: dict[str, Any] = {
            "_source": "titan007",
            "AsianAvrLet": asian_line,
            "BfIndexHome": 0.0,
            "BfIndexAway": 0.0,
            "BfIndexDraw": 0.0,
            "BfAmountHome": 0.0,
            "BfAmountAway": 0.0,
            "BfAmountDraw": 0.0,
            "BfPayoutHome": 0.0,
            "BfPayoutAway": 0.0,
            "BfPayoutDraw": 0.0,
            "BfOddsHome": 0.0,
            "BfOddsDraw": 0.0,
            "BfOddsAway": 0.0,
            "EuroAvrHome": 0.0,
            "EuroAvrDraw": 0.0,
            "EuroAvrAway": 0.0,
            "KellyHome": 0.0,
            "KellyDraw": 0.0,
            "KellyAway": 0.0,
            "AsianAvrHome": 0.0,
            "AsianAvrAway": 0.0,
        }

        mbody = self._goal_map.get(schedule_id)
        if mbody:
            tri = parse_ch_goalbf3_asian(mbody)
            if tri:
                line, h, a = tri
                raw["AsianAvrLet"] = str(line)
                raw["AsianAvrHome"] = h
                raw["AsianAvrAway"] = a
        if (raw.get("AsianAvrHome") in (0, 0.0) or raw.get("AsianAvrAway") in (0, 0.0)) and self._sb_map.get(schedule_id):
            srows = self._sb_map[schedule_id]
            tri_sb = first_plausible_handicap_from_sb(srows)
            if tri_sb:
                line, h, a = tri_sb
                raw["AsianAvrLet"] = str(line)
                raw["AsianAvrHome"] = h
                raw["AsianAvrAway"] = a

        hw = float(raw.get("AsianAvrHome") or 0.0)
        aw = float(raw.get("AsianAvrAway") or 0.0)
        let0 = str(raw.get("AsianAvrLet") or "0")
        let1 = adjust_titan007_asian_line_sign(let0, hw, aw)
        if let1 != let0:
            raw["_titan007_asian_line_feed"] = let0
        raw["AsianAvrLet"] = let1

        # 1x2 averages for Euro / Kelly (one HTTP per match — off for bulk listing)
        if fetch_1x2:
            try:
                js = _http_text(
                    ONE_X_TWO_JS.format(schedule_id=schedule_id),
                    LIVE_REFERER,
                    self.timeout,
                    self.cookie,
                )
                game_rows, _, _, _ = parse_1x2_js(js)
                if game_rows:
                    hs, ds, as_ = [], [], []
                    khs, kds, kas = [], [], []
                    for item in game_rows[:15]:
                        p = item["parts"]
                        if len(p) < 20:
                            continue
                        try:
                            hs.append(float(p[3]))
                            ds.append(float(p[4]))
                            as_.append(float(p[5]))
                            khs.append(float(p[17]))
                            kds.append(float(p[18]))
                            kas.append(float(p[19]))
                        except (ValueError, IndexError):
                            continue
                    if hs:
                        raw["EuroAvrHome"] = sum(hs) / len(hs)
                        raw["EuroAvrDraw"] = sum(ds) / len(ds)
                        raw["EuroAvrAway"] = sum(as_) / len(as_)
                        raw["KellyHome"] = sum(khs) / len(khs)
                        raw["KellyDraw"] = sum(kds) / len(kds)
                        raw["KellyAway"] = sum(kas) / len(kas)
                        raw["_kelly_from_1x2_avg"] = True
            except DataError as exc:
                raw["_titan007_1x2_error"] = str(exc)[:200]

        if self._okooo_load_error:
            raw["_okooo_feed_error"] = self._okooo_load_error
        if self._okooo_map_load_error:
            raw["_okooo_map_load_error"] = self._okooo_map_load_error
        if self.okooo_bifa_enabled and self._okooo_rows:
            res = resolve_okooo_bifa_match(
                schedule_id,
                home,
                away,
                mt,
                self._okooo_rows,
                schedule_tz=_bf_match_timezone(),
                titan_to_okooo=self._okooo_titan_map,
            )
            if res:
                pick, swapped, src = res
                merge_okooo_bifa_into_raw(raw, pick, swapped=swapped)
                raw["_okooo_match_source"] = src
                raw["_okooo_exchanges_detail"] = OKOOO_EXCHANGES_DETAIL_TMPL.format(okooo_id=pick.okooo_id)
            elif self._okooo_titan_map.get(schedule_id):
                raw["_okooo_id_map_miss"] = True
            else:
                raw["_okooo_match_skipped"] = True

        finalize_titan007_asian_line_sign_from_ml(raw)

        oid_detail = raw.get("_okooo_match_id")
        if oid_detail is None and self._okooo_titan_map.get(schedule_id):
            oid_detail = self._okooo_titan_map.get(schedule_id)
        if oid_detail is not None:
            try:
                self._exchanges_okooo_id_by_schedule[schedule_id] = int(oid_detail)
            except (TypeError, ValueError):
                pass

        return Match(
            event_id=schedule_id,
            match_time=mt,
            home=home,
            away=away,
            league_id=row.get("league_id"),
            league_name=row.get("league_name", ""),
            asian_line=str(raw["AsianAvrLet"]),
            is_stop_update=False,
            raw=raw,
        )

    def handicap_list(self, event_id: int, asian_line: str) -> list[HandicapRow]:
        self._ensure()
        assert self._sb_map is not None
        rows = self._sb_map.get(event_id)
        if not rows:
            raise DataError(f"Titan007 sbOddsData has no sData[{event_id}]")
        return sb_rows_to_handicap_rows(rows, asian_line, event_id)

    def euro_trend(self, event_id: int) -> list[EuroTrendPoint]:
        if event_id in self._euro_cache:
            return self._euro_cache[event_id]
        js = _http_text(ONE_X_TWO_JS.format(schedule_id=event_id), LIVE_REFERER, self.timeout, self.cookie)
        game_rows, _, _, _ = parse_1x2_js(js)
        pts = euro_rows_from_1x2(game_rows)
        self._euro_cache[event_id] = pts
        return pts

    def price_volume(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        okid = self._exchanges_okooo_id_by_schedule.get(event_id)
        ok_cookie = (os.environ.get("OKOOO_COOKIE") or "").strip() or (self.cookie or "").strip()
        if okid and ok_cookie:
            if event_id not in self._okooo_exchanges_pv_by_schedule:
                try:
                    self._okooo_exchanges_pv_by_schedule[event_id] = fetch_okooo_exchanges_detail_series(
                        int(okid),
                        timeout=self.timeout,
                        cookie=ok_cookie,
                    )
                except DataError:
                    self._okooo_exchanges_pv_by_schedule[event_id] = {"home": [], "draw": [], "away": []}
            block = self._okooo_exchanges_pv_by_schedule[event_id].get(selection, []) or []
            if block:
                return list(block)

        return synthetic_price_volume_from_snapshots(self.snapshot_store, event_id, selection)

    def newspdex_tradeflow(self, event_id: int, selection: str) -> list[PriceVolumePoint]:
        """Not used when _source != newspdex; implemented to mirror SpdexClient."""
        return self.price_volume(event_id, selection)
