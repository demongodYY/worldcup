import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from worldcup_ah_cli import (
    AnalysisResult,
    DataError,
    EuroTrendPoint,
    HandicapRow,
    Match,
    PriceVolumePoint,
    Predictor,
    ScheduledTask,
    SnapshotStore,
    build_scheduled_tasks,
    build_parser,
    normalize_line_for_spdex,
    parse_match,
    recommendation_from_score,
    score_bifa_odds_confirmation,
    score_handicap_row,
    score_hot_divergence_penalty,
    score_heat_handicap_divergence_penalty,
    score_bifa_heat_edge,
    score_price_volume,
    score_snapshot_signal_history,
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


class MissingTradeEuroClient(FakeClient):
    def price_volume(self, event_id, selection):
        raise DataError("stopped")

    def euro_trend(self, event_id):
        raise DataError("stopped")


class StaticSnapshotStore:
    def __init__(self, records):
        self.records = records

    def load_event(self, event_id):
        return self.records


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

    def test_hot_penalty_is_stronger_for_deep_lines(self):
        shallow = score_hot_divergence_penalty(
            heat_edge=0.65,
            confirmation_edge=0.02,
            payout_edge=-0.30,
            line_depth=0.5,
        )
        deep = score_hot_divergence_penalty(
            heat_edge=0.65,
            confirmation_edge=0.02,
            payout_edge=-0.30,
            line_depth=1.75,
        )
        self.assertGreater(deep, shallow)

    def test_hot_bifa_against_handicap_signal_is_penalized(self):
        penalty = score_heat_handicap_divergence_penalty(
            heat_edge=0.60,
            handicap_score=-0.35,
        )
        self.assertGreater(penalty, 0.30)

    def test_bifa_index_amount_split_does_not_create_clear_hot_side(self):
        raw = {
            "BfIndexHome": 45.6,
            "BfIndexAway": 39.2,
            "BfAmountHome": 366_015.0,
            "BfAmountAway": 1_345_419.0,
            "BfPayoutHome": 31.3,
            "BfPayoutAway": 12.9,
            "BfOddsHome": 6.40,
            "BfOddsAway": 1.62,
        }
        match = sample_match(raw=raw, asian_line="+1")
        upper_team, lower_team = upper_lower_teams(match)
        heat_edge = score_bifa_heat_edge(match, upper_team, lower_team)
        predictor = Predictor(FakeClient(handicap_rows=[]))
        result = predictor.analyze(match)
        bifa_signal = next(signal for signal in result.signals if signal.name == "必发指数")

        self.assertLess(abs(heat_edge), 0.18)
        self.assertIn("热度分裂已降权", bifa_signal.reason)

    def test_empty_handicap_data_degrades_to_watch_without_crashing(self):
        predictor = Predictor(FakeClient(handicap_rows=[]))
        result = predictor.analyze(sample_match())
        self.assertIn(result.recommendation, {"上盘", "下盘", "观望"})
        self.assertLess(result.completeness, 100)
        self.assertTrue(any(signal.name == "亚盘水位" and not signal.available for signal in result.signals))

    def test_network_failures_degrade_to_watch(self):
        predictor = Predictor(FakeClient(fail=True))
        result = predictor.analyze(sample_match())
        self.assertIn(result.recommendation, {"上盘", "下盘"})
        self.assertEqual(result.model_recommendation, "观望")
        self.assertLessEqual(result.confidence, 35)
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
        self.assertIn("升水", market_signal.reason)

    def test_upper_water_rise_is_not_strong_handicap_confirmation(self):
        match = sample_match(asian_line="+1")
        row = HandicapRow(51007, "PinnacleSports", 2.03, 1.90, 1.96, 1.85, 0.98, None)
        score = score_handicap_row(match, row, upper_team="客队")

        self.assertLess(score, 0.10)

    def test_snapshot_heat_rise_with_water_rise_penalizes_handicap(self):
        raw = {
            "BfIndexHome": 48.0,
            "BfIndexAway": 30.0,
            "BfAmountHome": 900_000.0,
            "BfAmountAway": 300_000.0,
            "BfPayoutHome": 8.0,
            "BfPayoutAway": 12.0,
            "BfOddsHome": 1.90,
            "BfOddsAway": 4.50,
            "AsianAvrHome": 1.96,
            "AsianAvrAway": 1.90,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 1.96, 1.90, 1.88, 1.98, 0.98, None),
        ]
        records = [
            {
                "match": {
                    "home": "主队",
                    "away": "客队",
                    "asian_line": "-0.75",
                    "raw": {
                        "BfIndexHome": 35.0,
                        "BfIndexAway": 30.0,
                        "BfAmountHome": 400_000.0,
                        "BfAmountAway": 300_000.0,
                        "AsianAvrHome": 1.86,
                        "AsianAvrAway": 2.02,
                    },
                },
                "result": {"score": 0.2, "upper_team": "主队", "lower_team": "客队", "signals": []},
            },
            {
                "match": {
                    "home": "主队",
                    "away": "客队",
                    "asian_line": "-0.75",
                    "raw": {
                        "BfIndexHome": 48.0,
                        "BfIndexAway": 30.0,
                        "BfAmountHome": 900_000.0,
                        "BfAmountAway": 300_000.0,
                        "AsianAvrHome": 1.96,
                        "AsianAvrAway": 1.90,
                    },
                },
                "result": {"score": 0.18, "upper_team": "主队", "lower_team": "客队", "signals": []},
            },
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows), StaticSnapshotStore(records))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-0.75"))
        handicap_signal = next(signal for signal in result.signals if signal.name == "亚盘水位")

        self.assertLess(handicap_signal.score, 0)
        self.assertIn("历史热度升高但上盘水位也升高", handicap_signal.reason)

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
        self.assertIn(result.lean, {"上盘", "下盘", "无明显倾向"})
        self.assertIn("lean", result.to_dict())
        self.assertIn("lean_team", result.to_dict())
        self.assertIn("purchase_side", result.to_dict())
        self.assertIn("purchase_score", result.to_dict())
        self.assertIn(result.purchase_side, {"上盘", "下盘"})

    def test_tiny_score_has_no_clear_lean(self):
        match = sample_match()
        result = AnalysisResult(
            match=match,
            recommendation="观望",
            score=0.01,
            confidence=10,
            completeness=80,
            upper_team=match.home,
            lower_team=match.away,
            signals=[],
            warnings=[],
        )
        self.assertEqual(result.lean, "无明显倾向")
        self.assertEqual(result.lean_team, "")

    def test_new_optimization_signals_are_added(self):
        raw = {
            "BfIndexHome": 58.0,
            "BfIndexAway": 24.0,
            "BfAmountHome": 900_000.0,
            "BfAmountAway": 260_000.0,
            "BfPayoutHome": 20.0,
            "BfPayoutAway": -12.0,
            "BfOddsHome": 1.85,
            "BfOddsAway": 4.80,
            "BfOddsDraw": 3.25,
            "EuroAvrHome": 1.82,
            "EuroAvrAway": 4.60,
            "EuroAvrDraw": 3.20,
            "KellyHome": 1.9,
            "KellyAway": 5.5,
            "KellyDraw": 1.7,
            "AsianAvrHome": 1.95,
            "AsianAvrAway": 1.92,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 1.92, 1.96, 1.90, 1.98, 0.98, None),
            HandicapRow(51003, "Ysb88", 1.95, 1.93, 1.91, 1.97, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-0.5"))
        signal_names = {signal.name for signal in result.signals}
        self.assertIn("平局风险", signal_names)
        self.assertIn("盘口合理性", signal_names)
        self.assertIn("公司一致性", signal_names)
        self.assertIn("盘口深度/打穿能力", signal_names)
        self.assertIn("资金/盘口弹性", signal_names)
        self.assertIn("外部赔率/实力校验", signal_names)
        self.assertIn("高低水价值", signal_names)
        draw_signal = next(signal for signal in result.signals if signal.name == "平局风险")
        self.assertLess(draw_signal.score, 0.2)

    def test_shallow_actual_line_with_high_upper_water_penalizes_upper(self):
        raw = {
            **sample_match().raw,
            "EuroAvrHome": 2.05,
            "EuroAvrAway": 4.30,
            "BfOddsHome": 2.08,
            "BfOddsAway": 4.10,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 2.10, 1.82, 1.90, 1.98, 0.98, None),
            HandicapRow(51003, "Ysb88", 2.08, 1.84, 1.91, 1.96, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-0.5"))
        fair_signal = next(signal for signal in result.signals if signal.name == "盘口合理性")

        self.assertLess(fair_signal.score, 0)
        self.assertIn("实际盘口偏浅且上盘高水", fair_signal.reason)

    def test_bookmaker_consensus_does_not_flip_negative_majority_positive(self):
        rows = [
            HandicapRow(51007, "PinnacleSports", 1.80, 2.08, 2.00, 1.88, 0.98, None),
            HandicapRow(51003, "Ysb88", 2.10, 1.78, 1.85, 2.02, 0.96, None),
            HandicapRow(51004, "IBC", 2.08, 1.80, 1.86, 2.00, 0.96, None),
            HandicapRow(51005, "Singbet", 2.06, 1.82, 1.88, 1.98, 0.96, None),
            HandicapRow(51006, "DafaBet", 2.04, 1.84, 1.88, 1.98, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(asian_line="-0.75"))
        consensus_signal = next(signal for signal in result.signals if signal.name == "公司一致性")

        self.assertLessEqual(consensus_signal.score, 0)

    def test_market_elasticity_penalizes_hot_side_when_water_rises(self):
        hot_raw = {
            "BfIndexHome": 72.0,
            "BfIndexAway": 16.0,
            "BfAmountHome": 1_700_000.0,
            "BfAmountAway": 260_000.0,
            "BfPayoutHome": 28.0,
            "BfPayoutAway": -5.0,
            "BfOddsHome": 1.95,
            "BfOddsAway": 4.20,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 2.06, 1.82, 1.84, 2.04, 0.98, None),
            HandicapRow(51003, "Ysb88", 2.02, 1.86, 1.86, 2.02, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(raw=hot_raw, asian_line="-0.75"))
        elasticity_signal = next(signal for signal in result.signals if signal.name == "资金/盘口弹性")

        self.assertLess(elasticity_signal.score, 0)
        self.assertIn("水位上升", elasticity_signal.reason)

    def test_high_water_without_model_value_is_penalized(self):
        raw = {
            "BfIndexHome": 72.0,
            "BfIndexAway": 18.0,
            "BfAmountHome": 1_500_000.0,
            "BfAmountAway": 320_000.0,
            "BfPayoutHome": 42.0,
            "BfPayoutAway": -8.0,
            "BfOddsHome": 3.10,
            "BfOddsAway": 1.95,
            "EuroAvrHome": 2.95,
            "EuroAvrAway": 2.05,
            "KellyHome": 6.0,
            "KellyAway": 2.2,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 2.14, 1.77, 1.86, 2.02, 0.98, None),
            HandicapRow(51003, "Ysb88", 2.10, 1.80, 1.88, 2.00, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-0.75"))
        water_value_signal = next(signal for signal in result.signals if signal.name == "高低水价值")

        self.assertLess(water_value_signal.score, 0)
        self.assertIn("缺少价值补偿", water_value_signal.reason)

    def test_high_water_can_be_value_when_model_probability_beats_market(self):
        raw = {
            **sample_match().raw,
            "BfOddsHome": 1.90,
            "BfOddsAway": 4.60,
            "EuroAvrHome": 1.74,
            "EuroAvrAway": 5.10,
            "KellyHome": 1.8,
            "KellyAway": 6.8,
            "ExternalSpreadUpperPrice": 1.68,
            "ExternalSpreadLowerPrice": 2.28,
            "ExternalFairLineDepth": 1.35,
            "ExternalPowerEdge": 0.55,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 2.08, 1.82, 2.18, 1.74, 0.98, None),
            HandicapRow(51003, "Ysb88", 2.04, 1.84, 2.12, 1.78, 0.96, None),
        ]
        predictor = Predictor(FakeClient(handicap_rows=rows))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-0.75"))
        water_value_signal = next(signal for signal in result.signals if signal.name == "高低水价值")

        self.assertGreater(water_value_signal.score, 0)
        self.assertIn("有赔率补偿", water_value_signal.reason)

    def test_external_consensus_signal_uses_adapter_fields(self):
        raw = {
            **sample_match().raw,
            "ExternalSpreadUpperPrice": 1.75,
            "ExternalSpreadLowerPrice": 2.16,
            "ExternalH2hUpperPrice": 1.62,
            "ExternalH2hLowerPrice": 5.20,
            "ExternalFairLineDepth": 1.20,
            "ExternalPowerEdge": 0.34,
        }
        predictor = Predictor(FakeClient(handicap_rows=[]))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-0.75"))
        external_signal = next(signal for signal in result.signals if signal.name == "外部赔率/实力校验")

        self.assertTrue(external_signal.available)
        self.assertGreater(external_signal.score, 0.25)
        self.assertIn("外部让球赔率", external_signal.reason)

    def test_middle_line_cover_risk_downgrades_stale_upper_pick(self):
        raw = {
            "BfIndexHome": 41.7,
            "BfIndexAway": 39.1,
            "BfAmountHome": 1_703_755.0,
            "BfAmountAway": 456_539.0,
            "BfPayoutHome": 17.0,
            "BfPayoutAway": 9.4,
            "BfOddsHome": 1.75,
            "BfOddsAway": 5.70,
            "BfOddsDraw": 4.00,
            "EuroAvrHome": 1.65,
            "EuroAvrAway": 5.34,
            "EuroAvrDraw": 3.79,
            "KellyHome": 2.70,
            "KellyAway": 29.11,
            "KellyDraw": 5.96,
        }
        rows = [
            HandicapRow(51007, "PinnacleSports", 1.93, 2.00, 2.06, 1.78, 0.98, None),
            HandicapRow(51003, "Ysb88", 1.92, 1.97, 1.85, 2.06, 0.96, None),
        ]
        records = [
            {
                "result": {
                    "score": 0.38,
                    "signals": [
                        {"name": "必发成交走势", "score": 0.37, "available": True},
                        {"name": "亚盘水位", "score": 0.29, "available": True},
                        {"name": "盘口深度/打穿能力", "score": 0.31, "available": True},
                        {"name": "公司一致性", "score": 0.15, "available": True},
                    ],
                }
            },
            {
                "result": {
                    "score": 0.30,
                    "signals": [
                        {"name": "必发成交走势", "score": 0.0, "available": False},
                        {"name": "亚盘水位", "score": 0.18, "available": True},
                        {"name": "盘口深度/打穿能力", "score": 0.20, "available": True},
                        {"name": "公司一致性", "score": -0.12, "available": True},
                    ],
                }
            },
        ]
        predictor = Predictor(MissingTradeEuroClient(handicap_rows=rows), StaticSnapshotStore(records))
        result = predictor.analyze(sample_match(raw=raw, asian_line="-0.75"))

        self.assertEqual(result.recommendation, "下盘")
        self.assertEqual(result.purchase_side, "下盘")
        self.assertTrue(result.is_reversed)
        self.assertIn("风险优先反向", result.decision_reason)
        cover_signal = next(signal for signal in result.signals if signal.name == "赢盘门槛风险")
        market_signal = next(signal for signal in result.signals if signal.name == "市场平衡/背离")
        self.assertLessEqual(cover_signal.score, -0.35)
        self.assertLessEqual(market_signal.score, 0.55)

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

    def test_snapshot_store_appends_and_loads_event_records(self):
        predictor = Predictor(FakeClient(handicap_rows=[]))
        result = predictor.analyze(sample_match(event_id=123))
        with TemporaryDirectory() as tmpdir:
            store = SnapshotStore(tmpdir)
            path = store.save(result, fetched_at=datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc))
            store.save(result, fetched_at=datetime(2026, 6, 13, 1, 0, tzinfo=timezone.utc))
            records = store.load_event(123)

        self.assertEqual(path.name, "123.jsonl")
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["match"]["event_id"], 123)
        self.assertIn("result", records[0])

    def test_snapshot_signal_history_uses_full_series(self):
        def record(trade, euro, handicap):
            return {
                "result": {
                    "signals": [
                        {"name": "必发成交走势", "score": trade, "available": True},
                        {"name": "欧赔/Kelly", "score": euro, "available": True},
                        {"name": "亚盘水位", "score": handicap, "available": True},
                    ]
                }
            }

        score, reason = score_snapshot_signal_history(
            [
                record(-0.30, -0.20, 0.00),
                record(0.05, 0.10, 0.12),
                record(0.42, 0.30, 0.22),
            ]
        )

        self.assertGreater(score, 0.25)
        self.assertIn("全历史基础信号", reason)
        self.assertIn("必发成交走势历史", reason)

    def test_snapshot_signal_history_keeps_single_valid_historical_evidence(self):
        records = [
            {
                "result": {
                    "signals": [
                        {"name": "必发成交走势", "score": 0.50, "available": True},
                        {"name": "欧赔/Kelly", "score": 0.20, "available": True},
                    ]
                }
            },
            {
                "result": {
                    "signals": [
                        {"name": "必发成交走势", "score": 0.0, "available": False},
                        {"name": "欧赔/Kelly", "score": 0.0, "available": False},
                    ]
                }
            },
        ]

        score, reason = score_snapshot_signal_history(records)

        self.assertGreater(score, 0.05)
        self.assertIn("历史仅1点", reason)

    def test_trade_signal_uses_snapshot_history_when_live_stopped(self):
        records = [
            {
                "match": {
                    "home": "主队",
                    "away": "客队",
                    "asian_line": "-0.75",
                    "raw": {},
                },
                "result": {
                    "score": 0.10,
                    "upper_team": "主队",
                    "lower_team": "客队",
                    "signals": [
                        {
                            "name": "必发成交走势",
                            "score": 0.20,
                            "available": True,
                            "reason": "主队: 买量 100 / 卖量 50",
                        }
                    ],
                },
            },
            {
                "match": {
                    "home": "主队",
                    "away": "客队",
                    "asian_line": "-0.75",
                    "raw": {},
                },
                "result": {
                    "score": 0.18,
                    "upper_team": "主队",
                    "lower_team": "客队",
                    "signals": [
                        {
                            "name": "必发成交走势",
                            "score": 0.50,
                            "available": True,
                            "reason": "主队: 买量 300 / 卖量 50",
                        }
                    ],
                },
            },
        ]
        predictor = Predictor(MissingTradeEuroClient(), StaticSnapshotStore(records))
        result = predictor.analyze(sample_match())
        trade_signal = next(signal for signal in result.signals if signal.name == "必发成交走势")

        self.assertTrue(trade_signal.available)
        self.assertGreater(trade_signal.score, 0.25)
        self.assertIn("历史快照兜底", trade_signal.reason)
        self.assertIn("真实成交信号历史", trade_signal.reason)

    def test_snapshot_trade_fallback_ignores_prior_fallback_signal(self):
        def record(upper_amount, lower_amount, upper_odds, lower_odds):
            return {
                "match": {
                    "home": "主队",
                    "away": "客队",
                    "asian_line": "-0.75",
                    "raw": {
                        "BfAmountHome": upper_amount,
                        "BfAmountAway": lower_amount,
                        "BfOddsHome": upper_odds,
                        "BfOddsAway": lower_odds,
                    },
                },
                "result": {
                    "score": 0.10,
                    "upper_team": "主队",
                    "lower_team": "客队",
                    "signals": [
                        {
                            "name": "必发成交走势",
                            "score": 0.95,
                            "available": True,
                            "reason": "实时成交走势接口不可用，历史快照兜底：旧兜底结果",
                        }
                    ],
                },
            }

        records = [
            record(100_000, 100_000, 1.80, 4.20),
            record(120_000, 320_000, 1.95, 3.80),
        ]
        predictor = Predictor(MissingTradeEuroClient(), StaticSnapshotStore(records))
        result = predictor.analyze(sample_match())
        trade_signal = next(signal for signal in result.signals if signal.name == "必发成交走势")

        self.assertTrue(trade_signal.available)
        self.assertLess(trade_signal.score, -0.50)
        self.assertIn("基础成交/赔率", trade_signal.reason)
        self.assertNotIn("旧兜底结果", trade_signal.reason)

    def test_watch_schedule_catches_up_latest_missed_window(self):
        now = datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc)
        match = sample_match(event_id=456, match_time=now + timedelta(hours=3))
        tasks = build_scheduled_tasks([match], now=now, horizon=timedelta(hours=24))
        catch_up_tasks = [task for task in tasks if task.is_catch_up]

        self.assertEqual(len(catch_up_tasks), 1)
        self.assertIn("T-4h", catch_up_tasks[0].label)
        self.assertEqual(catch_up_tasks[0].run_at, now)
        self.assertTrue(any(task.label.startswith("T-60m") for task in tasks))

    def test_watch_schedule_skips_completed_window(self):
        now = datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc)
        match = sample_match(event_id=789, match_time=now + timedelta(hours=3))
        completed_key = f"{match.event_id}:{match.match_time.isoformat()}:T-4h 观察热度/水位背离"
        tasks = build_scheduled_tasks(
            [match],
            now=now,
            horizon=timedelta(hours=24),
            completed={completed_key},
        )

        self.assertFalse(any(task.key == completed_key for task in tasks))

    def test_scheduler_state_marks_task_completed(self):
        match = sample_match(event_id=321)
        task = ScheduledTask(
            key="321:test:T-24h",
            label="T-24h 建立基线",
            run_at=match.match_time - timedelta(hours=24),
            match=match,
            do_predict=False,
        )
        with TemporaryDirectory() as tmpdir:
            store = SnapshotStore(tmpdir)
            store.mark_task_completed(task, completed_at=datetime(2026, 6, 13, 0, 0, tzinfo=timezone.utc))
            state = store.load_scheduler_state()

        self.assertIn(task.key, state["completed"])
        self.assertEqual(state["completed"][task.key]["event_id"], 321)

    def test_watch_defaults_to_hourly_polling(self):
        args = build_parser().parse_args(["watch", "--once"])
        self.assertEqual(args.poll_seconds, 3600)


if __name__ == "__main__":
    unittest.main()
