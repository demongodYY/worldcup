from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tools.okooo_dashboard_server import (
    build_snapshot_events,
    recommendation_outcome,
    summarize_validate_stats,
    validate_record_sort_key,
)


def snapshot_record(fetched_at: str, score: float, *, home_index: float, away_index: float) -> dict:
    return {
        "schema": 1,
        "fetched_at": fetched_at,
        "match": {
            "event_id": 42,
            "home": "主队",
            "away": "客队",
            "league_name": "世界杯",
            "match_time": "2026-06-20T12:00:00+00:00",
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
            "event_id": 42,
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
        self.assertEqual(event["last_result"]["score"], event["series"][-1]["score"])
        self.assertIn(event["last_result"]["purchase_side"], {"上盘", "下盘", "观望"})

    def test_summarize_validate_stats_counts_outcomes(self) -> None:
        stats = summarize_validate_stats(
            [
                {"outcome": "hit"},
                {"outcome": "hit"},
                {"outcome": "miss"},
                {"outcome": "push"},
                {"outcome": "na"},
                {"status": "missing", "outcome": "missing"},
            ]
        )

        self.assertEqual(stats["hit"], 2)
        self.assertEqual(stats["miss"], 1)
        self.assertEqual(stats["push"], 1)
        self.assertEqual(stats["na"], 1)
        self.assertEqual(stats["missing"], 1)
        self.assertEqual(stats["directional"], 3)
        self.assertAlmostEqual(stats["accuracy"], 0.6667)

    def test_recommendation_outcome_settles_against_upper_margin(self) -> None:
        outcome, margin = recommendation_outcome("上盘", "主队", "客队", "主队", "客队", "-0.5", 1, 0)
        self.assertEqual(outcome, "hit")
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
