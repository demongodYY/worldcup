"""Tests for Titan007 feed parsers and CLI (no network by default)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import patch

from titan007_ah_cli import _upcoming_league_ok
from titan007_client import (
    LIVE_BFDATA_UT,
    Titan007Client,
    _parse_bf_match_time,
    adjust_titan007_asian_line_sign,
    finalize_titan007_asian_line_sign_from_ml,
    coerce_asian_triplet,
    euro_rows_from_1x2,
    first_plausible_handicap_from_sb,
    parse_1x2_js,
    parse_bf_jc_rows,
    parse_bfdata_rows,
    parse_ch_goalbf3_asian,
    parse_ch_goalbf3_map,
    parse_sb_odds_map,
    synthetic_price_volume_from_snapshots,
)
from worldcup_ah_cli import SnapshotStore


class Titan007ParserTests(unittest.TestCase):
    def test_adjust_asian_sign_home_favorite_gives(self) -> None:
        self.assertEqual(adjust_titan007_asian_line_sign("1.0", 0.88, 0.98), "-1")
        self.assertEqual(adjust_titan007_asian_line_sign("1", 0.83, 0.97), "-1")

    def test_adjust_asian_sign_away_favorite_keeps_positive(self) -> None:
        self.assertEqual(adjust_titan007_asian_line_sign("0.5", 1.05, 0.82), "0.5")

    def test_adjust_preserves_negative(self) -> None:
        self.assertEqual(adjust_titan007_asian_line_sign("-1", 0.5, 1.2), "-1")

    def test_adjust_asian_sign_small_water_gap_noop(self) -> None:
        self.assertEqual(adjust_titan007_asian_line_sign("1", 0.91, 0.92), "1")

    def test_finalize_asian_sign_ml_home_fav_flips_positive(self) -> None:
        raw: dict = {"AsianAvrLet": "1", "EuroAvrHome": 1.55, "EuroAvrAway": 5.8}
        finalize_titan007_asian_line_sign_from_ml(raw)
        self.assertEqual(raw["AsianAvrLet"], "-1")
        self.assertTrue(raw.get("_titan007_asian_line_signed_by_ml"))

    def test_finalize_asian_sign_ml_uses_bf_when_no_euro_gap(self) -> None:
        raw: dict = {"AsianAvrLet": "1", "EuroAvrHome": 2.1, "EuroAvrAway": 2.15, "BfOddsHome": 1.7, "BfOddsAway": 5.2}
        finalize_titan007_asian_line_sign_from_ml(raw)
        self.assertEqual(raw["AsianAvrLet"], "-1")

    def test_finalize_asian_sign_ml_ambiguous_noop(self) -> None:
        raw: dict = {"AsianAvrLet": "1", "EuroAvrHome": 2.1, "EuroAvrAway": 2.12}
        finalize_titan007_asian_line_sign_from_ml(raw)
        self.assertEqual(raw["AsianAvrLet"], "1")
        self.assertIsNone(raw.get("_titan007_asian_line_signed_by_ml"))

    def test_finalize_asian_sign_ml_away_fav_flips_negative(self) -> None:
        raw: dict = {"AsianAvrLet": "-1", "EuroAvrHome": 6.0, "EuroAvrAway": 1.5}
        finalize_titan007_asian_line_sign_from_ml(raw)
        self.assertEqual(raw["AsianAvrLet"], "1")

    def test_upcoming_league_world_cup(self) -> None:
        self.assertTrue(_upcoming_league_ok("世界杯", "", True))
        self.assertTrue(_upcoming_league_ok("世界盃", "", True))
        self.assertTrue(_upcoming_league_ok("FIFA World Cup", "", True))
        self.assertFalse(_upcoming_league_ok("英超", "", True))
        self.assertTrue(_upcoming_league_ok("世界杯小组赛", "小组", True))
        self.assertFalse(_upcoming_league_ok("世界杯", "小组", True))

    def test_upcoming_league_top_euro_filters(self) -> None:
        keys = ("premier_league", "la_liga", "bundesliga")
        self.assertTrue(_upcoming_league_ok("英超", "", False, keys))
        self.assertTrue(_upcoming_league_ok("西甲", "", False, keys))
        self.assertTrue(_upcoming_league_ok("德甲", "", False, keys))
        self.assertTrue(_upcoming_league_ok("English Premier League", "", False, keys))
        self.assertTrue(_upcoming_league_ok("Spanish La Liga", "", False, keys))
        self.assertTrue(_upcoming_league_ok("German Bundesliga", "", False, keys))
        self.assertFalse(_upcoming_league_ok("意甲", "", False, keys))
        self.assertFalse(_upcoming_league_ok("英超", "西", False, keys))

    def test_bf_match_time_js_month(self) -> None:
        """Titan007 uses JavaScript month (5 = June), not ISO 1–12."""
        from zoneinfo import ZoneInfo

        dt = _parse_bf_match_time("2026,5,17,22,00,00")
        loc = dt.astimezone(ZoneInfo("Asia/Shanghai"))
        self.assertEqual(loc.year, 2026)
        self.assertEqual(loc.month, 6)
        self.assertEqual(loc.day, 17)
        self.assertEqual(loc.hour, 22)

    def test_coerce_line_first(self) -> None:
        self.assertEqual(coerce_asian_triplet(0.5, 0.84, 0.85), (0.5, 0.84, 0.85))

    def test_coerce_home_first(self) -> None:
        self.assertEqual(coerce_asian_triplet(0.84, 0.5, 0.85), (0.5, 0.84, 0.85))

    def test_parse_ch_goalbf3(self) -> None:
        xml = Path("fixtures/titan007_ch_goalbf_snippet.xml").read_text(encoding="utf-8")
        m = parse_ch_goalbf3_map(xml)[2931221]
        self.assertEqual(parse_ch_goalbf3_asian(m), (0.25, 0.84, 0.85))

    def test_sb_odds(self) -> None:
        js = Path("fixtures/titan007_sb_odds_snippet.js").read_text(encoding="utf-8")
        mp = parse_sb_odds_map(js)
        self.assertIn(2931221, mp)
        self.assertEqual(first_plausible_handicap_from_sb(mp[2931221]), (0.5, 0.84, 0.85))

    def test_parse_1x2(self) -> None:
        js = Path("fixtures/titan007_1x2_snippet.js").read_text(encoding="utf-8")
        rows, hc, gc, _mt = parse_1x2_js(js)
        self.assertEqual(hc, "主队测")
        self.assertEqual(gc, "客队测")
        pts = euro_rows_from_1x2(rows)
        self.assertGreaterEqual(len(pts), 2)
        self.assertAlmostEqual(pts[0].home_price, 2.1)

    def test_synthetic_price_volume(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            store = SnapshotStore(td)
            mid = 999001
            base = {
                "schema": 1,
                "fetched_at": "2026-01-01T10:00:00+00:00",
                "match": {
                    "event_id": mid,
                    "match_time": "2026-01-01T12:00:00+00:00",
                    "home": "H",
                    "away": "A",
                    "league_id": 1,
                    "league_name": "L",
                    "asian_line": "0.5",
                    "is_stop_update": False,
                    "raw": {
                        "EuroAvrHome": 2.0,
                        "EuroAvrAway": 3.0,
                        "BfOddsHome": 2.1,
                        "BfOddsAway": 3.1,
                    },
                },
                "result": {"score": 0.01, "upper_team": "H", "lower_team": "A", "signals": []},
            }
            rec2 = json.loads(json.dumps(base))
            rec2["fetched_at"] = "2026-01-01T11:00:00+00:00"
            rec2["match"]["raw"]["EuroAvrHome"] = 2.05
            rec2["match"]["raw"]["BfOddsHome"] = 2.15
            p = store.event_path(mid)
            p.write_text(json.dumps(base) + "\n" + json.dumps(rec2) + "\n", encoding="utf-8")
            pts = synthetic_price_volume_from_snapshots(store, mid, "home")
            self.assertGreaterEqual(len(pts), 2)
            self.assertAlmostEqual(pts[0].price, 2.1)

    def test_bfdata_fixture(self) -> None:
        js = Path("fixtures/titan007_bfdata_snippet.js").read_text(encoding="utf-8")
        rows = parse_bfdata_rows(js)
        self.assertIn(2931221, rows)
        self.assertEqual(rows[2931221]["home_en"], "IR Reykjavik")
        self.assertEqual(rows[2931221]["home_cn"], "IR雷克雅未克")
        self.assertEqual(rows[2931221]["away_cn"], "格洛塔")

    def test_bf_jc_fixture(self) -> None:
        raw = Path("fixtures/titan007_bf_jc_snippet.txt").read_text(encoding="utf-8")
        rows = parse_bf_jc_rows(raw)
        self.assertIn(2906748, rows)
        self.assertEqual(rows[2906748]["league_name"], "世界杯")
        self.assertIn("葡萄牙", rows[2906748]["home_cn"])
        self.assertEqual(rows[2906748]["match_time_str"], "2026,5,18,01,00,00")

    def test_build_match_skips_1x2_when_disabled(self) -> None:
        def spy(url: str, referer: str, timeout: float, cookie: str | None) -> str:
            if "1x2d" in url:
                raise AssertionError("1x2 should not be fetched when fetch_1x2=False")
            return ""

        from tempfile import TemporaryDirectory

        parts = ["1"] * 50
        parts[0] = "999"
        parts[2] = "L"
        parts[5] = "h"
        parts[6] = "h2"
        parts[7] = "H"
        parts[8] = "a"
        parts[9] = "a2"
        parts[10] = "A"
        parts[12] = "2026,6,20,12,0,0"
        parts[29] = "0.5"
        parts[45] = "381"

        with TemporaryDirectory() as td:
            with patch("titan007_client._http_text", side_effect=spy):
                with patch("titan007_client._bf_js_text", return_value=""):
                    c = Titan007Client(timeout=5.0, snapshot_store=SnapshotStore(td))
                    c._bf_rows = {
                        999: {
                            "parts": parts,
                            "league_name": "L",
                            "home_cn": "h",
                            "away_cn": "a",
                            "home_en": "H",
                            "away_en": "A",
                            "match_time_str": parts[12],
                            "asian_line_hint": "0.5",
                            "league_id": 381,
                        }
                    }
                    c._goal_map = {}
                    c._sb_map = {}
                    m = c.build_match(999, fetch_1x2=False)
                    self.assertEqual(m.event_id, 999)
                    self.assertEqual(m.raw.get("EuroAvrHome", 0), 0)

    def test_client_offline_build_merges_okooo_bifa(self) -> None:
        root = Path(__file__).resolve().parent
        bf = (root / "fixtures/titan007_bfdata_snippet.js").read_text(encoding="utf-8")
        gx = (root / "fixtures/titan007_ch_goalbf_snippet.xml").read_text(encoding="utf-8")
        sb = (root / "fixtures/titan007_sb_odds_snippet.js").read_text(encoding="utf-8")
        ox = (root / "fixtures/titan007_1x2_snippet.js").read_text(encoding="utf-8")
        okhtml = (root / "fixtures/okooo_betfa_snippet.html").read_text(encoding="utf-8")

        def fake_http(url: str, referer: str, timeout: float, cookie: str | None) -> str:
            if "ch_goalbf3" in url:
                return gx
            if "sbOddsData" in url:
                return sb
            if "1x2d" in url and "2931221" in url:
                return ox
            raise AssertionError(url)

        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            with patch("titan007_client.fetch_okooo_betfa_html", return_value=okhtml):
                with patch("titan007_client._http_text", side_effect=fake_http):
                    with patch("titan007_client._bf_js_text", return_value=bf):
                        c = Titan007Client(timeout=5.0, snapshot_store=SnapshotStore(td), okooo_bifa=True)
                        c.refresh_feeds()
                        m = c.build_match(2931221)
                        self.assertEqual(m.event_id, 2931221)
                        self.assertAlmostEqual(m.raw.get("BfAmountHome", 0), 12345.0)
                        self.assertFalse(m.raw.get("_okooo_bifa_swapped", True))

    def test_refresh_live_uses_bfdata_ut_url(self) -> None:
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as td:
            with patch("titan007_client._http_text", return_value=""):
                with patch("titan007_client._bf_js_text", return_value="var A=Array(1);") as bf_mock:
                    c = Titan007Client(timeout=5.0, snapshot_store=SnapshotStore(td), schedule_source="live")
                    c.refresh_feeds()
                    bf_mock.assert_called()
                    self.assertIn(LIVE_BFDATA_UT, bf_mock.call_args[0][2])


if __name__ == "__main__":
    unittest.main()
