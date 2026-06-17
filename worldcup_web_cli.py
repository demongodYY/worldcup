#!/usr/bin/env python3
"""World Cup web-capture CLI: run the same Predictor as worldcup_ah_cli.py on JSON
fetched from the browser (same-origin /spdexapi/... with session cookies).

When app.spdex.com redirects or returns HTML for urllib, collecting raw JSON from
DevTools Network or from an in-page fetch() in Cursor Simple Browser avoids the
broken curl redirect + Cookie header issue.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from worldcup_ah_cli import (
    AnalysisResult,
    DataError,
    EuroTrendPoint,
    HandicapRow,
    Match,
    Predictor,
    PriceVolumePoint,
    SnapshotStore,
    default_env_file_path,
    load_dotenv_file,
    parse_datetime,
    parse_euro_trend,
    parse_handicap,
    parse_match,
    parse_price_volume,
    print_analysis,
    require_list_payload,
)


def match_from_stored(d: dict[str, Any]) -> Match:
    """Rebuild Match from match_to_dict() / snapshot JSONL `match` object."""
    raw = d.get("raw")
    if not isinstance(raw, dict):
        raise DataError("bundle `match` must include dict `raw` (use snapshot match_to_dict shape)")
    mt = d["match_time"]
    if isinstance(mt, str):
        mt = parse_datetime(mt)
    return Match(
        event_id=int(d["event_id"]),
        match_time=mt,
        home=str(d.get("home", "")),
        away=str(d.get("away", "")),
        league_id=d.get("league_id"),
        league_name=str(d.get("league_name", "")),
        asian_line=str(d.get("asian_line", "0")),
        is_stop_update=bool(d.get("is_stop_update", False)),
        raw=raw,
    )


def _coerce_list_payload(payload: Any, label: str) -> list[Any]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if not payload:
            return []
        return list(require_list_payload(payload, label))
    raise DataError(f"{label}: expected list or dict, got {type(payload).__name__}")


class CapturedJsonClient:
    """Feeds Predictor from browser-captured API payloads (raw JSON bodies)."""

    def __init__(self, bundle: dict[str, Any]) -> None:
        self._bundle = bundle

    def handicap_list(self, _event_id: int, _asian_line: str) -> list[HandicapRow]:
        raw = self._bundle.get("handicap_list") or self._bundle.get("odds_view_list")
        rows = _coerce_list_payload(raw, "handicap list")
        out: list[HandicapRow] = []
        for item in rows:
            if isinstance(item, dict):
                out.append(parse_handicap(item))
        return out

    def price_volume(self, _event_id: int, selection: str) -> list[PriceVolumePoint]:
        pv = self._bundle.get("price_volume")
        if isinstance(pv, dict):
            block = pv.get(selection.lower()) or pv.get(selection)
        else:
            block = self._bundle.get(f"price_volume_{selection.lower()}")
        rows = _coerce_list_payload(block, "price/volumn")
        out: list[PriceVolumePoint] = []
        for item in rows:
            if isinstance(item, dict):
                out.append(parse_price_volume(item))
        return out

    def euro_trend(self, _event_id: int) -> list[EuroTrendPoint]:
        rows = _coerce_list_payload(self._bundle.get("euro_trend"), "euro trend")
        out: list[EuroTrendPoint] = []
        for item in rows:
            if isinstance(item, dict):
                out.append(parse_euro_trend(item))
        return out


def load_bundle(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DataError("bundle root must be a JSON object")
    return data


def match_from_bundle(data: dict[str, Any]) -> Match:
    if "match" in data and isinstance(data["match"], dict):
        m = data["match"]
        if "raw" in m and "event_id" in m:
            return match_from_stored(m)
    md = data.get("match_detail")
    if md is None:
        raise DataError("bundle needs `match` (snapshot shape) or `match_detail` (API JSON)")
    if isinstance(md, list):
        if not md:
            raise DataError("match_detail list is empty")
        return parse_match(md[0])
    if isinstance(md, dict):
        return parse_match(md)
    raise DataError("match_detail must be object or non-empty list")


def load_bundle_match(path: Path) -> Match:
    return match_from_bundle(load_bundle(path))


def last_jsonl_record(path: Path) -> dict[str, Any]:
    lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if not lines:
        raise DataError(f"empty jsonl: {path}")
    rec = json.loads(lines[-1])
    if not isinstance(rec, dict):
        raise DataError("jsonl line must be an object")
    return rec


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="使用浏览器采集的 SPDEX JSON 跑与 worldcup_ah_cli 相同的 Predictor。",
    )
    p.add_argument(
        "--env-file",
        type=str,
        default=None,
        help="加载 .env（默认与 worldcup_ah_cli 相同目录）；仅用于将来扩展，当前预测不发起网络请求",
    )
    p.add_argument("--no-dotenv", action="store_true", help="不加载 .env")
    p.add_argument(
        "--snapshot-dir",
        default=".spdex_snapshots",
        help="与 worldcup_ah_cli snapshot/trend 使用同一目录，便于快照信号",
    )
    sub = p.add_subparsers(dest="command", required=True)

    pred = sub.add_parser("predict", help="从 bundle JSON 预测单场")
    pred.add_argument("--bundle", type=str, required=True, help="bundle.json 路径")
    pred.add_argument("--verbose", action="store_true")
    pred.add_argument("--json", action="store_true")

    snap = sub.add_parser(
        "predict-from-jsonl",
        help="用 jsonl 最后一行的 match + 可选 bundle 文件补 handicap/成交/欧赔",
    )
    snap.add_argument("--jsonl", type=str, required=True, help=".spdex_snapshots/<id>.jsonl")
    snap.add_argument("--bundle", type=str, default=None, help="若缺省则 handicap/成交/欧赔为空列表")
    snap.add_argument("--verbose", action="store_true")
    snap.add_argument("--json", action="store_true")

    sub.add_parser("print-capture-js", help="打印 scripts/spdex_capture_bundle.js 的路径与用法")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.no_dotenv:
        env_path = Path(args.env_file) if args.env_file else default_env_file_path()
        load_dotenv_file(env_path)

    if args.command == "print-capture-js":
        root = Path(__file__).resolve().parent
        js = root / "scripts" / "spdex_capture_bundle.js"
        ex = root / "fixtures" / "web_bundle.example.json"
        print("在已登录 SPdex 的页面（与接口同源）打开 DevTools Console，粘贴并运行：")
        print(js.resolve())
        print("\n将下载的 JSON 存盘后：")
        print(f"  python3 {Path(__file__).name} predict --bundle {ex.name}")
        print(f"示例字段说明：{ex.resolve()}")
        return 0

    store = SnapshotStore(args.snapshot_dir)

    if args.command == "predict":
        bundle = load_bundle(Path(args.bundle))
        match = match_from_bundle(bundle)
        client = CapturedJsonClient(bundle)
        predictor = Predictor(client, store)
        result = predictor.analyze(match)
        return _emit(result, args.verbose, args.json)

    if args.command == "predict-from-jsonl":
        rec = last_jsonl_record(Path(args.jsonl))
        m = rec.get("match")
        if not isinstance(m, dict):
            raise SystemExit("jsonl record missing dict `match`")
        match = match_from_stored(m)
        if args.bundle:
            bundle = load_bundle(Path(args.bundle))
        else:
            bundle = {
                "handicap_list": [],
                "price_volume": {"home": [], "away": []},
                "euro_trend": [],
            }
        client = CapturedJsonClient(bundle)
        predictor = Predictor(client, store)
        result = predictor.analyze(match)
        return _emit(result, args.verbose, args.json)

    raise SystemExit(f"unknown command: {args.command}")


def _emit(result: AnalysisResult, verbose: bool, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print_analysis(result, verbose=verbose)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DataError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
