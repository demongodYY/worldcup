#!/usr/bin/env python3
"""Titan007 单场预测 + 可选「赛程 + 澳客 ID」同屏列出。

* ``predict``：加载 ``.env``、``refresh``、亚盘/欧赔 + 默认澳客必发，一次 ``analyze``（与 ``titan007_ah_cli predict --okooo-bifa`` 同源）。
* ``upcoming``：与 CLI ``upcoming`` 相同时间/联赛筛选，**额外列**澳客 ``okooo_id``（优先 ``TITAN007_OKOOO_IDS`` / 映射文件，否则队名+时间启发式）。

兼容：第一个参数为纯数字时视为 ``predict <match_id>``。

用法::

    python3 scripts/titan007_one_match.py 2906745
    python3 scripts/titan007_one_match.py predict 2906745 --okooo-ids 2906745=1316319
    python3 scripts/titan007_one_match.py upcoming --hours 72 --limit 30
    python3 scripts/titan007_one_match.py upcoming -w --hours 168 --limit 20
    # 等价: --world-cup / --wc
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 仓库根：…/WorldCup/scripts/thisfile.py -> parent.parent
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from okooo_bifa import OKOOO_EXCHANGES_DETAIL_TMPL, resolve_okooo_bifa_match  # noqa: E402
from titan007_ah_cli import (  # noqa: E402
    _cookie_from_env,
    _snapshot_dir,
    _upcoming_league_ok,
    cmd_predict,
)
from titan007_client import Titan007Client, _bf_match_timezone  # noqa: E402
from worldcup_ah_cli import (  # noqa: E402
    DataError,
    SnapshotStore,
    default_env_file_path,
    load_dotenv_file,
)


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--no-dotenv", action="store_true", help="不加载 .env")
    p.add_argument("--env-file", type=str, default=None, metavar="PATH", help="指定 .env 路径")
    p.add_argument(
        "--okooo-ids",
        type=str,
        default="",
        metavar="IDS",
        help="覆盖 TITAN007_OKOOO_IDS（如 2906745=1316319；多场逗号分隔）",
    )
    p.add_argument("--no-okooo", action="store_true", help="不拉澳客必发盈亏页（upcoming 则无 okooo 列匹配）")
    p.add_argument("--timeout", type=float, default=20.0, help="HTTP 超时秒")
    p.add_argument(
        "--snapshot-dir",
        type=str,
        default=None,
        metavar="DIR",
        help="快照目录（默认 .titan007_snapshots 或 TITAN007_SNAPSHOT_DIR）",
    )
    p.add_argument(
        "--schedule-source",
        choices=["bf", "live", "jc"],
        default=None,
        metavar="SRC",
        help="赛程源（默认读 TITAN007_SCHEDULE_SOURCE，未设则为 live）",
    )


def _apply_env_and_client(args: argparse.Namespace) -> tuple[Titan007Client, SnapshotStore]:
    if not args.no_dotenv:
        load_dotenv_file(Path(args.env_file) if args.env_file else default_env_file_path())
    if getattr(args, "okooo_ids", "").strip():
        os.environ["TITAN007_OKOOO_IDS"] = args.okooo_ids.strip()
    store = SnapshotStore(_snapshot_dir(args.snapshot_dir))
    client = Titan007Client(
        timeout=args.timeout,
        cookie=_cookie_from_env(),
        snapshot_store=store,
        schedule_source=args.schedule_source,
        okooo_bifa=not args.no_okooo,
    )
    return client, store


def cmd_upcoming_okooo(client: Titan007Client, args: argparse.Namespace) -> int:
    client.refresh_feeds()
    n = len(client.schedule_ids())
    print(f"已加载 Titan007 索引 {n} 场；澳客 betfa 解析 {len(client._okooo_rows or [])} 场。", flush=True)
    now = datetime.now(timezone.utc)
    hours = float(args.hours)
    limit = now + timedelta(hours=hours)
    upper = datetime(2100, 1, 1, tzinfo=timezone.utc) if args.all_future else limit
    league_kw = (args.league_contains or "").strip()
    world_cup = bool(args.world_cup)
    fetch_euro = args.fetch_euro
    tz = _bf_match_timezone()
    tmap = client._okooo_titan_map or {}
    rows_out: list[dict[str, object]] = []

    for sid in client.schedule_ids():
        m = client.build_match(sid, fetch_1x2=fetch_euro)
        if m.match_time <= now or m.match_time > upper:
            continue
        if not _upcoming_league_ok(m.league_name or "", league_kw, world_cup):
            continue
        okid: str | int = "-"
        src = "-"
        swapped: str | bool = "-"
        detail = ""
        if client.okooo_bifa_enabled and client._okooo_rows:
            res = resolve_okooo_bifa_match(
                sid,
                m.home,
                m.away,
                m.match_time,
                client._okooo_rows,
                schedule_tz=tz,
                titan_to_okooo=tmap,
            )
            if res:
                pick, sw, ssrc = res
                okid = pick.okooo_id
                src = ssrc
                swapped = sw
                detail = OKOOO_EXCHANGES_DETAIL_TMPL.format(okooo_id=pick.okooo_id)
            elif tmap.get(sid):
                okid = "(miss)"
                src = "id_map_miss"
        row = {
            "match_time_utc": m.match_time.isoformat(),
            "schedule_id": sid,
            "okooo_id": okid,
            "match_source": src,
            "okooo_swapped": swapped,
            "okooo_exchanges_detail": detail or None,
            "home": m.home,
            "away": m.away,
            "league": m.league_name or "",
            "asian_line": m.asian_line,
        }
        rows_out.append(row)

    rows_out.sort(key=lambda r: str(r["match_time_utc"]))

    if args.json:
        print(json.dumps(rows_out[: int(args.limit)], ensure_ascii=False, indent=2))
        return 0

    print(
        "ScheduleID\tokooo_id\tsource\tswap\t开赛(UTC)\t亚盘\t对阵\t联赛",
        flush=True,
    )
    for r in rows_out[: int(args.limit)]:
        sws = "-" if r["okooo_swapped"] in ("-", "", None) else ("Y" if r["okooo_swapped"] else "N")
        lg = str(r["league"] or "")
        print(
            f"{r['schedule_id']}\t{r['okooo_id']}\t{r['match_source']}\t{sws}\t"
            f"{r['match_time_utc']}\t{r['asian_line']}\t{r['home']} vs {r['away']}\t{lg}",
            flush=True,
        )
    if not rows_out:
        print("（窗口内没有比赛。）", flush=True)
    else:
        print("# okooo 成交明细: …/soccer/match/{okooo_id}/exchanges/detail/ ；source=id_map 为显式映射，heuristic 为队名+时间。", flush=True)
    return 0


def _build_predict_ns(
    *,
    match_id: int,
    no_refresh: bool,
    verbose: bool,
    as_json: bool,
) -> argparse.Namespace:
    return argparse.Namespace(
        match_id=match_id,
        no_refresh=no_refresh,
        verbose=verbose,
        json=as_json,
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].isdigit():
        argv = ["predict", *argv]

    root = argparse.ArgumentParser(
        description="Titan007：单场完整预测，或 upcoming 同时看 ScheduleID 与澳客 match id。",
    )
    sub = root.add_subparsers(dest="cmd", required=True)

    p_pred = sub.add_parser("predict", help="单场 refresh + build_match + analyze（默认澳客）")
    _add_common(p_pred)
    p_pred.add_argument("match_id", type=int, help="球探 ScheduleID")
    p_pred.add_argument("--no-refresh", action="store_true", help="不 refresh_feeds")
    p_pred.add_argument("--quiet", action="store_true", help="关闭 verbose")
    p_pred.add_argument("--json", action="store_true", help="输出 JSON")

    p_up = sub.add_parser("upcoming", help="未来场次 + 澳客 okooo_id（与 titan007_ah_cli upcoming 筛选一致）")
    _add_common(p_up)
    p_up.add_argument("--hours", type=float, default=48.0)
    p_up.add_argument("--limit", type=int, default=80)
    p_up.add_argument("--all-future", action="store_true", help="不按 --hours 截断上界")
    p_up.add_argument(
        "-w",
        "--wc",
        "--world-cup",
        dest="world_cup",
        action="store_true",
        help="只输出世界杯场次（联赛名含 世界杯 / 世界盃 / World Cup）；与 --league-contains 同时传时为 AND",
    )
    p_up.add_argument("--league-contains", type=str, default="", metavar="SUBSTR")
    p_up.add_argument(
        "--fetch-euro",
        action="store_true",
        help="每场请求 1x2d（慢）",
    )
    p_up.add_argument("--json", action="store_true", help="JSON 数组输出")

    args = root.parse_args(argv)

    client, store = _apply_env_and_client(args)

    if args.cmd == "predict":
        ns = _build_predict_ns(
            match_id=args.match_id,
            no_refresh=args.no_refresh,
            verbose=not args.quiet,
            as_json=args.json,
        )
        return cmd_predict(client, store, ns)
    if args.cmd == "upcoming":
        return cmd_upcoming_okooo(client, args)
    raise SystemExit(f"unknown cmd {args.cmd!r}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
