import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from market_index_bureau.coverage_sensitivity import StudyStore, main
from market_index_bureau.service import run_fixture

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures/analysis/input.json"
DEMO = ROOT / "fixtures/demo.json"


def load_fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def full_response_panel():
    """把缺报改为按基期值确认（增长 0），构造全响应对照面板。"""
    data = load_fixture()
    base = {row["merchant_id"]: row["value"] for row in data["turnover"] if row["period"] == "base"}
    for row in data["turnover"]:
        if row["period"] == "current" and row["response"] == "missing":
            row["response"] = "confirmed"
            row["value"] = base[row["merchant_id"]]
    return data


class CoverageSensitivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = Path(self.tmp.name)

    def run_study(self, data, extra_args=None, db_name="study.sqlite3", out_name="out.json"):
        input_path = self.workdir / (out_name.replace(".json", "") + "-input.json")
        input_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        output_path = self.workdir / out_name
        argv = ["--input", str(input_path), "--database", str(self.workdir / db_name),
                "--output", str(output_path)] + list(extra_args or [])
        with contextlib.redirect_stdout(io.StringIO()):
            code = main(argv)
        self.assertEqual(code, 0)
        return json.loads(output_path.read_text(encoding="utf-8"))

    def envelope(self, result, name):
        for entry in result["envelopes"]:
            if entry.get("assumption_set") == name or entry.get("scope") == name:
                return entry
        self.fail(f"缺少包络条目: {name}")

    def test_full_response_bounds_collapse(self):
        result = self.run_study(full_response_panel())
        self.assertEqual(result["coverage"]["current_period"]["missing"], 0)
        self.assertEqual(result["coverage"]["current_period"]["missing_weighted_base_share"], 0.0)
        responders = self.envelope(result, "responders_only")
        for entry in result["envelopes"]:
            self.assertEqual(entry["index_lower"], entry["index_upper"], entry.get("assumption_set"))
            self.assertEqual(entry["width"], 0.0)
            self.assertEqual(entry["direction"], "growth_holds")
        for entry in result["envelopes"]:
            if entry["scope"] == "full_frame":
                self.assertEqual(entry["index_lower"], responders["index_lower"])
        for point in result["tipping_points"]:
            self.assertFalse(point["exists"])
            self.assertIsNone(point["uniform_missing_growth_at_threshold"])

    def test_nonrandom_missing_can_reverse(self):
        result = self.run_study(load_fixture())
        coverage = result["coverage"]
        self.assertFalse(coverage["response_mechanism"]["missing_assumed_random"])
        large = coverage["by_size"]["large"]["response_rate_weighted_base"]
        small = coverage["by_size"]["small"]["response_rate_weighted_base"]
        self.assertLess(large, small)
        for name in ("mild", "severe"):
            entry = self.envelope(result, name)
            self.assertEqual(entry["direction"], "reversal_possible")
            self.assertLess(entry["index_lower"], 1.0)
            self.assertGreater(entry["index_upper"], 1.0)
        severe = self.envelope(result, "severe")
        self.assertAlmostEqual(severe["index_lower"], 0.72, places=6)

    def test_unbounded_keeps_unknown(self):
        result = self.run_study(load_fixture())
        entry = self.envelope(result, "unbounded")
        self.assertIsNone(entry["index_upper"])
        self.assertIsNone(entry["width"])
        self.assertFalse(entry["bounded"])
        self.assertEqual(entry["direction"], "indeterminate")
        self.assertIsNone(entry["contains_published_reference"])
        self.assertEqual(entry["index_lower"], 0.72)

    def test_replay_returns_same_study(self):
        data = load_fixture()
        first = self.run_study(data, out_name="first.json")
        replay = self.run_study(data, out_name="replay.json")
        self.assertEqual(first["study_id"], replay["study_id"])
        self.assertFalse(first["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["created_at"], replay["created_at"])
        self.assertEqual(StudyStore(self.workdir / "study.sqlite3").count(), 1)

    def test_assumption_variants_never_overwrite(self):
        data = load_fixture()
        first = self.run_study(data, out_name="base.json")
        variant = self.run_study(data, extra_args=["--missing-growth-min", "-0.5", "--missing-growth-max", "-0.1"],
                                 out_name="variant.json")
        self.assertNotEqual(first["study_id"], variant["study_id"])
        store = StudyStore(self.workdir / "study.sqlite3")
        self.assertEqual(store.count(), 2)
        replay = self.run_study(data, out_name="again.json")
        self.assertEqual(replay["study_id"], first["study_id"])
        self.assertEqual(replay["created_at"], first["created_at"])
        self.assertEqual(store.count(), 2)
        vintages = {first["index_reference"]["vintage"], variant["index_reference"]["vintage"]}
        self.assertEqual(vintages, {"published-r1"})

    def test_tipping_point_values(self):
        result = self.run_study(load_fixture())
        points = {p["threshold_kind"]: p for p in result["tipping_points"]}
        boundary = points["growth_boundary"]
        self.assertTrue(boundary["exists"])
        self.assertTrue(boundary["feasible"])
        self.assertAlmostEqual(boundary["uniform_missing_growth_at_threshold"], -0.136271, places=6)
        reference = points["published_reference"]
        self.assertAlmostEqual(reference["uniform_missing_growth_at_threshold"], 0.110508, places=6)

    def test_composition_split_not_mixed(self):
        result = self.run_study(load_fixture())
        comp = result["composition"]
        self.assertLess(comp["composition_effect"], 0.0)
        self.assertGreater(comp["matched_transaction_effect"], 0.0)
        naive = comp["naive_change_mixing_structure"]
        rebuilt = (1 + comp["composition_effect"]) * (1 + comp["matched_transaction_effect"]) - 1
        self.assertAlmostEqual(naive, rebuilt, places=6)
        self.assertLess(naive, comp["matched_transaction_effect"])
        strata = {(s["sector"], s["size"]): s for s in comp["strata"]}
        self.assertIn(("leather", "large"), strata)
        self.assertAlmostEqual(strata[("leather", "large")]["matched_growth_ratio"], 1.12, places=6)

    def test_confirmed_zero_and_missing_are_distinct(self):
        result = self.run_study(load_fixture())
        current = result["coverage"]["current_period"]
        self.assertEqual(current["confirmed_zero"], 3)
        self.assertEqual(current["missing"], 11)
        self.assertEqual(result["distribution"]["current_observed"]["count"], 33)
        self.assertEqual(result["distribution"]["current_observed"]["min"], 0.0)
        self.assertEqual(result["coverage"]["closure"]["closed_missing"], 1)

    def test_late_records_not_used_before_available(self):
        data = load_fixture()
        late = dict(data["turnover"][1])
        late["value"] = 500
        late["response"] = "confirmed"
        late["available_at"] = "2026-11-01T00:00:00+00:00"
        data["turnover"].append(late)
        result = self.run_study(data)
        self.assertEqual(result["coverage"]["late_records_excluded"], 1)
        self.assertEqual(result["coverage"]["current_period"]["missing"], 11)
        baseline = self.run_study(load_fixture(), db_name="baseline.sqlite3")
        self.assertEqual(result["envelopes"], baseline["envelopes"])

    def test_superseded_records_latest_available_wins(self):
        data = load_fixture()
        stale = dict(data["turnover"][3])
        stale["value"] = 999
        stale["available_at"] = "2026-10-01T00:00:00+00:00"
        data["turnover"].append(stale)
        result = self.run_study(data)
        self.assertEqual(result["coverage"]["superseded_records"], 1)
        baseline = self.run_study(load_fixture(), db_name="baseline.sqlite3")
        self.assertEqual(result["envelopes"], baseline["envelopes"])

    def test_vintage_linkage_to_published_run(self):
        run_fixture(DEMO, self.workdir / "linked.sqlite3")
        result = self.run_study(load_fixture(), db_name="linked.sqlite3")
        linkage = result["vintage_linkage"]
        self.assertTrue(linkage["linked"])
        self.assertEqual(linkage["baseline_case_id"], "market_index_bureau-demo")
        self.assertTrue(linkage["run_id"])
        unlinked = self.run_study(load_fixture(), db_name="fresh.sqlite3")
        self.assertFalse(unlinked["vintage_linkage"]["linked"])

    def test_operator_range_adjustable(self):
        result = self.run_study(load_fixture(),
                                extra_args=["--missing-growth-min", "-0.5", "--missing-growth-max", "-0.1"])
        entry = self.envelope(result, "operator")
        self.assertEqual(entry["missing_growth_range"], [-0.5, -0.1])
        self.assertAlmostEqual(entry["index_lower"], 0.882088, places=6)
        self.assertAlmostEqual(entry["index_upper"], 1.011758, places=6)
        self.assertEqual(entry["direction"], "reversal_possible")
        names = {item["name"] for item in result["assumptions"]["sets"]}
        self.assertIn("operator", names)

    def test_missing_base_value_rejected(self):
        data = load_fixture()
        for row in data["turnover"]:
            if row["merchant_id"] == "merchant-1" and row["period"] == "base":
                row["value"] = None
                row["response"] = "missing"
        input_path = self.workdir / "broken.json"
        input_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        argv = ["--input", str(input_path), "--database", str(self.workdir / "broken.sqlite3"),
                "--output", str(self.workdir / "broken-out.json")]
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            code = main(argv)
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
