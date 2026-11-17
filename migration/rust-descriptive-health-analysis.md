# Descriptive daily health analysis

Rust reads one allowed numeric daily metric at a time from the existing
projection. The date window is inclusive and capped at 3,661 days. The response
retains observed dates, missing days, finite-value denominators, sample
standard deviation, effect size and comparison limitations.

The legacy comparison uses 1,000 circular seven-observation block
resamples and NumPy 2.5.3's seeded PCG64 stream. Rust reproduces the seed-42
stream and uses one reusable observation buffer plus the 1,000 differences,
without a matrix of resampled observations. Both periods are read in the
same read-only database snapshot.

- [NumPy 2.5 PCG64](https://numpy.org/doc/2.5/reference/random/bit_generators/pcg64.html)
- [NumPy 2.5.3 bounded integer generation](https://github.com/numpy/numpy/blob/v2.5.3/numpy/random/src/distributions/distributions.c)

The native Linux ARM64 fixture `check_health_analysis.py` compares 52
public results against pinned Python and NumPy on disposable TimescaleDB.
The cases cover the 24 available metrics, empty and sparse periods,
non-finite observations, variable and constant series, calendar boundaries,
two maximum-length periods, and the bootstrap sample threshold. Numeric
fields use a 1e-12 relative tolerance with a 1e-12 absolute floor.
