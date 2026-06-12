import unittest
from datetime import datetime, timezone

from worldcup_ah_cli import (
    DataError,
    EuroTrendPoint,
    HandicapRow,
    Match,
    PriceVolumePoint,
    Predictor,
    normalize_line_for_spdex,
    parse_match,
    recommendation_from_score,
    score_bifa_odds_confirmation,
    score_hot_divergence_penalty,
    score_heat_handicap_divergence_penalty,
    score_price_volume,
    upper_lower_teams,
)


def sample_match(**overrides):
    raw = {
        "BfIndexHome": 60.0,
        "BfIndexAway": 25.0,
        "BfAmountHome": 1_000_000.0,
        "BfAmountAway": 300_000.0,
        "BfPayoutHome": 5.0,
        "BfPayoutAway": 30.0,
        "BfOddsHome": 1.80,
        "BfOddsAway": 4.20,
    }
    values = {
        "event_id": 1,
        "match_time": datetime(2026, 6, 13, 1, 0, tzinfo=timezone.utc),
        "home": "主队",
        "away": "客队",
        "league_id": 911,
        "league_name": "世界杯",
        "asian_line": "-0.75",
        "is_stop_update": False,
        "raw": raw,
    }
    values.update(overrides)
    return Match(**values)


class FakeClient:
    def __init__(self, handicap_rows=None, fail=False):
        self.handicap_rows = handicap_rows if handicap_rows is not None else []
        self.fail = fail

    def price_volume(self, event_id, selection):
        if self.fail:
            raise DataError("network down")
        if selection == "home":
            return [
                PriceVolumePoint(1.90, 1000, None, "买+"),
                PriceVolumePoint(1.82, 200, None, "卖"),
            ]
        return [
            PriceVolumePoint(3.80, 100, None, "买"),
            PriceVolumePoint(4.10, 1000, None, "卖+"),
        ]

    def handicap_list(self, event_id, asian_line):
        if self.fail:
            raise DataError("network down")
        return self.handicap_rows

    def handicap_detail(self, event_id, asian_line, bookmaker_id):
        return []

    def euro_trend(self, event_id):
        if self.fail:
            raise DataError("network down")
        return [
            EuroTrendPoint(None, 1.90, 3.5, 4.2, 2.0, 4.0, 6.0),
            EuroTrendPoint(None, 1.78, 3.6, 4.5, 1.5, 4.2, 6.5),
        ]


