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
    db, api, worker, migrate, observer, workload = (
        f"{prefix}-{role}" for role in ("db", "api", "worker", "migrate", "observer", "workload")
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
    if args.replay_days:
        app_env["GA_TIMEZONE"] = "UTC"
    api_env = dict(app_env)
    control_env = {}
    if args.controls_during_fit:
        control_env = {
            "GA_API_KEY": secrets.token_urlsafe(32),
            "GA_TELEGRAM_WEBHOOK_SECRET": secrets.token_urlsafe(24),
            "GA_TELEGRAM_USER_ID": "42",
        }
        api_env.pop("GA_INTEGRATIONS")
        api_env.update(control_env)
        api_env["GA_TELEGRAM_BOT_TOKEN"] = "synthetic-fixture"
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

    def launch(name, image, *command, limits=(), env=None, mounts=()):
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
            *mounts,
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
            env=api_env,
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
        launch(observer, image, "sleep", "infinity", env=app_env | control_env)

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

        workload_result = {}
        workload_modes = (
            ("fit", args.fit_records),
            ("fitlimit", args.fit_limit_mib),
            ("sync", args.sync_days),
            ("reconcile", args.reconcile_days),
            ("backup", args.backup_mib),
            ("analytics", args.analytics_rounds),
            ("mcp", args.mcp_idle_seconds),
            ("voice", args.voice_rounds),
            ("replay", args.replay_days),
        )
        selected = next(
            ((name, value) for name, value in workload_modes if value is not None), None
        )
        if selected:
            name, quantity = selected
            workload_key = f"{name}_workload"
            directory = Path(__file__).resolve().parent
            mounts = ["--mount", f"type=bind,src={directory},dst=/benchmark,readonly"]
            if name == "replay":
                docker("stop", "-t", "30", worker)
            if name == "backup":
                docker("exec", db, "createdb", "-U", "garmin", "garmin_resource_restore_test")
                mounts.extend(("--tmpfs", "/tmp/low-backup-space:rw,size=8m,uid=1000,gid=1000"))
            extra = " --preload-history" if args.preload_history else ""
            if args.fit_developer:
                extra = " --developer"
            if args.controls_during_fit:
                extra += " --lease-probe"
            workload_command = (
                f"python /benchmark/{workload_key}.py {quantity}{extra} > /tmp/result.json; "
                "task_status=$?; echo $task_status > /tmp/finished; "
                "exec sleep infinity"
            )
            launch(
                workload,
                image,
                "sh",
                "-c",
                workload_command,
                limits=(*cpu, "-m", "384m", "--memory-swap", "1024m"),
                env=app_env,
                mounts=mounts,
            )
            controls = {}
            if args.controls_during_fit:
                until_ready(
                    lambda: docker(
                        "exec", workload, "test", "-f", "/tmp/fit-projection-started", check=False
                    ),
                    "FIT projection start",
                )
                script = Path(__file__).with_name("control_overlap.py").read_text(encoding="utf-8")
                response = docker("exec", observer, "python", "-c", script, f"http://{api}:8080")
                write_finished = docker(
                    "exec",
                    workload,
                    "test",
                    "-f",
                    "/tmp/fit-projection-store-finished",
                    check=False,
                )
                controls = json.loads(response.stdout)
                controls["store_finished_during_probes"] = write_finished.returncode == 0
            workload_samples = []
            deadline = time.monotonic() + 1600
            while time.monotonic() < deadline:
                finished = docker("exec", workload, "test", "-f", "/tmp/finished", check=False)
                if not finished.returncode:
                    break
                state = docker("inspect", workload, "--format", "{{.State.Status}}").stdout.strip()
                if state != "running":
                    raise RuntimeError("Synthetic workload exited before completing")
                live = probe("/health/live")
                workload_samples.append(
                    {
                        "elapsed_seconds": elapsed(),
                        "health_live_ms": float(live.stdout) if not live.returncode else None,
                        "database": sample(db, user="postgres"),
                        "api": sample(api),
                        "worker": None if name == "replay" else sample(worker),
                        "workload": sample(workload),
                    }
                )
                time.sleep(args.interval)
            else:
                raise RuntimeError("Synthetic workload exceeded the measurement deadline")
            status = int(docker("exec", workload, "cat", "/tmp/finished").stdout)
            if status and name != "fitlimit":
                logs = docker("logs", "--tail", "1", workload, check=False).stderr
                detail = logs.replace(password, "<redacted>").strip()[:300]
                raise RuntimeError(f"Synthetic workload failed with status {status}: {detail}")
            result_path = "/tmp/fit-limit-input.json" if status else "/tmp/result.json"
            workload_result[workload_key] = {
                **json.loads(docker("exec", workload, "cat", result_path).stdout),
                "exit_code": status,
                "final_memory": sample(workload),
                "samples": workload_samples,
                **({"control_overlap": controls} if controls else {}),
            }
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
            **workload_result,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Locally built baseline application image")
    parser.add_argument("--sha", required=True, help="Git SHA from which the image was built")
    parser.add_argument("--healthcheck", default="deploy/gcp/healthcheck.py", type=Path)
    parser.add_argument("--cpu", default="0", help="Shared Docker CPU affinity")
    parser.add_argument("--samples", default=8, type=int, choices=range(1, 61), metavar="1..60")
    parser.add_argument("--interval", default=2, type=float, choices=(0.5, 1, 2, 5))
    workload = parser.add_mutually_exclusive_group()
    workload.add_argument("--fit-records", type=int, choices=(5000, 50000))
    workload.add_argument("--fit-limit-mib", type=int, choices=(100,))
    workload.add_argument("--sync-days", type=int, choices=(1, 7, 30, 365))
    workload.add_argument("--reconcile-days", type=int, choices=(7, 30))
    workload.add_argument("--backup-mib", type=int, choices=(32, 128))
    workload.add_argument("--analytics-rounds", type=int, choices=(1, 10))
    workload.add_argument("--mcp-idle-seconds", type=int, choices=(5, 10))
    workload.add_argument("--voice-rounds", type=int, choices=(1, 2))
    workload.add_argument("--replay-days", type=int, choices=(7, 30))
    parser.add_argument("--preload-history", action="store_true")
    parser.add_argument("--fit-developer", action="store_true")
    parser.add_argument("--controls-during-fit", action="store_true")
    args = parser.parse_args()
    if args.preload_history and args.sync_days not in (7, 30, 365):
        parser.error("--preload-history requires --sync-days 7, 30 or 365")
    if args.fit_developer and not args.fit_records:
        parser.error("--fit-developer requires --fit-records")
    if args.controls_during_fit and args.fit_records != 50000:
        parser.error("--controls-during-fit requires --fit-records 50000")
    report = report_run(args)
    print(json.dumps(report, indent=2))
    return 1 if report.get("fitlimit_workload", {}).get("exit_code") else 0


if __name__ == "__main__":
    raise SystemExit(main())
