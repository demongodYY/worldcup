#!/usr/bin/env python3
"""Asian-handicap helper using Titan007 / 球探 HTTP feeds + the same Predictor as worldcup_ah_cli."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from okooo_bifa import resolve_okooo_bifa_match  # noqa: E402
from titan007_client import (
    CH_GOAL_BF3,
    LIVE_REFERER,
    SB_ODDS_JS,
    Titan007Client,
    _bf_match_timezone,
    _http_text,
)
from worldcup_ah_cli import (
    DataError,
    Predictor,
    SnapshotStore,
    default_env_file_path,
    load_dotenv_file,
    print_analysis,
)


def _cookie_from_env() -> str | None:
    v = os.environ.get("TITAN007_COOKIE", "").strip()
    return v or None


def _snapshot_dir(cli_value: str | None) -> str:
    return (cli_value or os.environ.get("TITAN007_SNAPSHOT_DIR") or ".titan007_snapshots").strip()


LEAGUE_FILTER_ALIASES: dict[str, tuple[str, ...]] = {
    "world_cup": ("世界杯", "世界盃", "world cup"),
    "premier_league": ("英超", "英格兰超级联赛", "英格蘭超級聯賽", "premier league", "epl"),
    "la_liga": ("西甲", "西班牙甲级联赛", "西班牙甲級聯賽", "la liga", "laliga", "primera division", "primera división"),
    "bundesliga": ("德甲", "德国甲级联赛", "德國甲級聯賽", "bundesliga"),
}

LEAGUE_FILTER_LABELS: dict[str, str] = {
    "world_cup": "世界杯",
    "premier_league": "英超",
    "la_liga": "西甲",
    "bundesliga": "德甲",
}


def _selected_league_filter_keys(args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(key for key in LEAGUE_FILTER_ALIASES if getattr(args, key, False))


def _league_filter_label(keys: tuple[str, ...], league_kw: str) -> str:
    parts = [LEAGUE_FILTER_LABELS[key] for key in keys]
    if league_kw:
        parts.append(f"联赛名含 {league_kw!r}")
    return " / ".join(parts) if parts else "全部联赛"


def _league_matches_key(match_league: str, key: str) -> bool:
    text = match_league or ""
    lower = text.lower()
    return any(alias in text or alias in lower for alias in LEAGUE_FILTER_ALIASES[key])


def _add_league_filter_args(parser: argparse.ArgumentParser) -> None:
    suffix = "；可叠加多个联赛参数，按 OR 匹配；再与 --league-contains 叠加精筛"
    parser.add_argument(
        "-w",
        "--wc",
        "--world-cup",
        dest="world_cup",
        action="store_true",
        help="只输出世界杯场次（联赛名须含 世界杯 / 世界盃 / World Cup）" + suffix,
    )
    parser.add_argument("--epl", "--premier-league", dest="premier_league", action="store_true", help="只输出英超 / Premier League" + suffix)
    parser.add_argument("--la-liga", "--laliga", dest="la_liga", action="store_true", help="只输出西甲 / La Liga" + suffix)
    parser.add_argument("--bundesliga", dest="bundesliga", action="store_true", help="只输出德甲 / Bundesliga" + suffix)
    parser.add_argument(
        "--league-contains",
        type=str,
        default="",
        metavar="SUBSTR",
        help="仅保留联赛名（bfdata 第 2 列）包含该子串的场次",
    )


def cmd_sources(client: Titan007Client, _store: SnapshotStore, args: argparse.Namespace) -> int:
    ts = str(int(time.time() * 1000))
    print("Titan007 feed health (Referer required for livestatic):")
    try:
        t = _http_text(f"https://livestatic.titan007.com/vbsxml/time.txt?r=007{ts}", LIVE_REFERER, client.timeout, client.cookie)
        print(f"  time.txt: OK ({t.strip()[:40]}…)")
    except DataError as exc:
        print(f"  time.txt: FAIL {exc}")
    try:
        client.refresh_feeds()
        n = len(client.schedule_ids())
        print(f"  schedule index: {n} matches")
        print(f"  ch_goalbf3 keys: {len(client._goal_map or {})}")
        print(f"  sbOddsData keys: {len(client._sb_map or {})}")
    except DataError as exc:
        print(f"  feeds: FAIL {exc}")
    print("\nDocumented endpoints:")
    print(f"  schedule_source={client.schedule_source}")
    if client.okooo_bifa_enabled:
        nok = len(client._okooo_rows or [])
        err = client._okooo_load_error or ""
        nmap = len(getattr(client, "_okooo_titan_map", {}) or {})
        merr = getattr(client, "_okooo_map_load_error", None) or ""
        print(f"  okooo 必发盈亏: 已解析 {nok} 场" + (f"（拉取失败: {err}）" if err else ""))
        print(f"  okooo ID 映射: {nmap} 条" + (f"（加载失败: {merr}）" if merr else ""))
    else:
        print("  okooo 必发盈亏: 未启用（--okooo-bifa 或 TITAN007_OKOOO_BIFA=1）")
    print("  bf: https://bf.titan007.com/vbsxml/bfdata.js")
    print("  live (默认): https://livestatic.titan007.com/vbsxml/bfdata_ut.js (与 oldIndexall 页一致)")
    print("  jc: https://jc.titan007.com/xml/bf_jc.txt (竞彩开售子集)")
    print(f"  {CH_GOAL_BF3}")
    print(f"  {SB_ODDS_JS}")
    print("  https://1x2d.titan007.com/{schedule_id}.js")
    return 0


def cmd_predict(client: Titan007Client, store: SnapshotStore, args: argparse.Namespace) -> int:
    if not args.no_refresh:
        client.refresh_feeds()
    match = client.build_match(int(args.match_id))
    predictor = Predictor(client, store)
    result = predictor.analyze(match)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_analysis(result, verbose=args.verbose)
    return 0


def _upcoming_league_ok(
    match_league: str,
    league_kw: str,
    world_cup: bool,
    league_keys: tuple[str, ...] = (),
) -> bool:
    """League filter for ``upcoming``（快捷联赛按 OR，``--league-contains`` 为 AND）。"""
    ln = match_league or ""
    keys = league_keys or (("world_cup",) if world_cup else ())
    preset_ok = True
    if keys:
        preset_ok = any(_league_matches_key(ln, key) for key in keys)
    sub_ok = True
    if league_kw:
        sub_ok = league_kw in ln
    return preset_ok and sub_ok


def cmd_upcoming(client: Titan007Client, _store: SnapshotStore, args: argparse.Namespace) -> int:
    client.refresh_feeds()
    n = len(client.schedule_ids())
    if args.fetch_euro:
        print(f"已加载 {n} 场；将为每场请求 1x2d 欧赔（可能很慢）…", flush=True)
    elif client.schedule_source == "jc":
        print(
            f"已加载 {n} 场索引（赛程源: 竞彩 jc.titan007.com/xml/bf_jc.txt，"
            "仅含竞彩开售场次，与 bf 全量表不同）",
            flush=True,
        )
    elif client.schedule_source == "live":
        print(f"已加载 {n} 场索引（赛程源: livestatic bfdata_ut.js，与 oldIndexall 页同源）", flush=True)
    else:
        print(f"已加载 {n} 场索引（upcoming 默认跳过逐场欧赔以节省时间）", flush=True)
    if getattr(args, "with_okooo", False) and client.okooo_bifa_enabled:
        print(f"澳客必发列表已解析 {len(client._okooo_rows or [])} 场（用于 ScheduleID ↔ okooo_id 列）。", flush=True)
    now = datetime.now(timezone.utc)
    hours = float(args.hours)
    limit = now + timedelta(hours=hours)
    upper = datetime(2100, 1, 1, tzinfo=timezone.utc) if args.all_future else limit
    league_kw = (args.league_contains or "").strip()
    world_cup = bool(getattr(args, "world_cup", False))
    league_keys = _selected_league_filter_keys(args)
    want_league_filter = bool(league_keys) or bool(league_kw)
    rows: list[tuple[datetime, int, str, str, str]] = []
    fetch_euro = args.fetch_euro
    next_future: datetime | None = None
    next_future_kw: datetime | None = None
    latest_in_index: datetime | None = None
    for sid in client.schedule_ids():
        m = client.build_match(sid, fetch_1x2=fetch_euro)
        if latest_in_index is None or m.match_time > latest_in_index:
            latest_in_index = m.match_time
        if m.match_time > now and (next_future is None or m.match_time < next_future):
            next_future = m.match_time
        if want_league_filter and m.match_time > now and _upcoming_league_ok(m.league_name or "", league_kw, world_cup, league_keys):
            if next_future_kw is None or m.match_time < next_future_kw:
                next_future_kw = m.match_time
        if m.match_time <= now or m.match_time > upper:
            continue
        if not _upcoming_league_ok(m.league_name or "", league_kw, world_cup, league_keys):
            continue
        rows.append((m.match_time, sid, m.home, m.away, m.league_name))
    rows.sort()
    show_ok = getattr(args, "with_okooo", False)
    if show_ok:
        print(
            "开赛时间(UTC)\tScheduleID\tokooo_id\tmatch_source\t对阵\t联赛",
            flush=True,
        )
        print("# match_source: id_map | heuristic | - ；ScheduleID 即 predict --match-id", flush=True)
    else:
        print(
            "开赛时间(UTC)          ScheduleID   对阵   # 球探逗号时间为北京时间；"
            "月份为 JS 的 0–11（5=六月），已换算为 UTC；第二列即 predict --match-id",
            flush=True,
        )
    if args.all_future:
        print(
            f"筛选: 开球晚于当前时间的所有索引场次（--all-future，不按 --hours 截断上界），"
            f"最多输出 --limit={args.limit}",
            flush=True,
        )
    else:
        print(f"筛选窗口: {now.isoformat()} ～ {limit.isoformat()}（--hours={hours}）", flush=True)
    if want_league_filter:
        print(f"联赛筛选: {_league_filter_label(league_keys, league_kw)}", flush=True)
    if not rows:
        print("（窗口内没有比赛。）", flush=True)
        if want_league_filter and next_future_kw is not None:
            hint = _league_filter_label(league_keys, league_kw)
            print(
                f"提示: {hint} 的场次里，最近一场未来开球为 {next_future_kw.isoformat()} (UTC)；"
                "若与窗口不符可加 ``--all-future`` 或加大 ``--hours``。",
                flush=True,
            )
        elif next_future is not None:
            print(
                f"提示: 索引中下一场开球不早于 {next_future.isoformat()} (UTC)；"
                f"可把 --hours 加大，例如: python3 titan007_ah_cli.py upcoming --hours 168",
                flush=True,
            )
        elif latest_in_index is not None and latest_in_index <= now:
            print(
                f"提示: 当前赛程索引内最晚一场开赛为 {latest_in_index.isoformat()} (UTC)，"
                "均不晚于当前时间；bfdata 多为近期/已赛滚动列表，未必含远期赛程，"
                "单纯加大 --hours 不会出现未来场次。可稍后再抓索引或对照 bf.titan007 页面。",
                flush=True,
            )
        else:
            print(
                "提示: 索引中未找到晚于当前时间的场次；bfdata 可能仅为当日已赛/远期赛程，可稍后再试或检查数据源。",
                flush=True,
            )
        return 0
    for mt, sid, h, a, lg in rows[: int(args.limit)]:
        if show_ok:
            oid, src = "-", "-"
            if client.okooo_bifa_enabled and client._okooo_rows:
                res = resolve_okooo_bifa_match(
                    sid,
                    h,
                    a,
                    mt,
                    client._okooo_rows,
                    schedule_tz=_bf_match_timezone(),
                    titan_to_okooo=client._okooo_titan_map or {},
                )
                if res:
                    oid, src = str(res[0].okooo_id), res[2]
                elif (client._okooo_titan_map or {}).get(sid):
                    oid, src = "(miss)", "id_map_miss"
            print(f"{mt.isoformat()}\t{sid}\t{oid}\t{src}\t{h} vs {a}\t{lg}")
        else:
            extra = f"  [{lg}]" if want_league_filter else ""
            print(f"{mt.isoformat()}  {sid:>8}  {h} vs {a}{extra}")
    return 0


def cmd_watch(client: Titan007Client, store: SnapshotStore, args: argparse.Namespace) -> int:
    poll = float(os.environ.get("TITAN007_POLL_SEC", str(args.interval)))
    ids = [int(x) for x in args.match_ids.split(",") if x.strip()]
    if not ids:
        raise SystemExit("watch requires --match-ids id1,id2,...")
    print(f"watch poll={poll}s snapshot_dir={store.root} ids={ids}", flush=True)
    while True:
        try:
            client.refresh_feeds()
        except DataError as exc:
            print(f"refresh error: {exc}", file=sys.stderr, flush=True)
            time.sleep(min(poll, 30.0))
            continue
        predictor = Predictor(client, store)
        for mid in ids:
            try:
                match = client.build_match(mid)
                result = predictor.analyze(match)
                store.save(result)
                if args.verbose:
                    print(f"{datetime.now(timezone.utc).isoformat()} saved {mid} score={result.score:.4f}")
            except DataError as exc:
                print(f"match {mid}: {exc}", file=sys.stderr)
        time.sleep(poll)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Titan007 数据源版亚盘助手：复用 worldcup_ah_cli.Predictor。",
    )
    p.add_argument(
        "--schedule-source",
        choices=["bf", "live", "jc"],
        default=None,
        metavar="SRC",
        help="赛程索引: bf=bf.titan007 bfdata.js; live=oldIndexall 同款 bfdata_ut.js（默认）; jc=竞彩 bf_jc.txt。"
        "未传本参数时读环境变量 TITAN007_SCHEDULE_SOURCE，再未设置则为 live。",
    )
    og = p.add_mutually_exclusive_group()
    og.add_argument(
        "--okooo-bifa",
        dest="okooo_bifa",
        action="store_const",
        const=True,
        default=None,
        help="refresh 时抓取澳客「必发盈亏」HTML 并合并 Bf*（队名+开赛时间匹配，无官方 ID 映射）。",
    )
    og.add_argument(
        "--no-okooo-bifa",
        dest="okooo_bifa",
        action="store_const",
        const=False,
        default=None,
        help="显式关闭澳客合并（覆盖 TITAN007_OKOOO_BIFA）。",
    )
    p.add_argument("--env-file", type=str, default=None, help="加载 .env（默认仓库根 .env）")
    p.add_argument("--no-dotenv", action="store_true", help="不加载 .env")
    p.add_argument("--timeout", type=float, default=20.0, help="HTTP 超时秒")
    p.add_argument(
        "--snapshot-dir",
        type=str,
        default=None,
        help="快照目录（默认 .titan007_snapshots 或环境变量 TITAN007_SNAPSHOT_DIR）",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("sources", help="探测各 feed 是否可用")
    s.set_defaults(func=cmd_sources)

    u = sub.add_parser(
        "upcoming",
        help="浏览未来赛程，列出多场及各自的 ScheduleID（不是按单场 ID 查询）",
        description=(
            "从赛程索引筛未来比赛。默认赛程源为 live（与 oldIndexall 同源的 bfdata_ut）；"
            "可用全局 --schedule-source 改为 bf 或 jc。"
            "筛世界杯可加 ``--world-cup``、``-w`` 或 ``--wc``；筛英超/西甲/德甲可加 "
            "``--epl``、``--la-liga``、``--bundesliga``，多个快捷联赛参数按 OR 匹配。"
            "需要 **澳客比赛 ID** 时加 ``--with-okooo``（会多一次 betfa 请求，并在每行输出 ``okooo_id`` / ``match_source``）。"
            "若索引里全是已赛场次，则列不出未来球；跨度大时用 ``--all-future --limit 5``。"
            "每行第二列是 ScheduleID，与 predict --match-id 相同。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    u.add_argument("--hours", type=float, default=48.0)
    u.add_argument("--limit", type=int, default=80)
    u.add_argument(
        "--all-future",
        action="store_true",
        help="不按 --hours 截断上界，列出索引中所有「开球晚于当前时间」的场次（再受 --limit 限制）；"
        "适合世界杯跨度长、不想把 --hours 拉到几千的情况。",
    )
    _add_league_filter_args(u)
    u.add_argument(
        "--fetch-euro",
        action="store_true",
        help="为每场请求 1x2d 欧赔（默认关闭；开启后场次多时极慢）",
    )
    u.add_argument(
        "--with-okooo",
        action="store_true",
        help="refresh 时拉澳客必发盈亏列表，并在每行输出 okooo_id 与 match_source（id_map/heuristic）；等同全局加 --okooo-bifa",
    )
    u.set_defaults(func=cmd_upcoming)

    pr = sub.add_parser("predict", help="对单场 ScheduleID 做预测")
    pr.add_argument("--match-id", type=int, required=True, help="球探 ScheduleID（bfdata 第 0 列）")
    pr.add_argument("--no-refresh", action="store_true", help="使用内存缓存（默认先 refresh_feeds）")
    pr.add_argument("--verbose", action="store_true")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_predict)

    w = sub.add_parser("watch", help="轮询预测并写入 JSONL 快照（供合成必发走势）")
    w.add_argument("--match-ids", type=str, required=True, help="逗号分隔 ScheduleID")
    w.add_argument("--interval", type=float, default=120.0, help="轮询秒数（可用 TITAN007_POLL_SEC 覆盖）")
    w.add_argument("--verbose", action="store_true")
    w.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.no_dotenv:
        load_dotenv_file(Path(args.env_file) if args.env_file else default_env_file_path())
    store = SnapshotStore(_snapshot_dir(args.snapshot_dir))
    okooo = args.okooo_bifa
    if args.command == "upcoming" and getattr(args, "with_okooo", False):
        okooo = True
    client = Titan007Client(
        timeout=args.timeout,
        cookie=_cookie_from_env(),
        snapshot_store=store,
        schedule_source=args.schedule_source,
        okooo_bifa=okooo,
    )
    func = getattr(args, "func", None)
    if func is None:
        raise SystemExit("internal: subcommand not bound")
    if func is cmd_watch:
        return func(client, store, args)
    if func is cmd_predict:
        return func(client, store, args)
    return func(client, store, args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
