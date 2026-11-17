# Rust migration – observation freshness

Native observation checks measure observed Garmin samples independently of
response success. SQL joins adjacent points within each channel's allowed
gap and clips them to the requested intervals. It returns the newest
observed point with its source, ignores future or estimated points and
keeps calendar summaries separate. The shared replay fence keeps a
rebuild from changing the view during the read.

Native Linux ARM64 matched eleven pinned Python contexts with eight
channels each. The fixtures cover first and last points, a long gap,
exact gap limits, DST overlaps and gaps at midnight, source identity,
technical errors, sparse historical measurements and daily recency.
Numeric comparisons allow an absolute difference of 1e-12 seconds or
ratio units. The top-level freshness and history status response is
separate integration work.
