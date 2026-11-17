"""Measure cold-start and idle costs in an isolated synthetic Docker deployment."""

import argparse
import json
import os
import secrets
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path

DATABASE = "garmin_resource_test"
POSTGRES = (
    "timescale/timescaledb:latest-pg17@"
    "sha256:bc8527e62f70f0766b29515077965025872fabb5349db421565f69ee273baf2d"
)
POSTGRES_CONFIG = {
    "shared_buffers": "64MB",
    "effective_cache_size": "256MB",
    "work_mem": "2MB",
    "maintenance_work_mem": "32MB",
    "max_connections": "40",
    "max_parallel_workers_per_gather": "0",
    "timescaledb.max_background_workers": "4",
}
CGROUP_READ = r"""
set -eu
cd /sys/fs/cgroup
for name in cpu.max cpu.stat io.stat memory.current memory.peak memory.stat memory.events memory.max memory.swap.current memory.swap.max memory.pressure pids.current; do
    printf '@%s\n' "$name"
    cat "$name"
done
printf '@process_memory\n'
for file in /proc/[0-9]*/status; do
    pid=${file#/proc/}
    pid=${pid%/status}
    # The sampling shell and its children are included in the cgroup counters.
    [ "$pid" != "$$" ] || continue
    [ -r "$file" ] || continue
    printf '%s\n' "$pid"
    sed -n '/^Name:/p; /^VmRSS:/p; /^VmHWM:/p; /^Threads:/p' "$file" 2>/dev/null || true
    if [ -r /proc/"$pid"/smaps_rollup ]; then
        sed -n '/^Pss:/p' /proc/"$pid"/smaps_rollup 2>/dev/null || true
    fi
done
"""
HTTP_PROBE = """
import time
import urllib.request

start = time.monotonic()
with urllib.request.urlopen(URL, timeout=2) as response:
    if response.status != 200:
        raise SystemExit(1)
    response.read()
print(round((time.monotonic() - start) * 1000, 3))
"""


