# Changelog

## 1.1.0

- Added a "Log age axis and fit" option. The scatter switches to a log age axis (1 week to 8 years) and the trend line becomes a least-squares fit of read speed against log10(1 + age in days), with the slope in MB/s per decade of age.
- R-squared for both the linear-in-weeks fit and the log-age fit is shown under the controls, and in the legend for the active fit.
- Added a note that file age is not retention time: dwell time and temperature history also change loss rate.
- Basis for the log shape: 3D NAND threshold-voltage shift is linear in the log of retention time (Luo et al., HeatWatch, HPCA 2018, Equation 2).
- Fit code moved to `drift_fit.py`, with unit tests in `tests/` (`python -m unittest discover tests`).

## 1.0

- Initial release.
