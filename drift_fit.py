"""Trend fits for Bit Drift Tracer: linear in age (weeks) and linear in log age.

Pure numpy, no GUI imports, so the fits can be tested headlessly.

Why a log-age fit: in published 3D NAND retention measurements the threshold
voltage shift is linear in the logarithm of retention time (Luo et al.,
HeatWatch, HPCA 2018, Equation 2), so loss is fast early and slow later. A
straight line in weeks is the wrong shape for that curve and is dominated by
the oldest files. The tool still cannot see charge, so the fit describes host
read speed against file age only.
"""

from typing import NamedTuple, Optional

import numpy as np

MIN_POINTS = 4          # fewest points a fit will accept
OUTLIER_SIGMA = 3.0     # speed outliers beyond this many sigma are not fitted

CONFOUND_NOTE = (
    "File age is not retention time. Dwell time before the data was last "
    "rewritten and the temperature history of the drive also change how fast "
    "charge is lost, and file timestamps can be copied or reset."
)

SOURCE_NOTE = (
    "Log-age shape: threshold-voltage shift is linear in the log of retention "
    "time for 3D NAND (Luo et al., HeatWatch, HPCA 2018, Eq. 2)."
)


class FitResult(NamedTuple):
    slope: float        # MB/s per week (linear) or MB/s per decade of age (log)
    intercept: float
    r2: float
    n: int
    log_x: bool

    def predict(self, age: np.ndarray) -> np.ndarray:
        """Fitted MB/s. `age` is weeks for a linear fit, days for a log fit."""
        x = log_age_x(age) if self.log_x else np.asarray(age, dtype=float)
        return self.slope * x + self.intercept


def log_age_x(age_days: np.ndarray) -> np.ndarray:
    """log10(1 + age in days); keeps zero-day files finite and at zero."""
    return np.log10(1.0 + np.clip(np.asarray(age_days, dtype=float), 0.0, None))


def fit_mask(age: np.ndarray, speeds: np.ndarray) -> np.ndarray:
    """True for points that are finite and not a speed outlier."""
    age = np.asarray(age, dtype=float)
    speeds = np.asarray(speeds, dtype=float)
    keep = np.isfinite(age) & np.isfinite(speeds)
    if keep.any():
        sd = speeds[keep]
        std = sd.std()
        if std > 0:
            z = np.abs((sd - sd.mean()) / std)
            keep[keep] = z < OUTLIER_SIGMA
    return keep


def _fit(x: np.ndarray, y: np.ndarray, log_x: bool) -> Optional[FitResult]:
    if len(x) < MIN_POINTS or np.ptp(x) == 0:
        return None
    slope, intercept = np.polyfit(x, y, 1)
    resid = y - (slope * x + intercept)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 0.0 if ss_tot == 0 else 1.0 - float((resid ** 2).sum()) / ss_tot
    return FitResult(float(slope), float(intercept), r2, int(len(x)), log_x)


def fit_linear_age(age_weeks: np.ndarray, speeds: np.ndarray) -> Optional[FitResult]:
    """Least-squares line of speed against age in weeks (MB/s per week)."""
    keep = fit_mask(age_weeks, speeds)
    return _fit(np.asarray(age_weeks, dtype=float)[keep],
                np.asarray(speeds, dtype=float)[keep], False)


def fit_log_age(age_days: np.ndarray, speeds: np.ndarray) -> Optional[FitResult]:
    """Least-squares line of speed against log10(1 + age in days).

    The slope is MB/s per tenfold increase in age (per decade).
    """
    keep = fit_mask(age_days, speeds)
    return _fit(log_age_x(np.asarray(age_days, dtype=float)[keep]),
                np.asarray(speeds, dtype=float)[keep], True)