def docker(*args, variables=None, check=True, timeout=20):
    process = subprocess.run(
        ["docker", *args],
        env={**os.environ, **(variables or {})},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if check and process.returncode:
        raise RuntimeError(f"Docker {args[0]} failed with status {process.returncode}")
    return process


def until_ready(check, description, timeout=80):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result.returncode == 0:
            return result.stdout.strip()
        time.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for {description}")


def sample(container, *, user=None):
    user_flag = ["--user", user] if user else []
    sections = docker("exec", *user_flag, container, "sh", "-c", CGROUP_READ).stdout.split("@")[1:]
    return {
        name: content.strip()
        for section in sections
        for name, _, content in [section.partition("\n")]
    }


def report_run(args):
    started = time.monotonic()
    prefix = f"garmin-benchmark-{secrets.token_hex(5)}"
    db, api, worker, migrate, observer = (
        f"{prefix}-{role}" for role in ("db", "api", "worker", "migrate", "observer")
    )
    password = secrets.token_urlsafe(24)
    app_env = {
        "GA_DATABASE_URL": f"postgresql+psycopg://garmin:{password}@{db}/{DATABASE}",
        "GA_INTEGRATIONS": "[]",
        "GA_LOCK_DIR": "/tmp/benchmark/state",
        "GA_DATA_DIR": "/tmp/benchmark/data",
        "GA_TOKEN_DIR": "/tmp/benchmark/tokens",
        "GA_BACKUP_DIR": "/tmp/benchmark/backups",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    db_env = {
        "POSTGRES_USER": "garmin",
        "POSTGRES_PASSWORD": password,
        "POSTGRES_DB": DATABASE,
        "NO_TS_TUNE": "true",
    }
    image = docker("image", "inspect", args.image, "--format", "{{.Id}}").stdout.strip()
    image_revision = docker(
        "image",
        "inspect",
        image,
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
    ).stdout.strip()
    if image_revision != args.sha:
        raise RuntimeError("Application image revision label does not match --sha")
    platform = docker(
        "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}"
    ).stdout.strip()
    server_format = "{{.CgroupVersion}} {{.NCPU}} {{.MemTotal}} {{.KernelVersion}}"
    engine = docker("info", "--format", server_format).stdout.strip()
    if not engine.startswith("2 "):
        raise RuntimeError("The startup benchmark requires cgroup v2")

    def launch(name, image, *command, limits=(), env=None):
        env_flags = [flag for key in env or {} for flag in ("-e", key)]
        cleanup.callback(docker, "rm", "-fv", name, check=False)
        docker(
            "run",
            "-d",
            "--pull",
            "never",
            "--name",
            name,
            "--network",
            prefix,
            *limits,
            *env_flags,
            image,
            *command,
            variables=env,
        )

    def elapsed():
        return round(time.monotonic() - started, 3)

    cpu = ["--cpuset-cpus", args.cpu]
    entry = Path(args.healthcheck).read_text(encoding="utf-8")
    with ExitStack() as cleanup:
        docker("network", "create", "--internal", prefix)
        cleanup.callback(docker, "network", "rm", prefix, check=False)
        options = [
            part for key, value in POSTGRES_CONFIG.items() for part in ("-c", f"{key}={value}")
        ]
        launch(
            db,
            POSTGRES,
            "postgres",
            *options,
            limits=(*cpu, "-m", "320m", "--memory-swap", "768m"),
            env=db_env,
        )
        until_ready(
            lambda: docker(
                "exec",
                db,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-U",
                "garmin",
                "-d",
                DATABASE,
                check=False,
            ),
            "synthetic PostgreSQL",
        )
        lifecycle = {"database_ready_seconds": elapsed()}
        db_metadata = (
            docker(
                "exec",
                db,
                "psql",
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "garmin",
                "-d",
                DATABASE,
                "-Atc",
                "SELECT current_database(), current_setting('server_version'), extversion "
                "FROM pg_extension WHERE extname = 'timescaledb'",
            )
            .stdout.strip()
            .split("|")
        )
        if len(db_metadata) != 3 or db_metadata[0] != DATABASE:
            raise RuntimeError("Benchmark database identity or TimescaleDB extension did not match")

        # Keep the migration cgroup alive after success, so its startup peak is readable.
        launch(
            migrate,
            image,
            "sh",
            "-ec",
            "garmin-ai migrate; touch /tmp/migrated; exec sleep 300",
            limits=(*cpu, "-m", "256m"),
            env=app_env,
        )
        until_ready(
            lambda: docker("exec", migrate, "test", "-f", "/tmp/migrated", check=False), "migration"
        )
        lifecycle["migrated_seconds"] = elapsed()
        migration_sample = sample(migrate)
        docker("stop", "-t", "1", migrate)

        launch(
            api,
            image,
            "garmin-ai",
            "serve",
            "--host",
            "0.0.0.0",
            limits=(*cpu, "-m", "192m", "--memory-swap", "384m"),
            env=app_env,
        )
        launch(
            worker,
            image,
            "garmin-ai",
            "worker",
            limits=(*cpu, "-m", "384m", "--memory-swap", "1024m"),
            env=app_env,
        )
        # Readiness probes execute in a separate cgroup. Their RSS is excluded.
        launch(observer, image, "sleep", "300", env=app_env)

        def probe(path):
            url = f"http://{api}:8080{path}"
            code = f"URL = {url!r}\n{HTTP_PROBE}"
            return docker("exec", observer, "python", "-c", code, check=False)

        until_ready(lambda: probe("/health/ready"), "API readiness")
        lifecycle["api_observed_ready_seconds"] = elapsed()
        until_ready(
            lambda: docker("exec", observer, "python", "-c", entry, check=False), "worker heartbeat"
        )
        lifecycle["worker_observed_ready_seconds"] = elapsed()
        samples = []
        for _ in range(args.samples):
            result = probe("/health/live")
            if result.returncode:
                raise RuntimeError("API liveness failed during the idle measurement")
            samples.append(
                {
                    "elapsed_seconds": elapsed(),
                    "health_live_ms": float(result.stdout),
                    "database": sample(db, user="postgres"),
                    "api": sample(api),
                    "worker": sample(worker),
                }
            )
            time.sleep(args.interval)
        revision = docker(
            "exec",
            db,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "garmin",
            "-d",
            DATABASE,
            "-Atc",
            "SELECT version_num FROM alembic_version",
        ).stdout.strip()
        return {
            "baseline_sha": args.sha,
            "platform": platform,
            "docker_engine": engine,
            "application_image_id": image,
            "database_image": POSTGRES,
            "postgresql": db_metadata[1],
            "timescaledb": db_metadata[2],
            "migration_revision": revision,
            "limits": {
                "shared_cpu_affinity": args.cpu,
                "database_memory_mib": 320,
                "api_memory_mib": 192,
                "worker_memory_mib": 384,
                "migration_memory_mib": 256,
            },
            "config": POSTGRES_CONFIG,
            "lifecycle": lifecycle,
            "migration": migration_sample,
            "idle_samples": samples,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Locally built baseline application image")
    parser.add_argument("--sha", required=True, help="Git SHA from which the image was built")
    parser.add_argument("--healthcheck", default="deploy/gcp/healthcheck.py", type=Path)
    parser.add_argument("--cpu", default="0", help="Shared Docker CPU affinity")
    parser.add_argument("--samples", default=8, type=int, choices=range(1, 61), metavar="1..60")
    parser.add_argument("--interval", default=2, type=float, choices=(0.5, 1, 2, 5))
    args = parser.parse_args()
    print(json.dumps(report_run(args), indent=2))


if __name__ == "__main__":
    main()
