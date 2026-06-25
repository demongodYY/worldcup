#!/usr/bin/env python3
"""从澳客竞彩某日期的「必发 / 盘口 / 凯利」三页生成单条离线快照（jsonl 一行）。

用法（在仓库根）::

    python3 tools/import_okooo_jingcai_snapshots.py --issue 2026-06-15

依赖：本机可访问 okooo.cn；页面编码为 GB18030（与 ``okooo_ah_cli`` 解析一致）。
合并逻辑对齐 ``OkoooClient.build_match`` 中 betfa + pankou + peilv 部分。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import okooo_ah_cli as ok  # noqa: E402
from worldcup_ah_cli import line_depth, side_key, upper_lower_teams  # noqa: E402


def _fetch(path: str, dest: Path) -> str:
    import urllib.request

    # 避免 gzip 响应在 urllib 下未解压导致 HTML 过短、解析为空
    req = urllib.request.Request(
        path,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; worldcup-snapshot-import/1.0)",
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
        raw = resp.read()
    dest.write_bytes(raw)
    return raw.decode("gb18030", errors="replace")


def _merge_like_build_match(
    base: ok.OkoooBaseMatch,
    handicap: ok.OkoooHandicap | None,
    euro: ok.OkoooEuroKelly | None,
) -> dict[str, Any]:
    raw: dict[str, Any] = dict(base.raw)
    asian_line = base.lottery_handicap or "0"
    if handicap:
        asian_line = handicap.consensus_line
        raw["AsianAvrLet"] = asian_line
        raw["AsianAvrHome"] = handicap.avg_home_water
        raw["AsianAvrAway"] = handicap.avg_away_water
        raw["_okooo_handicap_rows"] = len(handicap.rows)
        raw["_okooo_handicap_line_samples"] = handicap.line_samples[:8]

    if euro:
        raw["EuroAvrHome"] = euro.current_home or raw.get("EuroAvrHome", 0)
        raw["EuroAvrDraw"] = euro.current_draw or raw.get("EuroAvrDraw", 0)
        raw["EuroAvrAway"] = euro.current_away or raw.get("EuroAvrAway", 0)
        raw["KellyHome"] = euro.kelly_home
        raw["KellyDraw"] = euro.kelly_draw
        raw["KellyAway"] = euro.kelly_away
        raw["_okooo_euro_bookmakers"] = euro.bookmaker_count

    # 与线上一致：欧赔隐含概率（若 peilv 已给出 EuroAvr）
    def _implied(p: float) -> float:
        if p <= 0:
            return 0.0
        inv = 1.0 / p
        s = inv
        return round(100.0 * inv / s, 2) if s > 0 else 0.0

    eh, ed, ea = (
        float(raw.get("EuroAvrHome") or 0),
        float(raw.get("EuroAvrDraw") or 0),
        float(raw.get("EuroAvrAway") or 0),
    )
    if eh > 0 and ed > 0 and ea > 0:
        s = 1.0 / eh + 1.0 / ed + 1.0 / ea
        raw["_okooo_euro_prob_home"] = round(100.0 * (1.0 / eh) / s, 2)
        raw["_okooo_euro_prob_draw"] = round(100.0 * (1.0 / ed) / s, 2)
        raw["_okooo_euro_prob_away"] = round(100.0 * (1.0 / ea) / s, 2)
    else:
        raw.setdefault("_okooo_euro_prob_home", _implied(eh))
        raw.setdefault("_okooo_euro_prob_draw", _implied(ed))
        raw.setdefault("_okooo_euro_prob_away", _implied(ea))

    # 历史导入只保留真实抓到的字段。缺失项由 Predictor 标记 unavailable，
    # 不再填固定占位值，以免回测把人工常数误当成赛前市场信息。
    expected_optional = (
        "_okooo_zhishu_home",
        "_okooo_zhishu_draw",
        "_okooo_zhishu_away",
        "_okooo_popularity_home",
        "_okooo_popularity_draw",
        "_okooo_popularity_away",
        "_okooo_diff_home",
        "_okooo_diff_draw",
        "_okooo_diff_away",
    )
    raw["_validation_missing_fields"] = [key for key in expected_optional if key not in raw]
    raw["_validation_eligible"] = False
    raw["_validation_exclusion_reason"] = "historical_post_match_import"

    from worldcup_ah_cli import Match

    m = Match(
        event_id=base.okooo_id,
        match_time=base.kickoff,
        home=base.home,
        away=base.away,
        league_id=None,
        league_name=base.league,
        asian_line=asian_line,
        is_stop_update=True,
        raw=raw,
    )
    upper_team, lower_team = upper_lower_teams(m)
    upper_key = side_key(m, upper_team)
    lower_key = side_key(m, lower_team)

    if handicap and upper_key in ("Home", "Away") and lower_key in ("Home", "Away"):
        d = line_depth(m.asian_line)
        if d > 0:
            raw["ModelFairLineDepth"] = float(d)
        if upper_key == "Home":
            raw["ExternalSpreadUpperPrice"] = handicap.avg_home_water
            raw["ExternalSpreadLowerPrice"] = handicap.avg_away_water
        else:
            raw["ExternalSpreadUpperPrice"] = handicap.avg_away_water
            raw["ExternalSpreadLowerPrice"] = handicap.avg_home_water

    z_by_key = {"Home": eh, "Away": ea}
    if upper_key in z_by_key and lower_key in z_by_key:
        uo = z_by_key.get(upper_key, 0.0)
        lo = z_by_key.get(lower_key, 0.0)
        if uo > 0 and lo > 0:
            raw["ExternalH2hUpperPrice"] = uo
            raw["ExternalH2hLowerPrice"] = lo

    return {
        "event_id": base.okooo_id,
        "home": base.home,
        "away": base.away,
        "asian_line": asian_line,
        "match_time": base.kickoff.isoformat(),
        "league_id": None,
        "league_name": base.league,
        "is_stop_update": True,
        "raw": raw,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--issue", required=True, help="日期 YYYY-MM-DD，如 2026-06-15")
    p.add_argument(
        "--only",
        type=int,
        nargs="*",
        default=None,
        help="仅导出指定 event_id（默认：该日 betfa 页全部解析到的比赛）",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / ".okooo_import_cache",
        help="原始 HTML 缓存目录",
    )
    args = p.parse_args()
    issue = args.issue.strip()
    cache = args.cache_dir
    cache.mkdir(parents=True, exist_ok=True)

    betfa_path = f"https://www.okooo.cn/jingcai/shuju/betfa/{issue}/"
    pankou_path = f"https://www.okooo.cn/jingcai/shuju/pankou/{issue}/"
    peilv_path = f"https://www.okooo.cn/jingcai/shuju/peilv/{issue}/"

    betfa_html = _fetch(betfa_path, cache / f"betfa_{issue}.html")
    pankou_html = _fetch(pankou_path, cache / f"pankou_{issue}.html")
    peilv_html = _fetch(peilv_path, cache / f"peilv_{issue}.html")

    bases = ok.parse_betfa_html(betfa_html)
    hcaps = ok.parse_pankou_html(pankou_html)
    euros = ok.parse_peilv_html(peilv_html)

    snap_dir = ROOT / ".okooo_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    ids = sorted(bases)
    if args.only:
        ids = [i for i in ids if i in set(args.only)]

    for oid in ids:
        base = bases[oid]
        rec = {
            "fetched_at": now,
            "match": _merge_like_build_match(base, hcaps.get(oid), euros.get(oid)),
            "schema": 2,
            "provenance": {
                "kind": "historical_import",
                "validation_eligible": False,
                "reason": "historical_post_match_import",
                "issue": issue,
            },
        }
        out = snap_dir / f"{oid}.jsonl"
        out.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {out.relative_to(ROOT)}  {base.home} vs {base.away}")


if __name__ == "__main__":
    main()
