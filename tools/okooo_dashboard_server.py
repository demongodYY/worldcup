#!/usr/bin/env python3
"""Local dashboard for Okooo prediction snapshots and validation replay."""

from __future__ import annotations

import argparse
import json
import sys
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from okooo_ah_cli import (  # noqa: E402
    DEFAULT_OKOOO_VALIDATE_SCORES_PATH,
    OKOOO_DEFAULT_ISSUE,
    OkoooClient,
    ValidateReplaySnapshotStore,
    attach_snapshot_replay_fields,
    cookie_from_env,
    load_okooo_validate_scores,
    snapshot_dir as resolve_okooo_snapshot_dir,
)
from worldcup_ah_cli import (  # noqa: E402
    AnalysisResult,
    DataError,
    Match,
    OkoooSnapshotReplayClient,
    Predictor,
    SnapshotStore,
    line_value,
    load_dotenv_file,
    match_from_dict,
    score_snapshot_signal_history,
    score_strength_label,
    snapshot_metrics,
    snapshot_trend_summary,
)

DEFAULT_HTML_PATH = ROOT / "tools" / "okooo_dashboard.html"
MAX_PENDING_PREDICTIONS = 16
PENDING_PREDICTIONS: dict[str, AnalysisResult] = {}


@dataclass(frozen=True)
class DashboardConfig:
    snapshot_root: Path
    scores_json: Path | None
    html_path: Path
    issue: str
    timeout: float
    cookie_file: str | None
    trade_trend: bool
    detail_max_pages: int


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return sorted(records, key=lambda item: str(item.get("fetched_at", "")))


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def timestamp_sort_value(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return float("-inf")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


def snapshot_event_sort_key(event: dict[str, Any]) -> tuple[float, int]:
    return timestamp_sort_value(event.get("match_time")), as_int(event.get("event_id"))


def infer_strength(score: Any, explicit: Any = None) -> str:
    if explicit:
        return str(explicit)
    return score_strength_label(as_float(score))


def normalize_result_dict(result: dict[str, Any]) -> dict[str, Any]:
    score = as_float(result.get("score"))
    recommendation = str(result.get("recommendation") or result.get("purchase_side") or "观望")
    purchase_side = str(result.get("purchase_side") or (recommendation if recommendation in ("上盘", "下盘") else "观望"))
    purchase_team = str(result.get("purchase_team") or "")
    if not purchase_team:
        if purchase_side == "上盘":
            purchase_team = str(result.get("upper_team") or "")
        elif purchase_side == "下盘":
            purchase_team = str(result.get("lower_team") or "")
    return {
        "event_id": result.get("event_id"),
        "match": result.get("match"),
        "recommendation": recommendation,
        "purchase_side": purchase_side,
        "purchase_team": purchase_team,
        "model_recommendation": result.get("model_recommendation") or recommendation,
        "score": round(score, 4),
        "purchase_score": round(as_float(result.get("purchase_score"), score), 4),
        "strength": infer_strength(score, result.get("strength")),
        "confidence": as_int(result.get("confidence")),
        "completeness": as_int(result.get("completeness")),
        "upper_team": result.get("upper_team") or "",
        "lower_team": result.get("lower_team") or "",
        "decision_reason": result.get("decision_reason") or "",
        "warnings": result.get("warnings") if isinstance(result.get("warnings"), list) else [],
        "signals": result.get("signals") if isinstance(result.get("signals"), list) else [],
    }


def record_point(record: dict[str, Any]) -> dict[str, Any]:
    metrics = snapshot_metrics(record)
    match = record.get("match") if isinstance(record.get("match"), dict) else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    normalized = normalize_result_dict(result)
    return {
        "fetched_at": record.get("fetched_at") or "",
        "match_time": match.get("match_time") or result.get("match_time") or "",
        "asian_line": match.get("asian_line") or result.get("asian_line") or "",
        "recommendation": normalized["recommendation"],
        "purchase_side": normalized["purchase_side"],
        "purchase_team": normalized["purchase_team"],
        "model_recommendation": normalized["model_recommendation"],
        "strength": normalized["strength"],
        "score": round(metrics["score"], 4),
        "confidence": normalized["confidence"],
        "completeness": normalized["completeness"],
        "heat_edge": round(metrics["heat_edge"], 4),
        "amount_edge": round(metrics["amount_edge"], 4),
        "payout_edge": round(metrics["payout_edge"], 4),
        "upper_water": round(metrics["upper_water"], 4),
        "lower_water": round(metrics["lower_water"], 4),
        "line_depth": round(metrics["line_depth"], 4),
        "euro_edge": round(metrics["euro_edge"], 4),
    }


def important_signals(result: dict[str, Any], limit: int = 7) -> list[dict[str, Any]]:
    signals = result.get("signals")
    if not isinstance(signals, list):
        return []
    usable: list[dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict) or not signal.get("available", False):
            continue
        usable.append(
            {
                "name": signal.get("name") or "",
                "score": round(as_float(signal.get("score")), 4),
                "weight": as_float(signal.get("weight")),
                "summary": signal.get("summary") or signal.get("reason") or "",
            }
        )
    usable.sort(key=lambda item: abs(item["score"]) * max(item["weight"], 0.01), reverse=True)
    return usable[:limit]


def replay_snapshot_result(snapshot_root: Path, event_id: int, records: list[dict[str, Any]], index: int) -> AnalysisResult:
    """Replay one snapshot point with the current Predictor and only prior history.

    ``records[index]`` is the current point. The replay client sees records up to
    that point, while SnapshotStore history excludes the current point so trend
    signals never look ahead.
    """
    current_records = records[: index + 1]
    match = match_from_dict(current_records[-1]["match"])
    client = OkoooSnapshotReplayClient(current_records)
    store = ValidateReplaySnapshotStore(snapshot_root, event_id, current_records[:-1])
    return Predictor(client, store).analyze(match)


def replayed_snapshot_records(snapshot_root: Path, event_id: int, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, record in enumerate(records):
        try:
            result = replay_snapshot_result(snapshot_root, event_id, records, idx)
            replayed = dict(record)
            replayed["result"] = result.to_dict()
            out.append(replayed)
        except Exception:
            # Keep the row visible if one old snapshot cannot be replayed.
            out.append(record)
    return out


def build_snapshot_events(snapshot_root: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not snapshot_root.is_dir():
        return events
    for path in sorted(snapshot_root.glob("*.jsonl")):
        try:
            event_id = int(path.stem)
        except ValueError:
            continue
        records = [record for record in read_jsonl(path) if isinstance(record.get("match"), dict)]
        if not records:
            continue
        replayed_records = replayed_snapshot_records(snapshot_root, event_id, records)
        first = replayed_records[0]
        last = replayed_records[-1]
        first_metrics = snapshot_metrics(first)
        last_metrics = snapshot_metrics(last)
        match = last.get("match") if isinstance(last.get("match"), dict) else {}
        result = last.get("result") if isinstance(last.get("result"), dict) else {}
        normalized = normalize_result_dict(result)
        score_delta = last_metrics["score"] - first_metrics["score"]
        heat_delta = last_metrics["heat_edge"] - first_metrics["heat_edge"]
        amount_delta = last_metrics["amount_edge"] - first_metrics["amount_edge"]
        payout_delta = last_metrics["payout_edge"] - first_metrics["payout_edge"]
        upper_water_delta = last_metrics["upper_water"] - first_metrics["upper_water"]
        line_depth_delta = last_metrics["line_depth"] - first_metrics["line_depth"]
        if len(records) >= 2:
            trend_note = snapshot_trend_summary(score_delta, heat_delta, upper_water_delta, line_depth_delta)
            signal_history_score, signal_history_reason = score_snapshot_signal_history(records)
        else:
            trend_note = "本地只有 1 条快照，趋势等待下一次采样"
            signal_history_score, signal_history_reason = 0.0, "本地快照不足 2 条"
        event = {
            "event_id": event_id,
            "home": match.get("home") or "",
            "away": match.get("away") or "",
            "match": f"{match.get('home') or ''} vs {match.get('away') or ''}",
            "league_name": match.get("league_name") or "",
            "match_time": match.get("match_time") or result.get("match_time") or "",
            "asian_line": match.get("asian_line") or result.get("asian_line") or "",
            "snapshot_count": len(records),
            "first_fetched_at": first.get("fetched_at") or "",
            "last_fetched_at": last.get("fetched_at") or "",
            "last_result": normalized,
            "score_delta": round(score_delta, 4),
            "heat_delta": round(heat_delta, 4),
            "amount_delta": round(amount_delta, 4),
            "payout_delta": round(payout_delta, 4),
            "upper_water_delta": round(upper_water_delta, 4),
            "line_depth_delta": round(line_depth_delta, 4),
            "trend_note": trend_note,
            "signal_history_score": round(signal_history_score, 4),
            "signal_history_reason": signal_history_reason,
            "important_signals": important_signals(result),
            "series": [record_point(record) for record in replayed_records],
        }
        events.append(event)
    return sorted(events, key=snapshot_event_sort_key, reverse=True)


def margin_for_upper(home_goals: int, away_goals: int, line: float, upper: str, home: str, away: str) -> float:
    if upper == home:
        return float(home_goals - away_goals) + line
    if upper == away:
        return float(away_goals - home_goals) - line
    raise ValueError(f"upper_team {upper!r} not in {home!r} / {away!r}")


def recommendation_outcome(
    recommendation: str,
    upper: str,
    lower: str,
    home: str,
    away: str,
    line_text: str,
    home_goals: int,
    away_goals: int,
) -> tuple[str, float]:
    if recommendation == "观望":
        return "na", 0.0
    margin = margin_for_upper(home_goals, away_goals, line_value(line_text), upper, home, away)
    eps = 1e-9
    if recommendation == "上盘":
        if margin > eps:
            return "hit", margin
        if margin < -eps:
            return "miss", margin
        return "push", margin
    if recommendation == "下盘":
        if margin < -eps:
            return "hit", margin
        if margin > eps:
            return "miss", margin
        return "push", margin
    return "na", margin


def replay_validate_result(snapshot_root: Path, event_id: int, home_goals: int, away_goals: int) -> dict[str, Any]:
    path = snapshot_root / f"{event_id}.jsonl"
    if not path.is_file():
        return {
            "event_id": event_id,
            "status": "missing",
            "outcome": "missing",
            "scoreline": f"{home_goals}-{away_goals}",
            "match": f"(missing {path.name})",
        }
    records = read_jsonl(path)
    if not records:
        return {
            "event_id": event_id,
            "status": "empty",
            "outcome": "missing",
            "scoreline": f"{home_goals}-{away_goals}",
            "match": f"(empty {path.name})",
        }
    try:
        match = match_from_dict(records[-1]["match"])
        result = replay_snapshot_result(snapshot_root, event_id, records, len(records) - 1)
        outcome, margin = recommendation_outcome(
            result.recommendation,
            result.upper_team,
            result.lower_team,
            match.home,
            match.away,
            match.asian_line,
            home_goals,
            away_goals,
        )
        return {
            "event_id": event_id,
            "status": "ok",
            "outcome": outcome,
            "margin": round(margin, 4),
            "scoreline": f"{home_goals}-{away_goals}",
            "home_goals": home_goals,
            "away_goals": away_goals,
            "match": f"{match.home} vs {match.away}",
            "home": match.home,
            "away": match.away,
            "match_time": match.match_time.isoformat(),
            "asian_line": match.asian_line,
            "recommendation": result.recommendation,
            "purchase_side": result.purchase_side,
            "purchase_team": result.purchase_team,
            "model_recommendation": result.model_recommendation,
            "strength": result.strength,
            "score": round(result.score, 4),
            "confidence": result.confidence,
            "completeness": result.completeness,
            "upper_team": result.upper_team,
            "lower_team": result.lower_team,
            "last_fetched_at": records[-1].get("fetched_at") or "",
        }
    except Exception as exc:  # dashboard should show one bad row, not blank the whole page
        return {
            "event_id": event_id,
            "status": "error",
            "outcome": "error",
            "scoreline": f"{home_goals}-{away_goals}",
            "match": path.name,
            "error": str(exc),
        }


def summarize_validate_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    stats = {"hit": 0, "miss": 0, "push": 0, "na": 0, "missing": 0, "error": 0}
    for record in records:
        outcome = str(record.get("outcome") or "")
        if outcome in stats:
            stats[outcome] += 1
        elif record.get("status") in ("missing", "empty"):
            stats["missing"] += 1
        elif record.get("status") == "error":
            stats["error"] += 1
    directional = stats["hit"] + stats["miss"]
    stats["directional"] = directional
    stats["accuracy"] = round(stats["hit"] / directional, 4) if directional else None
    stats["total"] = len(records)
    return stats


def validate_record_sort_key(record: dict[str, Any]) -> tuple[str, int]:
    try:
        event_id = int(record.get("event_id") or 0)
    except (TypeError, ValueError):
        event_id = 0
    return str(record.get("last_fetched_at") or record.get("match_time") or ""), event_id


def build_validate_records(snapshot_root: Path, scores_json: Path | None) -> dict[str, Any]:
    effective_scores = scores_json or DEFAULT_OKOOO_VALIDATE_SCORES_PATH
    scores = load_okooo_validate_scores(effective_scores)
    records = [
        replay_validate_result(snapshot_root, event_id, home_goals, away_goals)
        for event_id, (home_goals, away_goals) in sorted(scores.items())
    ]
    records = sorted(records, key=validate_record_sort_key, reverse=True)
    return {
        "scores_json": str(effective_scores),
        "records": records,
        "stats": summarize_validate_stats(records),
    }


def build_dashboard_payload(snapshot_root: Path, scores_json: Path | None) -> dict[str, Any]:
    snapshots = build_snapshot_events(snapshot_root)
    validation = build_validate_records(snapshot_root, scores_json)
    validation_by_id = {record.get("event_id"): record for record in validation["records"]}
    for event in snapshots:
        validation_record = validation_by_id.get(event["event_id"])
        is_finished = bool(validation_record and validation_record.get("scoreline"))
        event["is_finished"] = is_finished
        event["match_status"] = "finished" if is_finished else "unfinished"
        if validation_record:
            event["validation"] = {
                "outcome": validation_record.get("outcome"),
                "scoreline": validation_record.get("scoreline"),
                "margin": validation_record.get("margin"),
                "recommendation": validation_record.get("recommendation"),
                "purchase_side": validation_record.get("purchase_side"),
                "purchase_team": validation_record.get("purchase_team"),
                "model_recommendation": validation_record.get("model_recommendation"),
                "score": validation_record.get("score"),
                "confidence": validation_record.get("confidence"),
                "completeness": validation_record.get("completeness"),
            }
        else:
            event["validation"] = None
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot_root": str(snapshot_root),
        "snapshots": snapshots,
        "validation": validation,
    }


def analysis_result_payload(result: AnalysisResult) -> dict[str, Any]:
    data = normalize_result_dict(result.to_dict())
    data["signals"] = important_signals(result.to_dict(), limit=12)
    return data


def prune_pending_predictions() -> None:
    while len(PENDING_PREDICTIONS) > MAX_PENDING_PREDICTIONS:
        oldest = next(iter(PENDING_PREDICTIONS))
        PENDING_PREDICTIONS.pop(oldest, None)


def predict_latest(config: DashboardConfig, match_id: int, *, save_snapshot: bool) -> dict[str, Any]:
    client = OkoooClient(
        issue=config.issue,
        timeout=config.timeout,
        cookie=cookie_from_env(config.cookie_file),
        trade_trend=config.trade_trend,
        detail_max_pages=config.detail_max_pages,
    )
    store = SnapshotStore(config.snapshot_root)
    client.refresh()
    match: Match = client.build_match(match_id)
    result = Predictor(client, store).analyze(match)
    attach_snapshot_replay_fields(client, result.match)
    token = str(uuid.uuid4())
    PENDING_PREDICTIONS[token] = result
    prune_pending_predictions()
    path: Path | None = None
    if save_snapshot:
        path = store.save(result)
        PENDING_PREDICTIONS.pop(token, None)
    return {
        "saved": path is not None,
        "snapshot_path": str(path) if path else None,
        "save_token": None if path else token,
        "result": analysis_result_payload(result),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def save_pending_prediction(config: DashboardConfig, token: str) -> dict[str, Any]:
    result = PENDING_PREDICTIONS.pop(token, None)
    if result is None:
        raise DataError("prediction token not found or already saved")
    path = SnapshotStore(config.snapshot_root).save(result)
    return {
        "saved": True,
        "snapshot_path": str(path),
        "result": analysis_result_payload(result),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "OkoooDashboard/1.0"

    @property
    def config(self) -> DashboardConfig:
        return self.server.config  # type: ignore[attr-defined, no-any-return]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[dashboard] {self.address_string()} - {fmt % args}\n")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            raw = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "file not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DataError(f"invalid JSON body: {exc}") from exc
        if not isinstance(payload, dict):
            raise DataError("JSON body must be an object")
        return payload

    def handle_error(self, exc: Exception) -> None:
        status = HTTPStatus.BAD_REQUEST if isinstance(exc, DataError) else HTTPStatus.INTERNAL_SERVER_ERROR
        if not isinstance(exc, DataError):
            traceback.print_exc()
        self.send_json({"ok": False, "error": str(exc)}, status)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/dashboard", "/index.html"):
                self.send_file(self.config.html_path, "text/html; charset=utf-8")
                return
            if parsed.path == "/api/dashboard":
                query = parse_qs(parsed.query)
                scores_path = self.config.scores_json
                if query.get("scores_json"):
                    scores_path = Path(query["scores_json"][0]).expanduser()
                self.send_json({"ok": True, "data": build_dashboard_payload(self.config.snapshot_root, scores_path)})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = self.read_json_body()
            if parsed.path == "/api/predict":
                match_id = payload.get("match_id", payload.get("event_id"))
                if match_id is None:
                    raise DataError("match_id is required")
                data = predict_latest(self.config, int(match_id), save_snapshot=bool(payload.get("save_snapshot")))
                self.send_json({"ok": True, "data": data})
                return
            if parsed.path == "/api/save-prediction":
                token = str(payload.get("token") or "").strip()
                if not token:
                    raise DataError("token is required")
                self.send_json({"ok": True, "data": save_pending_prediction(self.config, token)})
                return
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
        except Exception as exc:
            self.handle_error(exc)


class DashboardServer(ThreadingHTTPServer):
    config: DashboardConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve an Okooo snapshot/validate dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--snapshot-dir", default=None, help="默认仓库根 .okooo_snapshots 或 OKOOO_SNAPSHOT_DIR")
    parser.add_argument("--scores-json", type=Path, default=None, help="默认 tools/okooo_validate_scores.json")
    parser.add_argument("--html", type=Path, default=DEFAULT_HTML_PATH)
    parser.add_argument("--issue", default=OKOOO_DEFAULT_ISSUE)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--env-file", type=Path, default=None)
    parser.add_argument("--no-dotenv", action="store_true")
    parser.add_argument("--cookie-file", default=None)
    parser.add_argument("--detail-max-pages", type=int, default=5)
    parser.add_argument("--no-trade-trend", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.no_dotenv:
        load_dotenv_file(args.env_file.expanduser() if args.env_file else ROOT / ".env")
    snapshot_root = Path(resolve_okooo_snapshot_dir(args.snapshot_dir)).expanduser()
    config = DashboardConfig(
        snapshot_root=snapshot_root,
        scores_json=args.scores_json.expanduser() if args.scores_json else None,
        html_path=args.html.expanduser(),
        issue=args.issue,
        timeout=args.timeout,
        cookie_file=args.cookie_file,
        trade_trend=not args.no_trade_trend,
        detail_max_pages=args.detail_max_pages,
    )
    server = DashboardServer((args.host, args.port), DashboardHandler)
    server.config = config
    print(f"Okooo dashboard: http://{args.host}:{server.server_port}/")
    print(f"snapshot_dir: {config.snapshot_root}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
