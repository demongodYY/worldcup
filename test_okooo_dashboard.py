from __future__ import annotations

import json
import unittest
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from okooo_ah_cli import (
    base_match_matches_filters,
    fill_finished_okooo_validate_scores,
    finished_snapshot_matches,
    parse_okooo_livecenter_final_scores,
    parse_validate_scoreline,
    write_okooo_validate_score,
)
from tools.okooo_dashboard_server import (
    build_snapshot_events,
    recommendation_outcome,
    summarize_validate_stats,
    validate_record_sort_key,
)


def snapshot_record(
    fetched_at: str,
    score: float,
    *,
    home_index: float,
    away_index: float,
    event_id: int = 42,
    match_time: str = "2026-06-20T12:00:00+00:00",
) -> dict:
    return {
        "schema": 1,
        "fetched_at": fetched_at,
        "match": {
            "event_id": event_id,
            "home": "主队",
            "away": "客队",
            "league_name": "世界杯",
            "match_time": match_time,
            "asian_line": "-0.5",
            "is_stop_update": False,
            "raw": {
                "BfIndexHome": home_index,
                "BfIndexAway": away_index,
                "BfAmountHome": home_index * 1000,
                "BfAmountAway": away_index * 1000,
                "BfPayoutHome": 5.0,
                "BfPayoutAway": 20.0,
                "BfPayoutDraw": 8.0,
                "AsianAvrHome": 0.88,
                "AsianAvrAway": 0.96,
                "EuroAvrHome": 1.9,
                "EuroAvrAway": 4.4,
            },
        },
        "result": {
            "event_id": event_id,
            "match": "主队 vs 客队",
            "recommendation": "上盘",
            "purchase_side": "上盘",
            "purchase_team": "主队",
            "model_recommendation": "上盘",
            "score": score,
            "confidence": 61,
            "completeness": 92,
            "upper_team": "主队",
            "lower_team": "客队",
            "signals": [
                {
                    "name": "亚盘水位",
                    "score": score,
                    "weight": 0.2,
                    "available": True,
                    "summary": "测试信号",
                }
            ],
            "warnings": [],
        },
    }


