import unittest

from exchange_odds_ah_cli import (
    augment_match_with_betfair,
    build_match_from_odds_event,
    choose_main_spread_line,
    run_selftest,
    sample_betfair_market,
    sample_odds_event,
    stable_event_id,
)
from worldcup_ah_cli import Predictor


class ExchangeOddsAhCliTests(unittest.TestCase):
    def test_stable_event_id_is_repeatable_positive_int(self):
        first = stable_event_id("fixture-brazil-morocco")
        second = stable_event_id("fixture-brazil-morocco")

        self.assertEqual(first, second)
        self.assertIsInstance(first, int)
        self.assertGreater(first, 0)

    def test_odds_api_event_maps_spread_and_h2h_fields(self):
        event = sample_odds_event()
        match, rows, _euro_points, _price_points = build_match_from_odds_event(event)

        self.assertEqual(choose_main_spread_line(event), -1.0)
        self.assertEqual(match.asian_line, "-1")
        self.assertEqual(match.raw["OddsApiSpreadUpperPrice"], match.raw["OddsApiSpreadHomePrice"])
        self.assertGreater(match.raw["EuroAvrHome"], 1.0)
        self.assertGreater(match.raw["EuroAvrDraw"], 1.0)
        self.assertGreater(match.raw["ExternalFairLineDepth"], 0.0)
        self.assertEqual(len(rows), 2)

    def test_betfair_market_augments_bifa_and_trade_points(self):
        event = sample_odds_event()
        match, _rows, _euro_points, _price_points = build_match_from_odds_event(event)
        match, price_points = augment_match_with_betfair(match, sample_betfair_market())

        self.assertEqual(match.raw["BetfairMarketId"], "1.234567890")
        self.assertGreater(match.raw["BfAmountHome"], match.raw["BfAmountAway"])
        self.assertGreater(match.raw["BfIndexHome"], match.raw["BfIndexAway"])
        self.assertGreaterEqual(len(price_points["home"]), 2)
        self.assertGreaterEqual(len(price_points["away"]), 2)

    def test_selftest_full_stack(self):
        run_selftest(verbose=False)

    def test_predictor_runs_with_sample_betfair_data(self):
        event = sample_odds_event()
        market = sample_betfair_market()
        match, rows, euro_points, price_points = build_match_from_odds_event(event, betfair_market=market)

        class Client:
            def handicap_list(self, _event_id, _asian_line):
                return rows

            def handicap_detail(self, _event_id, _asian_line, _bookmaker_id):
                return []

            def euro_trend(self, _event_id):
                return euro_points

            def price_volume(self, _event_id, selection):
                return price_points[selection]

        result = Predictor(Client(), None).analyze(match)
        trade_signal = next(signal for signal in result.signals if signal.name == "必发成交走势")
        external_signal = next(signal for signal in result.signals if signal.name == "外部赔率/实力校验")

        self.assertTrue(trade_signal.available)
        self.assertTrue(external_signal.available)
        self.assertGreater(result.completeness, 80)


if __name__ == "__main__":
    unittest.main()
