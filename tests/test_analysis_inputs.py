import json
import unittest
from pathlib import Path


class AnalysisInputsTests(unittest.TestCase):
    def load_data(self):
        return json.loads((Path(__file__).resolve().parents[1] / "fixtures/analysis/input.json").read_text())

    def test_missing_not_zero(self):
        data = self.load_data()
        self.assertTrue(any(row['value'] is None for row in data['turnover']) and any(row['value'] == 0 for row in data['turnover']))

    def test_unbounded_assumption(self):
        data = self.load_data()
        self.assertTrue(any(row['missing_growth_max'] is None for row in data['assumption_sets']))

    def test_large_merchants(self):
        data = self.load_data()
        self.assertTrue(any(row['size'] == 'large' for row in data['frame']))

    def test_source_versions_and_cutoff(self):
        data = self.load_data()
        self.assertEqual(data["schema_version"], 1)
        self.assertTrue(data["source_versions"])
        self.assertIn("+00:00", data["as_of"])
