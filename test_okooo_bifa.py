"""Tests for okooo_bifa HTML parsing and Titan007 merge helpers (no network)."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from okooo_bifa import (
    best_okooo_bifa_match,
    fetch_okooo_exchanges_detail_series,
    load_titan_to_okooo_id_map,
    merge_okooo_bifa_into_raw,
    parse_okooo_betfa_html,
    resolve_okooo_bifa_match,
)
from unittest.mock import patch

from titan007_client import _parse_bf_match_time
from worldcup_ah_cli import DataError


class OkoooAhCliBetfaParseTests(unittest.TestCase):
    """okooo_ah_cli.parse_betfa_html：已赛比分行（无 VS）与未赛行兼容。"""

    def test_parse_betfa_finished_match_without_vs(self) -> None:
        from okooo_ah_cli import parse_betfa_html

        html = """
        <div class="clearfix container_wrapper betfa">
          <div class="magazineDateTit font_14">
            <p class="float_l"><b>周一013</b><b>世界杯</b><b>06-16 00:00</b></p>
            <div class="titnamebox titname_box">
              <span>西班牙</span><em class="font_red">(-2)</em><strong>0-0</strong><b>佛得角</b>
            </div>
          </div>
          <a href="/soccer/match/1315999/odds/">x</a>
          <table>
            <tr><td>主胜</td><td></td><td></td><td></td><td></td><td>100</td><td>1</td><td>2</td><td>1.1</td><td>10%</td><td>2</td><td>20%</td><td></td><td>0</td></tr>
            <tr><td>平局</td><td></td><td></td><td></td><td></td><td>100</td><td>1</td><td>2</td><td>3</td><td>10%</td><td>2</td><td>20%</td><td></td><td>0</td></tr>
            <tr><td>客胜</td><td></td><td></td><td></td><td></td><td>100</td><td>1</td><td>2</td><td>5</td><td>10%</td><td>2</td><td>20%</td><td></td><td>0</td></tr>
          </table>
        </div>
        """
        out = parse_betfa_html(html)
        self.assertIn(1315999, out)
        m = out[1315999]
        self.assertEqual(m.home, "西班牙")
        self.assertEqual(m.away, "佛得角")
        self.assertEqual(m.lottery_handicap, "-2")


class OkoooBifaTests(unittest.TestCase):
    def test_parse_fixture_snippet(self) -> None:
        html = Path("fixtures/okooo_betfa_snippet.html").read_text(encoding="utf-8")
        rows = parse_okooo_betfa_html(html)
        self.assertEqual(len(rows), 1)
        m = rows[0]
        self.assertEqual(m.okooo_id, 8811001)
        self.assertEqual(m.home, "IR雷克雅未克")
        self.assertEqual(m.away, "格洛塔")
        self.assertEqual(m.kickoff_md, "06-17 22:00")
        self.assertAlmostEqual(m.home_sel.amount, 12345.0)
        self.assertAlmostEqual(m.home_sel.ratio_pct, 45.5)
        self.assertAlmostEqual(m.home_sel.payout, 999.0)

    def test_best_match_and_merge(self) -> None:
        html = Path("fixtures/okooo_betfa_snippet.html").read_text(encoding="utf-8")
        rows = parse_okooo_betfa_html(html)
        mt = _parse_bf_match_time("2026,5,17,22,00,00")
        bk = best_okooo_bifa_match(
            "IR雷克雅未克",
            "格洛塔",
            mt,
            rows,
            schedule_tz=ZoneInfo("Asia/Shanghai"),
        )
        self.assertIsNotNone(bk)
        assert bk is not None
        pick, swapped = bk
        self.assertFalse(swapped)
        raw: dict = {}
        merge_okooo_bifa_into_raw(raw, pick, swapped=swapped)
        self.assertAlmostEqual(raw["BfAmountHome"], 12345.0)
        self.assertAlmostEqual(raw["BfAmountDraw"], 23456.0)
        self.assertAlmostEqual(raw["BfAmountAway"], 3456.0)
        self.assertAlmostEqual(raw["BfPayoutHome"], 999.0)
        self.assertAlmostEqual(raw["BfOddsHome"], 1.90)
        self.assertAlmostEqual(raw["BfIndexHome"], 45.5)
        self.assertEqual(raw["_okooo_match_id"], 8811001)

    def test_swapped_when_ok_columns_reversed(self) -> None:
        html = """
        <div class="container_wrapper betfa">
        <a href="/soccer/match/8811999/odds/">x</a>
        <span>格洛塔</span><strong>VS</strong><b>IR雷克雅未克</b>
        <div class="magazineDateTit"><p><b>x</b><b>y</b><b>06-17 22:00</b></p></div>
        <table>
        <tr><td>主胜</td><td>0</td><td>0</td><td>0</td><td>0</td><td>111</td><td>1</td><td>2</td><td>1.1</td><td>10%</td><td></td><td></td><td></td><td>1</td></tr>
        <tr><td>平局</td><td>0</td><td>0</td><td>0</td><td>0</td><td>222</td><td>1</td><td>2</td><td>2.2</td><td>20%</td><td></td><td></td><td></td><td>2</td></tr>
        <tr><td>客胜</td><td>0</td><td>0</td><td>0</td><td>0</td><td>88888</td><td>1</td><td>2</td><td>3.3</td><td>70%</td><td></td><td></td><td></td><td>3</td></tr>
        </table></div>
        """
        rows = parse_okooo_betfa_html(html)
        mt = _parse_bf_match_time("2026,5,17,22,00,00")
        bk = best_okooo_bifa_match(
            "IR雷克雅未克",
            "格洛塔",
            mt,
            rows,
            schedule_tz=ZoneInfo("Asia/Shanghai"),
        )
        self.assertIsNotNone(bk)
        assert bk is not None
        pick, swapped = bk
        self.assertTrue(swapped)
        raw: dict = {}
        merge_okooo_bifa_into_raw(raw, pick, swapped=swapped)
        self.assertAlmostEqual(raw["BfAmountHome"], 88888.0)

    def test_load_map_from_env_ids(self) -> None:
        with patch.dict(
            os.environ,
            {"TITAN007_OKOOO_IDS": "2906745=1316319,2:3", "OKOOO_TITAN_MAP_PATH": ""},
            clear=False,
        ):
            m = load_titan_to_okooo_id_map()
        self.assertEqual(m[2906745], 1316319)
        self.assertEqual(m[2], 3)

    def test_resolve_prefers_id_map(self) -> None:
        html = Path("fixtures/okooo_betfa_snippet.html").read_text(encoding="utf-8")
        rows = parse_okooo_betfa_html(html)
        # Wrong kickoff time so heuristic would skip; id map still picks row
        mt = _parse_bf_match_time("2026,0,1,0,0,0")
        res = resolve_okooo_bifa_match(
            2931221,
            "IR雷克雅未克",
            "格洛塔",
            mt,
            rows,
            schedule_tz=ZoneInfo("Asia/Shanghai"),
            titan_to_okooo={2931221: 8811001},
        )
        self.assertIsNotNone(res)
        assert res is not None
        pick, swapped, src = res
        self.assertEqual(src, "id_map")
        self.assertEqual(pick.okooo_id, 8811001)
        self.assertFalse(swapped)

    def test_resolve_id_map_miss(self) -> None:
        html = Path("fixtures/okooo_betfa_snippet.html").read_text(encoding="utf-8")
        rows = parse_okooo_betfa_html(html)
        mt = _parse_bf_match_time("2026,0,1,0,0,0")
        res = resolve_okooo_bifa_match(
            1,
            "x",
            "y",
            mt,
            rows,
            schedule_tz=ZoneInfo("Asia/Shanghai"),
            titan_to_okooo={1: 999999999},
        )
        self.assertIsNone(res)

    def test_fetch_exchanges_detail_requires_cookie(self) -> None:
        with self.assertRaises(DataError):
            fetch_okooo_exchanges_detail_series(1317856, timeout=5.0, cookie="  ")


if __name__ == "__main__":
    unittest.main()
