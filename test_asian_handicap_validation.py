from __future__ import annotations

import unittest

from asian_handicap_validation import (
    normalize_asian_decimal_odds,
    select_last_eligible_snapshots,
    select_snapshot_at_cutoff,
    settle_asian_handicap,
    snapshot_validation_issues,
    summarize_settlements,
)


def record(fetched_at: str, *, match_time: str = "2026-06-25T12:00:00+00:00") -> dict:
    return {
        "fetched_at": fetched_at,
        "match": {
            "home": "主队",
            "away": "客队",
            "match_time": match_time,
            "asian_line": "-0.75",
            "raw": {"AsianAvrHome": 1.90, "AsianAvrAway": 1.96},
        },
        "result": {"recommendation": "上盘", "upper_team": "主队", "lower_team": "客队"},
    }


class AsianHandicapValidationTests(unittest.TestCase):
    def test_quarter_line_supports_half_win_and_half_loss(self) -> None:
        half_win = settle_asian_handicap(
            "上盘", "主队", "主队", "客队", -0.75, 1, 0, decimal_odds=1.90
        )
        self.assertEqual(half_win.outcome, "half_win")
        self.assertAlmostEqual(half_win.unit_result, 0.5)
        self.assertAlmostEqual(half_win.profit or 0, 0.45)

        half_loss = settle_asian_handicap(
            "上盘", "客队", "主队", "客队", 1.25, 0, 1, decimal_odds=1.90
        )
        self.assertEqual(half_loss.outcome, "half_loss")
        self.assertAlmostEqual(half_loss.unit_result, -0.5)
        self.assertAlmostEqual(half_loss.profit or 0, -0.5)

    def test_hong_kong_water_is_normalized_to_decimal_odds(self) -> None:
        self.assertAlmostEqual(normalize_asian_decimal_odds(0.88) or 0, 1.88)
        self.assertAlmostEqual(normalize_asian_decimal_odds(1.92) or 0, 1.92)

    def test_cutoff_selection_uses_nearest_tradeable_snapshot(self) -> None:
        records = [
            record("2026-06-25T10:59:00+00:00"),
            record("2026-06-25T11:08:00+00:00"),
            record("2026-06-25T11:31:00+00:00"),
        ]
        selected_60 = select_snapshot_at_cutoff(records, 60, tolerance_minutes=15)
        selected_30 = select_snapshot_at_cutoff(records, 30, tolerance_minutes=15)
        self.assertIsNotNone(selected_60)
        self.assertIsNotNone(selected_30)
        self.assertEqual(selected_60.index, 0)
        self.assertEqual(selected_30.index, 2)

    def test_last_two_selection_ignores_post_kickoff_rows(self) -> None:
        records = [
            record("2026-06-25T10:30:00+00:00"),
            record("2026-06-25T11:30:00+00:00"),
            record("2026-06-25T11:55:00+00:00"),
            record("2026-06-25T12:01:00+00:00"),
        ]
        selected = select_last_eligible_snapshots(records, count=2)
        self.assertEqual([item.index for item in selected], [1, 2])

    def test_post_kickoff_and_placeholder_snapshots_are_excluded(self) -> None:
        post = record("2026-06-25T12:01:00+00:00")
        self.assertIn("post_kickoff", snapshot_validation_issues(post))

        placeholder = record("2026-06-25T11:30:00+00:00")
        placeholder["match"]["raw"].update(
            {
                "_okooo_popularity_home": 50.0,
                "_okooo_popularity_draw": 28.0,
                "_okooo_popularity_away": 22.0,
                "_okooo_diff_home": 10.0,
                "_okooo_diff_draw": 5.0,
                "_okooo_diff_away": 10.0,
                "_okooo_zhishu_tips": "胜 平",
            }
        )
        self.assertIn("placeholder_features", snapshot_validation_issues(placeholder))

    def test_roi_uses_only_rows_with_real_prices(self) -> None:
        stats = summarize_settlements(
            [
                {"outcome": "full_win", "profit": 0.9},
                {"outcome": "half_loss", "profit": -0.5},
                {"outcome": "full_win", "profit": None},
                {"outcome": "push", "profit": 0.0},
            ]
        )
        self.assertEqual(stats["roi_bets"], 3)
        self.assertAlmostEqual(stats["net_profit"], 0.4)
        self.assertAlmostEqual(stats["roi"], 0.1333)


if __name__ == "__main__":
    unittest.main()
