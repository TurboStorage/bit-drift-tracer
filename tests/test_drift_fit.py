import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import drift_fit as df  # noqa: E402


class LogAgeX(unittest.TestCase):
    def test_zero_age_maps_to_zero(self):
        self.assertEqual(df.log_age_x(np.array([0.0]))[0], 0.0)

    def test_nine_days_is_one_decade(self):
        self.assertAlmostEqual(df.log_age_x(np.array([9.0]))[0], 1.0)

    def test_negative_age_clamped(self):
        self.assertEqual(df.log_age_x(np.array([-5.0]))[0], 0.0)


class LinearFit(unittest.TestCase):
    def test_recovers_known_line(self):
        wk = np.linspace(0, 200, 50)
        r = df.fit_linear_age(wk, 500 - 0.5 * wk)
        self.assertAlmostEqual(r.slope, -0.5)
        self.assertAlmostEqual(r.intercept, 500.0)
        self.assertAlmostEqual(r.r2, 1.0)
        self.assertEqual(r.n, 50)

    def test_too_few_points_returns_none(self):
        self.assertIsNone(df.fit_linear_age(np.arange(3.0), np.arange(3.0)))


class LogFit(unittest.TestCase):
    def test_recovers_known_log_curve(self):
        days = np.array([1, 3, 9, 30, 99, 365, 999, 1460], dtype=float)
        speeds = 800 - 40 * np.log10(1 + days)
        r = df.fit_log_age(days, speeds)
        self.assertAlmostEqual(r.slope, -40.0)
        self.assertAlmostEqual(r.intercept, 800.0)
        self.assertAlmostEqual(r.r2, 1.0)

    def test_predict_matches_curve(self):
        days = np.array([1, 10, 100, 1000, 2000, 3000], dtype=float)
        r = df.fit_log_age(days, 800 - 40 * np.log10(1 + days))
        self.assertAlmostEqual(float(r.predict(np.array([99.0]))[0]), 800 - 80.0)

    def test_log_shape_beats_linear_fit(self):
        days = np.geomspace(1, 3000, 80)
        speeds = 900 - 60 * np.log10(1 + days)
        lin = df.fit_linear_age(days / 7.0, speeds)
        log = df.fit_log_age(days, speeds)
        self.assertGreater(log.r2, lin.r2)

    def test_flat_data_has_zero_slope_and_zero_r2(self):
        days = np.linspace(1, 1000, 20)
        r = df.fit_log_age(days, np.full(20, 500.0))
        self.assertAlmostEqual(r.slope, 0.0)
        self.assertEqual(r.r2, 0.0)

    def test_identical_ages_returns_none(self):
        self.assertIsNone(df.fit_log_age(np.full(10, 30.0), np.arange(10.0)))


class Cleaning(unittest.TestCase):
    def test_nonfinite_and_outliers_dropped(self):
        days = np.linspace(1, 100, 40)
        speeds = np.full(40, 500.0)
        speeds[5] = np.nan
        speeds[10] = 50_000.0
        mask = df.fit_mask(days, speeds)
        self.assertFalse(mask[5])
        self.assertFalse(mask[10])
        self.assertEqual(int(mask.sum()), 38)

    def test_constant_speeds_all_kept(self):
        self.assertTrue(df.fit_mask(np.arange(10.0), np.full(10, 3.0)).all())


class Note(unittest.TestCase):
    def test_note_is_plain_ascii_without_em_dash(self):
        self.assertNotIn("—", df.CONFOUND_NOTE)
        self.assertIn("dwell", df.CONFOUND_NOTE.lower())
        self.assertIn("temperature", df.CONFOUND_NOTE.lower())

    def test_source_text_cites_primary_only(self):
        self.assertIn("HeatWatch", df.SOURCE_NOTE)
        self.assertIn("HPCA 2018", df.SOURCE_NOTE)


class NoBritishSpelling(unittest.TestCase):
    def test_module_and_readme_use_american_spelling(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        for name in ("drift_fit.py", "README.md", "CHANGELOG.md"):
            path = os.path.join(root, name)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self.assertNotIn("colour", f.read().lower(), name)


if __name__ == "__main__":
    unittest.main()
