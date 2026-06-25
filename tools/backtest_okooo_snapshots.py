#!/usr/bin/env python3
"""Read `.okooo_snapshots/*.jsonl` last snapshot per event and compare 推荐 to 1X0 比分下的亚盘结算。

用法:
  python okooo_ah_cli.py validate-snapshots [--snapshot-dir .okooo_snapshots]
  python tools/backtest_okooo_snapshots.py
  python tools/backtest_okooo_snapshots.py --scores-json path/to.json
  python tools/backtest_okooo_snapshots.py --list-all
  python tools/backtest_okooo_snapshots.py --replay [--allow-miss]

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

from asian_handicap_validation import settle_asian_handicap  # noqa: E402
from okooo_ah_cli import (  # noqa: E402
    DEFAULT_OKOOO_MODEL_FREEZE_PATH,
    load_model_freeze,
    load_okooo_validate_scores,
    run_validate_snapshots_from_dir,
)
from worldcup_ah_cli import line_value  # noqa: E402


def load_scores(path: Path | None) -> dict[int, tuple[int, int]]:
    return load_okooo_validate_scores(path)


def recommendation_outcome(
    rec: str, upper: str, lower: str, home: str, away: str, line_txt: str, hg: int, ag: int
) -> tuple[str, float]:
    """返回精确亚盘结算档位和上盘原始 margin。"""
    settlement = settle_asian_handicap(rec, upper, home, away, line_value(line_txt), hg, ag)
    return settlement.outcome, settlement.margin


def last_snapshot(path: Path) -> dict | None:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


def run_replay_predict(
    snap_dir: Path,
    scores: dict[int, tuple[int, int]],
    *,
    allow_miss: bool,
    walk_forward: bool,
    freeze_manifest: Path,
) -> int:
    """委托 ``okooo_ah_cli.run_validate_snapshots_from_dir``（与 ``validate-snapshots`` 子命令一致）。"""
    freeze = load_model_freeze(freeze_manifest) if walk_forward else None
    return run_validate_snapshots_from_dir(
        snap_dir,
        scores,
        fail_on_miss=not allow_miss,
        mode="walk-forward" if walk_forward else "replay",
        freeze=freeze,
    )


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
    ap.add_argument(
        "--allow-miss",
        action="store_true",
        help="与 okooo_ah_cli validate-snapshots --allow-miss 一致：存在未中方向时仍退出 0",
    )
    ap.add_argument("--walk-forward", action="store_true", help="使用快照当时保存的冻结模型预测")
    ap.add_argument("--freeze-manifest", type=Path, default=DEFAULT_OKOOO_MODEL_FREEZE_PATH)
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
        return run_replay_predict(
            args.snapshot_dir,
            scores,
            allow_miss=args.allow_miss,
            walk_forward=args.walk_forward,
            freeze_manifest=args.freeze_manifest,
        )
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
                if out in ("full_win", "half_win"):
                    hit += 1
                elif out in ("full_loss", "half_loss"):
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