class WorldCupAhCliTests(unittest.TestCase):
    def test_parse_spdex_match_detail_shape(self):
        match = parse_match(
            {
                "Match": {
                    "EventId": 35438948,
                    "MatchTime": "2026-06-12T02:00:00Z",
                    "HomeTeam": "韩国",
                    "GuestTeam": "捷克",
                    "LeagueId": 911,
                    "MatchPath": "世界杯",
                },
                "BaseInfo": {
                    "AsianAvrLet": "0",
                    "BfIndexHome": 33.8,
                    "BfIndexAway": 32.4,
                },
            }
        )
        self.assertEqual(match.event_id, 35438948)
        self.assertEqual(match.away, "捷克")
        self.assertEqual(match.asian_line, "0")

    def test_normalize_positive_line_for_spdex(self):
        self.assertEqual(normalize_line_for_spdex("+1.75"), "1.75")
        self.assertEqual(normalize_line_for_spdex("-0.750"), "-0.75")
        self.assertEqual(normalize_line_for_spdex("0.00"), "0")

    def test_upper_lower_mapping_for_negative_and_positive_lines(self):
        self.assertEqual(upper_lower_teams(sample_match(asian_line="-0.5")), ("主队", "客队"))
        self.assertEqual(upper_lower_teams(sample_match(asian_line="+1.25")), ("客队", "主队"))

    def test_recommendation_thresholds(self):
        self.assertEqual(recommendation_from_score(0.19), "上盘")
        self.assertEqual(recommendation_from_score(-0.19), "下盘")
        self.assertEqual(recommendation_from_score(0.02), "观望")

    def test_price_volume_scoring_prefers_buy_pressure_and_price_drop(self):
        score, reason = score_price_volume(
            [
                PriceVolumePoint(2.00, 900, None, "买+"),
                PriceVolumePoint(1.90, 100, None, "卖"),
            ]
        )
        self.assertIsNotNone(score)
        self.assertGreater(score, 0)
        self.assertIn("买量", reason)

    def test_hot_bifa_without_odds_confirmation_is_penalized(self):
        confirmation = score_bifa_odds_confirmation(3.40, 2.10)
        penalty = score_hot_divergence_penalty(
            heat_edge=0.65,
            confirmation_edge=confirmation,
            payout_edge=-0.20,
        )
        self.assertLess(confirmation, 0)
        self.assertGreater(penalty, 0.30)

    def test_strong_odds_confirmation_tolerates_moderate_payout_pressure(self):
        penalty = score_hot_divergence_penalty(
            heat_edge=0.37,
            confirmation_edge=0.75,
            payout_edge=-0.12,
        )
        self.assertEqual(penalty, 0.0)

    def test_hot_bifa_against_handicap_signal_is_penalized(self):
        penalty = score_heat_handicap_divergence_penalty(
            heat_edge=0.60,
            handicap_score=-0.35,
        )
        self.assertGreater(penalty, 0.30)

    def test_empty_handicap_data_degrades_to_watch_without_crashing(self):
        predictor = Predictor(FakeClient(handicap_rows=[]))
        result = predictor.analyze(sample_match())
        self.assertIn(result.recommendation, {"上盘", "下盘", "观望"})
        self.assertLess(result.completeness, 100)
        self.assertTrue(any(signal.name == "亚盘水位" and not signal.available for signal in result.signals))

    def test_network_failures_degrade_to_watch(self):
        predictor = Predictor(FakeClient(fail=True))
        result = predictor.analyze(sample_match())
        self.assertEqual(result.recommendation, "观望")
        self.assertTrue(result.warnings)

    def test_stopped_match_keeps_model_confidence_for_review(self):
        rows = [
            HandicapRow(51007, "PinnacleSports", 1.80, 2.08, 2.00, 1.88, 0.98, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(is_stop_update=True))
        self.assertGreater(result.confidence, 0)
        self.assertTrue(any("仅供复盘" in warning for warning in result.warnings))

    def test_handicap_rows_can_contribute_to_upper_pick(self):
        rows = [
            HandicapRow(51007, "PinnacleSports", 1.80, 2.08, 2.00, 1.88, 0.98, None),
            HandicapRow(51003, "Ysb88", 1.82, 2.02, 1.92, 1.94, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match())
        self.assertGreater(result.score, 0)

    def test_public_heat_with_sweet_upper_water_does_not_auto_push_upper(self):
        hot_raw = {
            "BfIndexHome": 82.0,
            "BfIndexAway": 8.0,
            "BfAmountHome": 2_000_000.0,
            "BfAmountAway": 180_000.0,
            "BfPayoutHome": 45.0,
            "BfPayoutAway": -20.0,
            "BfOddsHome": 2.70,
            "BfOddsAway": 2.20,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 2.10, 1.78, 1.82, 2.02, 0.98, None),
            HandicapRow(51003, "Ysb88", 2.06, 1.82, 1.84, 2.00, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(raw=hot_raw))
        self.assertNotEqual(result.recommendation, "上盘")
        self.assertLess(result.score, 0.18)
        market_signal = next(signal for signal in result.signals if signal.name == "市场平衡/背离")
        self.assertLess(market_signal.score, 0)
        self.assertIn("更好买", market_signal.reason)

    def test_quiet_heat_with_active_handicap_defense_adds_market_balance(self):
        quiet_raw = {
            "BfIndexHome": 42.0,
            "BfIndexAway": 34.0,
            "BfAmountHome": 420_000.0,
            "BfAmountAway": 390_000.0,
            "BfPayoutHome": 2.0,
            "BfPayoutAway": 8.0,
            "BfOddsHome": 1.92,
            "BfOddsAway": 3.80,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 1.76, 2.14, 1.96, 1.88, 0.98, None),
            HandicapRow(51003, "Ysb88", 1.78, 2.08, 1.94, 1.90, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(raw=quiet_raw))
        market_signal = next(signal for signal in result.signals if signal.name == "市场平衡/背离")
        self.assertGreater(market_signal.score, 0)
        self.assertIn("主动防守", market_signal.reason)

    def test_result_always_exposes_lean_even_when_watch(self):
        predictor = Predictor(FakeClient(handicap_rows=[]))
        result = predictor.analyze(sample_match())
        self.assertIn(result.lean, {"上盘", "下盘"})
        self.assertIn("lean", result.to_dict())
        self.assertIn("lean_team", result.to_dict())

    def test_static_handicap_fallback_is_not_overweighted(self):
        raw = {
            "BfIndexHome": 45.0,
            "BfIndexAway": 40.4,
            "BfAmountHome": 2_787_051.0,
            "BfAmountAway": 373_746.0,
            "BfPayoutHome": 17.7,
            "BfPayoutAway": 5.6,
            "BfOddsHome": 1.44,
            "BfOddsAway": 10.0,
            "AsianAvrHome": 2.05,
            "AsianAvrAway": 1.83,
            "EuroAvrHome": 1.42,
            "EuroAvrAway": 8.29,
            "KellyHome": 3.03,
            "KellyAway": 71.72,
        }
        predictor = Predictor(FakeClient(handicap_rows=[]))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-1.25"))
        handicap_signal = next(signal for signal in result.signals if signal.name == "亚盘水位")
        self.assertGreater(handicap_signal.score, -0.35)
        self.assertEqual(result.lean, "上盘")


if __name__ == "__main__":
    unittest.main()
