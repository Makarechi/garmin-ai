# Analysis methods and limits

All personal numbers come from PostgreSQL tools. Gemini may select tools and explain their
results; it may not invent missing values or compute unsupported statistics in prose.
Daily dates use the configured timezone. Intraday timestamps retain UTC instants, including DST.

| Analysis | Method | Evidence threshold and limits |
|---|---|---|
| Personal baseline | Observed daily mean, median, sample SD and range | Always reports observed and missing days; one observation has no sample SD |
| Period comparison | Difference of means; sample-size-weighted pooled standardized difference | Both variances required for standardized effect; 14 observations per group for CI |
| Comparison CI | 1,000 circular seven-observation block bootstrap resamples, seed 42 | Blocks follow observations, not gaps in calendar time; missing days can bias estimates |
| Running efficiency | Distance / moving duration × 60 / average HR | Minimum 20 minutes; caller HR range; terrain/type and prior sleep/HRV/readiness shown |
| Event windows | Source-separated, time-weighted gauge means and summed increments in −48 to −24, −24 to −12, −12 to −6, −6 to 0, and 0 to +24 hours | At most 100 episodes and 366 days; gauge means require 80% coverage using bounded left-hold intervals; gaps remain missing and correlated samples do not establish independent evidence |
| Migraine comparison | Maximum number of same-weekday matches within 56 days, then minimum total distance; deterministic ordering | Controls exclude ±3 days around logged migraine starts; at least 10 pairs for uncertainty |
| Migraine uncertainty | Paired bootstrap and exploratory sign permutation, 2,000 resamples, seed 42 | Serial dependence, incomplete logging and unmeasured confounding remain |
| Lagged associations | Calendar-day matched Pearson correlations | At least 10 pairs, nonconstant values, at most 15 lags within ±30 days; exploratory |
| Proactive trends | Two complete 14-day windows | 14 observations each, absolute standardized effect ≥0.5 and CI excluding zero |

These analyses establish observational associations, not causes or treatment effects. Multiple
comparisons are not adjusted. Sleep, medication, training, alcohol, illness, weather and other
factors are not jointly controlled by the current estimators. Activity rankings are not
weather-, grade- or steady-state-adjusted. No diagnostic or prescribing system is implemented.

Timeline evidence prioritizes recorded activities, confirmed diary intervals and recorded sleep.
Unclassified intervals remain unknown. Candidate inferences are labelled explicitly; heart rate
alone never establishes that a person was driving, in a meeting, or working.

Short histories cannot support statistically credible personal pattern claims.
The application retains candidates and waits for sufficient observations instead of notifying
unsupported trends. API or provider failure is reported separately from absence of evidence.
