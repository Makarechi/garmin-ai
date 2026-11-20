# Observe a native release

After the release candidate passes the functional gate, start the observer
against its installed Compose project on the representative Linux host. Use
the image ID reported by Docker, the verified image revision, and the API
port published by that project. Set `GA_SOAK_API_KEY` in the observer's
environment through the same private credential source used by the test
installation.

```sh
python migration/observe_native_soak.py \
  --compose-project "$GARMIN_SOAK_PROJECT" \
  --url "$GARMIN_SOAK_API_ORIGIN" \
  --image-id "$GARMIN_SOAK_IMAGE_ID" \
  --revision "$GARMIN_SOAK_REVISION" \
  --report "$GARMIN_SOAK_REPORT"
```

The observer selects the installed API, worker, and database containers.
Each record includes the time, the health and operations timings, worker
heartbeat and backup ages, queue summaries, and container resource counters.
The default window runs for 604,800 seconds and samples once a minute.
It measures both monotonic time and wall time, counts gaps and incomplete
samples, and records an interruption if a selected container is replaced.
Container start times and restart counts let reviewers correlate a recovery
with the recorded probes. A shorter smoke run uses `--duration-seconds` and
records `short_observation` in its end record.

Keep the report private, especially if the installation contains real data.
Before accepting the release, inspect queue progress, nightly and weekly
scheduling, backup history, reconnect, memory pressure, and any incomplete
sample. Attach the observed window and supporting results to the release
evidence.