class OkoooDashboardTests(unittest.TestCase):
    def test_parse_validate_scoreline_accepts_common_separators(self) -> None:
        self.assertEqual(parse_validate_scoreline("2-1"), (2, 1))
        self.assertEqual(parse_validate_scoreline("2:1"), (2, 1))
        self.assertEqual(parse_validate_scoreline("2：1"), (2, 1))

    def test_parse_okooo_livecenter_final_scores(self) -> None:
        page = '''
        <tr state="End" matchid="1331779">
          <td><b class="font_red ctrl_homescore">1</b><b class="font_red ctrl_scoresplit">-</b><b class="font_red ctrl_awayscore">1</b></td>
        </tr>
        <tr state="On" matchid="1331783">
          <td><b class="font_red ctrl_homescore">3</b><b class="font_red ctrl_scoresplit">-</b><b class="font_red ctrl_awayscore">0</b></td>
        </tr>
        '''
        self.assertEqual(parse_okooo_livecenter_final_scores(page), {1331779: (1, 1)})

    def test_write_okooo_validate_score_adds_and_updates_score(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "scores.json"
            effective, old = write_okooo_validate_score(path, 123, 2, 1)
            self.assertEqual(effective, path)
            self.assertIsNone(old)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["123"], [2, 1])

            _, old = write_okooo_validate_score(path, 123, 3, 0)

            self.assertEqual(old, (2, 1))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["123"], [3, 0])

    def test_fill_finished_okooo_validate_scores_only_adds_missing_by_default(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.base_matches = {
                    1: SimpleNamespace(okooo_id=1, final_score=(2, 1), kickoff="a", home="主一", away="客一"),
                    2: SimpleNamespace(okooo_id=2, final_score=(0, 0), kickoff="b", home="主二", away="客二"),
                    3: SimpleNamespace(okooo_id=3, final_score=None, kickoff="c", home="主三", away="客三"),
                }

            def refresh(self, *, core_only: bool = False) -> None:
                self.core_only = core_only

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "scores.json"
            path.write_text(json.dumps({"1": [9, 9]}, ensure_ascii=False), encoding="utf-8")

            _, updates = fill_finished_okooo_validate_scores(Client(), path)

            self.assertEqual([item[0] for item in updates], [2])
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["1"], [9, 9])
            self.assertEqual(data["2"], [0, 0])

    def test_finished_snapshot_matches_only_returns_started_events(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            past = snapshot_record("2026-08-01T10:00:00+00:00", 0.1, home_index=50, away_index=30, event_id=11)
            past["match"]["home"] = "主队一"
            future = snapshot_record("2026-08-01T10:00:00+00:00", 0.1, home_index=50, away_index=30, event_id=12, match_time="2099-01-01T12:00:00+00:00")
            (root / "scores.jsonl").write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in (past, future)),
                encoding="utf-8",
            )
            self.assertEqual(finished_snapshot_matches(root), {11: ("主队一", "客队")})

    def test_okooo_league_filters_match_top_euro_leagues(self) -> None:
        args = SimpleNamespace(
            world_cup=False,
            premier_league=True,
            la_liga=True,
            bundesliga=True,
            league_contains="",
        )
        self.assertTrue(base_match_matches_filters(SimpleNamespace(league="英超"), args))
        self.assertTrue(base_match_matches_filters(SimpleNamespace(league="西甲"), args))
        self.assertTrue(base_match_matches_filters(SimpleNamespace(league="德甲"), args))
        self.assertTrue(base_match_matches_filters(SimpleNamespace(league="Premier League"), args))
        self.assertFalse(base_match_matches_filters(SimpleNamespace(league="意甲"), args))

    def test_okooo_league_contains_still_refines_preset_filters(self) -> None:
        args = SimpleNamespace(
            world_cup=False,
            premier_league=True,
            la_liga=True,
            bundesliga=False,
            league_contains="西",
        )
        self.assertFalse(base_match_matches_filters(SimpleNamespace(league="英超"), args))
        self.assertTrue(base_match_matches_filters(SimpleNamespace(league="西甲"), args))

    def test_build_snapshot_events_summarizes_series(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "42.jsonl"
            records = [
                snapshot_record("2026-06-20T10:00:00+00:00", 0.08, home_index=55, away_index=30),
                snapshot_record("2026-06-20T11:00:00+00:00", 0.18, home_index=67, away_index=24),
            ]
            path.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records), encoding="utf-8")

            events = build_snapshot_events(Path(tmp))

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["event_id"], 42)
        self.assertEqual(event["snapshot_count"], 2)
        self.assertEqual(len(event["series"]), 2)
        self.assertEqual(event["last_result"]["score"], round(median(point["score"] for point in event["series"]), 4))
        self.assertEqual(event["last_result"]["snapshot_median_count"], 2)
        self.assertEqual(event["last_result"]["snapshot_median_total_count"], 2)
        self.assertIn(event["last_result"]["purchase_side"], {"上盘", "下盘"})
        self.assertIn("headline", event["plain_explanation"])
        self.assertGreaterEqual(len(event["plain_explanation"]["bullets"]), 1)
        self.assertGreaterEqual(len(event["trend_reasons"]), 1)
        self.assertGreaterEqual(len(event["signal_deltas"]), 1)
        self.assertTrue({row["name"] for row in event["signal_deltas"]})
        self.assertTrue(event["series"][-1]["signal_scores"])

    def test_build_snapshot_events_sort_by_match_time_descending(self) -> None:
        with TemporaryDirectory() as tmp:
            old_match = snapshot_record(
                "2026-06-22T10:00:00+00:00",
                0.08,
                home_index=55,
                away_index=30,
                event_id=1,
                match_time="2026-06-20T12:00:00+00:00",
            )
            new_match = snapshot_record(
                "2026-06-20T10:00:00+00:00",
                0.18,
                home_index=67,
                away_index=24,
                event_id=2,
                match_time="2026-06-22T12:00:00+00:00",
            )
            (Path(tmp) / "1.jsonl").write_text(json.dumps(old_match, ensure_ascii=False), encoding="utf-8")
            (Path(tmp) / "2.jsonl").write_text(json.dumps(new_match, ensure_ascii=False), encoding="utf-8")

            events = build_snapshot_events(Path(tmp))

        self.assertEqual([event["event_id"] for event in events], [2, 1])

    def test_summarize_validate_stats_counts_outcomes(self) -> None:
        stats = summarize_validate_stats(
            [
                {"outcome": "full_win", "profit": 0.9},
                {"outcome": "half_win", "profit": 0.45},
                {"outcome": "full_loss", "profit": -1.0},
                {"outcome": "push"},
                {"outcome": "na"},
                {"status": "missing", "outcome": "missing"},
            ]
        )

        self.assertEqual(stats["hit"], 2)
        self.assertEqual(stats["miss"], 1)
        self.assertEqual(stats["full_win"], 1)
        self.assertEqual(stats["half_win"], 1)
        self.assertEqual(stats["full_loss"], 1)
        self.assertEqual(stats["push"], 1)
        self.assertEqual(stats["na"], 1)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(stats["directional"], 3)
        self.assertAlmostEqual(stats["accuracy"], 0.6667)
        self.assertAlmostEqual(stats["net_profit"], 0.35)
        self.assertAlmostEqual(stats["roi"], 0.1167)

    def test_recommendation_outcome_settles_against_upper_margin(self) -> None:
        outcome, margin = recommendation_outcome("上盘", "主队", "客队", "主队", "客队", "-0.5", 1, 0)
        self.assertEqual(outcome, "full_win")
        self.assertAlmostEqual(margin, 0.5)

        outcome, margin = recommendation_outcome("下盘", "主队", "客队", "主队", "客队", "-1", 1, 0)
        self.assertEqual(outcome, "push")
        self.assertAlmostEqual(margin, 0.0)

    def test_validate_records_sort_by_snapshot_time_descending(self) -> None:
        records = [
            {"event_id": 1, "last_fetched_at": "2026-06-20T10:00:00+00:00"},
            {"event_id": 2, "last_fetched_at": "2026-06-22T10:00:00+00:00"},
            {"event_id": 3, "match_time": "2026-06-21T10:00:00+00:00"},
            {"event_id": 4, "outcome": "missing"},
        ]

        ordered = sorted(records, key=validate_record_sort_key, reverse=True)

        self.assertEqual([record["event_id"] for record in ordered], [2, 3, 1, 4])


if __name__ == "__main__":
    unittest.main()
