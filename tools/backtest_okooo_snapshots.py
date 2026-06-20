#!/usr/bin/env python3
"""Read `.okooo_snapshots/*.jsonl` last snapshot per event and compare 推荐 to 1X0 比分下的亚盘结算。

用法:
  python okooo_ah_cli.py validate-snapshots [--snapshot-dir .okooo_snapshots]
  python tools/backtest_okooo_snapshots.py
  python tools/backtest_okooo_snapshots.py --scores-json path/to.json
  python tools/backtest_okooo_snapshots.py --list-all
  python tools/backtest_okooo_snapshots.py --replay

`--scores-json` 格式: { "1315857": [2, 0], "1316320": [1, 0] }  （主队进球, 客队进球）
不传则读取 ``tools/okooo_validate_scores.json``（与 ``okooo_ah_cli.py validate-snapshots`` 默认一致）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from okooo_ah_cli import load_okooo_validate_scores, run_validate_snapshots_from_dir  # noqa: E402
from worldcup_ah_cli import line_value  # noqa: E402


def load_scores(path: Path | None) -> dict[int, tuple[int, int]]:
    return load_okooo_validate_scores(path)


def margin_for_upper(h: int, a: int, line: float, upper: str, home: str, away: str) -> float:
    if upper == home:
        return float(h - a) + line
    if upper == away:
        return float(a - h) - line
    raise ValueError(f"upper_team {upper!r} not in {home!r} / {away!r}")


def recommendation_outcome(
    rec: str, upper: str, lower: str, home: str, away: str, line_txt: str, hg: int, ag: int
) -> tuple[str, float]:
    """返回 (hit|miss|push|na, margin_for_upper)。"""
    if rec == "观望":
        return "na", 0.0
    line = line_value(line_txt)
    m = margin_for_upper(hg, ag, line, upper, home, away)
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


def last_snapshot(path: Path) -> dict | None:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def run_replay_predict(snap_dir: Path, scores: dict[int, tuple[int, int]]) -> int:
    """委托 ``okooo_ah_cli.run_validate_snapshots_from_dir``（与 ``validate-snapshots`` 子命令一致）。"""
    return run_validate_snapshots_from_dir(snap_dir, scores)


def main() -> int:
    ap = argparse.ArgumentParser(description="澳客快照最后一条 vs 已知比分（亚盘结算）")
    ap.add_argument(
        "--snapshot-dir",
        type=Path,
        default=ROOT / ".okooo_snapshots",
        help="快照目录，默认仓库根 .okooo_snapshots",
    )
    ap.add_argument("--scores-json", type=Path, default=None, help="event_id -> [主队进球, 客队进球]")
    ap.add_argument(
        "--list-all",
        action="store_true",
        help="列出目录内所有 jsonl 的最后推荐；若该 event 在比分表中有比分则计算 hit/miss",
    )
    ap.add_argument(
        "--replay",
        action="store_true",
        help="用快照 jsonl 基础数据 + 当前 Predictor 重放（非磁盘上旧的 result 字段）",
    )
    args = ap.parse_args()

    try:
        scores = load_scores(args.scores_json)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.replay:
        if not args.snapshot_dir.is_dir():
            print(f"error: not a directory: {args.snapshot_dir}", file=sys.stderr)
            return 1
        return run_replay_predict(args.snapshot_dir, scores)
    snap_dir: Path = args.snapshot_dir
    if not snap_dir.is_dir():
        print(f"error: not a directory: {snap_dir}", file=sys.stderr)
        return 1

    hit = miss = push = na_dir = 0
    printed_any = False

    print("event_id\tmatch\tline\trec\tmodel\tscore\tout\tmargin\tfetched_at")
    for path in sorted(snap_dir.glob("*.jsonl")):
        try:
            eid = int(path.stem)
        except ValueError:
            continue
        rec_data = last_snapshot(path)
        if not rec_data or "result" not in rec_data:
            continue
        r = rec_data["result"]
        m = rec_data.get("match") or {}
        home = m.get("home", "")
        away = m.get("away", "")
        line = m.get("asian_line", "0")
        rec = r.get("recommendation", "")
        model = r.get("model_recommendation", "")
        upper = r.get("upper_team", "")
        lower = r.get("lower_team", "")
        fetched = str(rec_data.get("fetched_at", ""))[:19]

        if not args.list_all and eid not in scores:
            continue

        sc = ""
        out = ""
        margin_s = ""
        if eid in scores:
            hg, ag = scores[eid]
            sc = f"{hg}-{ag}"
            out, margin = recommendation_outcome(rec, upper, lower, home, away, line, hg, ag)
            margin_s = f"{margin:+.3f}"
            if not args.list_all:
                if out == "hit":
                    hit += 1
                elif out == "miss":
                    miss += 1
                elif out == "push":
                    push += 1
                else:
                    na_dir += 1
        elif args.list_all:
            out = "-"
            margin_s = "-"

        printed_any = True
        print(f"{eid}\t{home} vs {away}\t{line}\t{rec}\t{model}\t{sc}\t{out}\t{margin_s}\t{fetched}")

    if not printed_any:
        print("（无匹配 jsonl；检查 --snapshot-dir）")
        return 0

    if not args.list_all:
        denom = hit + miss + push
        if denom:
            print(
                f"\n有方向推荐 {denom} 场: 命中 {hit} 未中 {miss} 走水 {push} ；"
                f"命中率 {hit / denom:.1%}（观望未计入分母）"
            )
        if na_dir:
            print(f"观望场次（在比分表内）: {na_dir}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
