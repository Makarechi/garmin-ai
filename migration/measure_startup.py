"""Measure startup, idle and workload costs in isolated synthetic Docker deployments."""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

from web_chat_workload import MAX_MESSAGE_LENGTH, MULTIBYTE_FILLER

DATABASE = "garmin_resource_test"
# The offline CLI and the running API share the 384 MiB application budget.
PAIRED_BACKUP_MEMORY_MIB = 192
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


def local_image(reference, revision):
    image = docker("image", "inspect", reference, "--format", "{{.Id}}").stdout.strip()
    actual = docker(
        "image",
        "inspect",
        image,
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
    ).stdout.strip()
    if actual != revision:
        raise RuntimeError("Application image revision label does not match the requested revision")
    platform = docker(
        "image", "inspect", image, "--format", "{{.Os}}/{{.Architecture}}"
    ).stdout.strip()
    return image, platform


def collect_web_chat_workload(args, observer, db, api, worker, elapsed):
    script = Path(__file__).with_name("web_chat_workload.py").read_text(encoding="utf-8")
    command = [
        "docker",
        "exec",
        observer,
        "python",
        "-c",
        script,
        f"http://{api}:8080",
        str(args.web_chat_messages),
    ]
    if args.web_chat_multibyte:
        command.append("--multibyte")
    return collect_client_workload(command, args.interval, db, api, worker, elapsed)


def collect_http_analytics(args, observer, db, api, worker, elapsed):
    for filename in ("analytics_workload.py", "web_chat_workload.py"):
        source = Path(__file__).with_name(filename)
        docker("cp", str(source), f"{observer}:/tmp/{filename}")
    command = [
        "docker",
        "exec",
        observer,
        "python",
        "/tmp/analytics_workload.py",
        str(args.analytics_rounds),
        "--http-base",
        f"http://{api}:8080",
    ]
    return collect_client_workload(command, args.interval, db, api, worker, elapsed)


def collect_fit_replay(args, observer, db, api, worker, elapsed, probe):
    for filename in ("fit_replay_workload.py", "fit_workload.py", "fitlimit_workload.py"):
        source = Path(__file__).with_name(filename)
        docker("cp", str(source), f"{observer}:/tmp/{filename}")
    command = ["docker", "exec", observer, "python", "/tmp/fit_replay_workload.py"]
    extra = ("--with-controls",) if args.controls_during_fit else ()
    fixture = json.loads(
        docker(*command[1:], "prepare", args.fit_replay, *extra, timeout=300).stdout
    )
    start = time.monotonic()
    controls = {}
    if args.controls_during_fit:
        script = Path(__file__).with_name("control_overlap.py").read_text(encoding="utf-8")
        before = json.loads(docker(*command[1:], "running", fixture["job"]).stdout)
        response = docker(
            "exec", observer, "python", "-c", script, f"http://{api}:8080", "--paired-fit"
        )
        after = json.loads(docker(*command[1:], "running", fixture["job"]).stdout)
        controls = {
            "control_overlap": {**json.loads(response.stdout), "before": before, "after": after}
        }
    result = collect_client_workload(
        [*command, "wait", json.dumps(fixture)],
        args.interval,
        db,
        api,
        worker,
        elapsed,
        probe=probe,
    )
    processing = round(time.monotonic() - start, 3)
    validated = json.loads(docker(*command[1:], "verify", json.dumps(fixture), timeout=600).stdout)
    public_fixture = {key: value for key, value in fixture.items() if key not in {"source", "job"}}
    return {
        **public_fixture,
        **result,
        **validated,
        **controls,
        "observed_queue_seconds": processing,
    }


def collect_paired_backup(args, observer, workload, executable, db, api, worker, elapsed, probe):
    for filename in ("paired_backup_fixture.py", "backup_workload.py"):
        script = Path(__file__).with_name(filename)
        docker("cp", str(script), f"{observer}:/tmp/{filename}")
    fixture = ["exec", observer, "python", "/tmp/paired_backup_fixture.py"]
    expected = json.loads(docker(*fixture, "prepare", str(args.backup_mib), timeout=180).stdout)
    script = Path(__file__).with_name("paired_backup_commands.sh").read_text(encoding="utf-8")
    measured = collect_client_workload(
        ["docker", "exec", workload, "sh", "-c", script, "paired-backup", executable],
        args.interval,
        db,
        api,
        worker,
        elapsed,
        backup=workload,
        probe=probe,
    )
    checked = json.loads(docker(*fixture, "verify", json.dumps(expected), timeout=180).stdout)
    return {
        "synthetic_raw_bytes": args.backup_mib * 1024 * 1024,
        "worker_stopped_for_cli": True,
        **measured,
        **checked,
    }


def collect_client_workload(
    command, interval, db, api, worker, elapsed, *, mcp=None, backup=None, probe=None
):
    def capture():
        resources = {
            "elapsed_seconds": elapsed(),
            "database": sample(db, user="postgres"),
            "api": sample(api),
            "worker": sample(worker) if worker else None,
        }
        if mcp:
            resources["mcp"] = sample(mcp)
        if backup:
            resources["backup"] = sample(backup)
        if probe:
            live = probe("/health/live")
            resources["health_live_ms"] = float(live.stdout) if not live.returncode else None
        return resources

    samples = []
    deadline = time.monotonic() + 900
    with subprocess.Popen(
        command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as process:
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        "Synthetic client workload exceeded the measurement deadline"
                    )
                samples.append(capture())
                time.sleep(interval)
            stdout, stderr = process.communicate(timeout=5)
            if process.returncode:
                phase = (
                    re.search(r"Paired backup phase ([a-z]+) failed", stderr) if backup else None
                )
                detail = f" in the {phase[1]} phase" if phase else ""
                raise RuntimeError(
                    f"Synthetic client workload failed with status {process.returncode}{detail}"
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    return {**json.loads(stdout), "samples": samples, "after_workload": capture()}


def verify_web_chat_storage(db, result, *, multibyte):
    psql = ["docker", "exec", db, "psql", "-X", "-U", "garmin", "-d", DATABASE, "-Atc"]
    filler = MULTIBYTE_FILLER if multibyte else "x"
    counters = (
        subprocess.check_output(
            [
                *psql,
                "SELECT (SELECT count(*) FROM events), "
                "(SELECT count(*) FROM audit_log WHERE action='create'), "
                "(SELECT count(*) FROM inbound_messages WHERE channel='web'), "
                "(SELECT count(*) FROM outbox_messages), "
                "(SELECT count(*) FROM inbound_messages WHERE channel='web' "
                f"AND normalized_text=repeat(chr({ord(filler)}), {MAX_MESSAGE_LENGTH}))",
            ],
            text=True,
            timeout=10,
        )
        .strip()
        .split("|")
    )
    observed = list(map(int, counters))
    if observed != [
        result["notes"],
        result["notes"],
        result["message_count"],
        result["message_count"],
        result["maximum_length_messages"],
    ]:
        raise RuntimeError(
            "The synthetic conversation did not retain exactly one write per message"
        )
    digest = hashlib.sha256()
    with subprocess.Popen(
        [*psql, "SELECT payload->>'description' FROM events ORDER BY payload->>'description'"],
        stdout=subprocess.PIPE,
        text=True,
    ) as process:
        for note in process.stdout:
            digest.update(note.rstrip("\n").encode())
        if process.wait(timeout=10) or digest.hexdigest() != result["note_digest"]:
            raise RuntimeError(
                "The stored synthetic note content did not match the submitted corpus"
            )


def report_run(args, image, platform, *, native=False, observer_image=None):
    started = time.monotonic()
    executable = "garmin-ai-rust" if native else "garmin-ai"
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
    if native:
        app_env.update(
            GA_LOCK_DIR="/app/state",
            GA_DATA_DIR="/app/data",
            GA_TOKEN_DIR="/app/tokens/garmin",
            GA_BACKUP_DIR="/app/backups",
        )
    if args.replay_days or args.fit_replay:
        app_env["GA_TIMEZONE"] = "UTC"
    if args.fit_replay:
        app_env["GA_DATA_DIR"] = "/fit-fixture"
    api_env = dict(app_env)
    control_env = {}
    paired_analytics = args.candidate_image is not None and args.analytics_rounds is not None
    paired_mcp = args.candidate_image is not None and args.mcp_idle_seconds is not None
    paired_backup = args.candidate_image is not None and args.backup_mib is not None
    if args.web_chat_messages or paired_analytics:
        control_env["GA_API_KEY"] = secrets.token_urlsafe(32)
        api_env.update(control_env)
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
    server_format = "{{.CgroupVersion}} {{.NCPU}} {{.MemTotal}} {{.KernelVersion}}"
    engine = docker("info", "--format", server_format).stdout.strip()
    if not engine.startswith("2 "):
        raise RuntimeError("The startup benchmark requires cgroup v2")

    def launch(name, container_image, *command, limits=(), env=None, mounts=()):
        env_flags = [flag for key in env or {} for flag in ("-e", key)]
        native_flags = ("--entrypoint", "") if native and container_image == image else ()
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
            "--no-healthcheck",
            *native_flags,
            *limits,
            *mounts,
            *env_flags,
            container_image,
            *command,
            variables=env,
        )

    def elapsed():
        return round(time.monotonic() - started, 3)

    cpu = ["--cpuset-cpus", args.cpu]
    entry = None if native else Path(args.healthcheck).read_text(encoding="utf-8")
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
            f"{executable} migrate; touch /tmp/migrated; exec sleep 300",
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
            executable,
            "serve",
            "--host",
            "0.0.0.0",
            limits=(*cpu, "-m", "192m", "--memory-swap", "384m"),
            env=api_env,
        )
        shared_archive = ()
        observer_env = app_env | control_env
        worker_command = (executable, "worker")
        if args.fit_replay:
            volume = f"{prefix}-fit"
            docker("volume", "create", volume)
            cleanup.callback(docker, "volume", "rm", volume, check=False)
            shared_archive = ("--mount", f"type=volume,src={volume},dst=/fit-fixture")
            # Retain the cgroup counters even if the workload exceeds its memory limit.
            worker_command = (
                "sh",
                "-c",
                f"{executable} worker; echo $? > /fit-fixture/worker-exit; exec sleep infinity",
            )
        if paired_backup:
            volume = f"{prefix}-backup"
            docker("volume", "create", volume)
            cleanup.callback(docker, "volume", "rm", volume, check=False)
            shared_archive = ("--mount", f"type=volume,src={volume},dst=/backup-fixture")
            backup_env = app_env | {
                "GA_DATA_DIR": "/backup-fixture/data",
                "GA_TOKEN_DIR": "/backup-fixture/tokens",
                "GA_LOCK_DIR": "/backup-fixture/locks",
                "GA_BACKUP_DIR": "/backup-fixture/backups",
                "GA_BACKUP_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
                "GA_BACKUP_DATABASE_URL": f"postgresql+psycopg://garmin:{password}@{db}/garmin_resource_backup_test",
                "GA_RESTORE_DATABASE_URL": f"postgresql+psycopg://garmin:{password}@{db}/garmin_resource_restore_test",
            }
            observer_env.update(backup_env)
        # Readiness probes execute in a separate cgroup. Their RSS is excluded.
        launch(
            observer,
            observer_image or image,
            "sleep",
            "infinity",
            env=observer_env,
            mounts=shared_archive,
        )
        if args.fit_replay:
            docker(
                "exec",
                "--user",
                "0",
                observer,
                "install",
                "-d",
                "-m",
                "0700",
                "-o",
                "1000",
                "-g",
                "1000",
                "/fit-fixture",
                "/fit-fixture/raw",
            )
        if paired_backup:
            docker(
                "exec",
                "--user",
                "0",
                observer,
                "install",
                "-d",
                "-m",
                "0700",
                "-o",
                "1000",
                "-g",
                "1000",
                "/backup-fixture",
            )
        launch(
            worker,
            image,
            *worker_command,
            limits=(*cpu, "-m", "384m", "--memory-swap", "1024m"),
            env=app_env,
            mounts=shared_archive if args.fit_replay else (),
        )

        def probe(path):
            url = f"http://{api}:8080{path}"
            code = f"URL = {url!r}\n{HTTP_PROBE}"
            return docker("exec", observer, "python", "-c", code, check=False)

        until_ready(lambda: probe("/health/ready"), "API readiness")
        lifecycle["api_observed_ready_seconds"] = elapsed()

        def heartbeat():
            if native:
                return docker(
                    "run",
                    "--rm",
                    "--pull",
                    "never",
                    "--network",
                    prefix,
                    "--entrypoint",
                    executable,
                    "-e",
                    "GA_DATABASE_URL",
                    image,
                    "healthcheck",
                    "--worker",
                    variables=app_env,
                    check=False,
                )
            return docker("exec", observer, "python", "-c", entry, check=False)

        until_ready(heartbeat, "worker heartbeat")
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
        if paired_backup:
            docker("stop", "-t", "10", worker)
            for database in ("garmin_resource_backup_test", "garmin_resource_restore_test"):
                docker("exec", db, "createdb", "-U", "garmin", database)
            launch(
                workload,
                image,
                "sleep",
                "infinity",
                limits=(*cpu, "-m", f"{PAIRED_BACKUP_MEMORY_MIB}m", "--memory-swap", "384m"),
                env=backup_env | {"GA_DATABASE_URL": backup_env["GA_BACKUP_DATABASE_URL"]},
                mounts=(
                    *shared_archive,
                    "--tmpfs",
                    "/tmp/low-backup-space:rw,size=8m,uid=1000,gid=1000",
                ),
            )
            docker("exec", workload, executable, "migrate", timeout=120)
            docker(
                "exec",
                workload,
                "sh",
                "-ec",
                'GA_DATABASE_URL="$GA_RESTORE_DATABASE_URL" GA_DATA_DIR=/backup-fixture/recovery-data "$1" migrate',
                "migrate-recovery",
                executable,
                timeout=120,
            )
            workload_result["backup_workload"] = collect_paired_backup(
                args, observer, workload, executable, db, api, None, elapsed, probe
            )
        if args.fit_replay:
            workload_result["fit_replay_workload"] = collect_fit_replay(
                args, observer, db, api, worker, elapsed, probe
            )
        if args.web_chat_messages:
            result = collect_web_chat_workload(args, observer, db, api, worker, elapsed)
            verify_web_chat_storage(db, result, multibyte=args.web_chat_multibyte)
            workload_result["web_chat_workload"] = result
        if paired_analytics:
            workload_result["http_analytics_workload"] = collect_http_analytics(
                args, observer, db, api, worker, elapsed
            )
        if paired_mcp:
            launch(
                workload,
                image,
                "sleep",
                "infinity",
                limits=(*cpu, "-m", "384m", "--memory-swap", "1024m"),
                env=app_env,
            )
            command = [
                sys.executable,
                str(Path(__file__).with_name("mcp_workload.py")),
                str(args.mcp_idle_seconds),
                "--container",
                workload,
            ]
            if native:
                command.append("--native")
            workload_result["mcp_workload"] = collect_client_workload(
                command, args.interval, db, api, worker, elapsed, mcp=workload
            )
        workload_modes = (
            ("fit", args.fit_records),
            ("fitlimit", args.fit_limit_mib),
            ("sync", args.sync_days),
            ("reconcile", args.reconcile_days),
            ("backup", None if paired_backup else args.backup_mib),
            ("analytics", None if paired_analytics else args.analytics_rounds),
            ("mcp", None if paired_mcp else args.mcp_idle_seconds),
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
            "candidate_sha" if native else "baseline_sha": args.candidate_sha
            if native
            else args.sha,
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
                **({"mcp_memory_mib": 384} if paired_mcp else {}),
                **({"backup_memory_mib": PAIRED_BACKUP_MEMORY_MIB} if paired_backup else {}),
            },
            "config": POSTGRES_CONFIG,
            "lifecycle": lifecycle,
            "migration": migration_sample,
            "idle_samples": samples,
            **workload_result,
        }


def check_fit_controls(result):
    observed = result["control_overlap"]
    if (
        observed["failures"]
        or not {"authenticated_web_pause_ms", "operations_response_ms", "webhook_ingress_ms"}
        <= observed["latencies_ms"].keys()
    ):
        raise RuntimeError(
            f"Native control probes failed during FIT replay: {observed['failures']}"
        )
    if max(observed["latencies_ms"].values()) > 2000:
        raise RuntimeError("Native control latency exceeded two seconds during FIT replay")
    if not observed["before"]["active"] or observed["after"] != observed["before"]:
        raise RuntimeError("The native control probes did not run during the same FIT lease")
    if (
        not result["owner_pause_persisted"]
        or not result["telegram_ingress_persisted"]
        or not result["observed_lease_renewals"]
    ):
        raise RuntimeError(
            "Native FIT replay did not preserve its control writes and renew its lease"
        )


def check_fit_replay(size, before, after):
    fixture = ("records", "uncompressed_bytes", "compressed_bytes", "archive_sha256")
    if any(before[key] != after[key] for key in fixture):
        raise RuntimeError("The paired FIT experiments used different archived inputs")
    if size == "over-limit":
        for result in (before, after):
            if (
                result.get("status") != "error"
                or result["stored_rows"]
                or result["fit_pointer_published"]
            ):
                raise RuntimeError("Oversized FIT input published a projection")
        return
    for result in (before, after):
        if result is before and size == "ceiling" and result.get("worker_exit_code") == 137:
            memory = result["after_workload"]["worker"]["memory.events"]
            counters = {
                name: int(value) for name, value in (line.split() for line in memory.splitlines())
            }
            if not counters["oom_kill"] or result["stored_rows"] or result["fit_pointer_published"]:
                raise RuntimeError("Baseline FIT exit lacked clean OOM evidence")
            continue
        if (
            result.get("status"),
            result["source_status"],
            result["stored_rows"],
            result["fit_pointer_published"],
        ) != ("normalized", "normalized", result["records"], True):
            runtime = "Python" if result is before else "Rust"
            summary = {
                key: result.get(key)
                for key in (
                    "status",
                    "error_type",
                    "source_status",
                    "stored_rows",
                    "fit_pointer_published",
                    "job_status",
                )
            }
            raise RuntimeError(
                f"{runtime} FIT replay did not publish the expected records {summary}"
            )
    if (
        before.get("status") == "normalized"
        and before["projection_sha256"] != after["projection_sha256"]
    ):
        raise RuntimeError("The paired FIT experiments published different projections")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Locally built baseline application image")
    parser.add_argument("--sha", required=True, help="Git SHA from which the image was built")
    parser.add_argument("--candidate-image", help="Locally built Rust release image for comparison")
    parser.add_argument("--candidate-sha", help="Git SHA of the Rust release image")
    parser.add_argument("--healthcheck", default="deploy/gcp/healthcheck.py", type=Path)
    parser.add_argument("--cpu", default="0", help="Shared Docker CPU affinity")
    parser.add_argument("--samples", default=8, type=int, choices=range(1, 61), metavar="1..60")
    parser.add_argument("--interval", default=2, type=float, choices=(0.5, 1, 2, 5))
    workload = parser.add_mutually_exclusive_group()
    workload.add_argument("--fit-records", type=int, choices=(5000, 50000))
    workload.add_argument("--fit-limit-mib", type=int, choices=(100,))
    workload.add_argument("--fit-replay", choices=("developer", "ceiling", "over-limit"))
    workload.add_argument("--sync-days", type=int, choices=(1, 7, 30, 365))
    workload.add_argument("--reconcile-days", type=int, choices=(7, 30))
    workload.add_argument("--backup-mib", type=int, choices=(32, 128))
    workload.add_argument("--analytics-rounds", type=int, choices=(1, 10))
    workload.add_argument("--mcp-idle-seconds", type=int, choices=(5, 10))
    workload.add_argument("--voice-rounds", type=int, choices=(1, 2))
    workload.add_argument("--replay-days", type=int, choices=(7, 30))
    workload.add_argument("--web-chat-messages", type=int, choices=(80, 800))
    parser.add_argument("--web-chat-multibyte", action="store_true")
    parser.add_argument("--preload-history", action="store_true")
    parser.add_argument("--fit-developer", action="store_true")
    parser.add_argument("--controls-during-fit", action="store_true")
    args = parser.parse_args()
    if args.web_chat_multibyte and not args.web_chat_messages:
        parser.error("--web-chat-multibyte requires --web-chat-messages")
    if args.preload_history and args.sync_days not in (7, 30, 365):
        parser.error("--preload-history requires --sync-days 7, 30 or 365")
    if args.fit_developer and not args.fit_records:
        parser.error("--fit-developer requires --fit-records")
    if args.controls_during_fit and args.fit_records != 50000 and args.fit_replay != "ceiling":
        parser.error("--controls-during-fit requires --fit-records 50000 or --fit-replay ceiling")
    if bool(args.candidate_image) != bool(args.candidate_sha):
        parser.error("--candidate-image and --candidate-sha must be supplied together")
    if args.candidate_image and any(
        getattr(args, name) is not None
        for name in (
            "fit_records",
            "fit_limit_mib",
            "sync_days",
            "reconcile_days",
            "voice_rounds",
            "replay_days",
        )
    ):
        parser.error(
            "The paired comparison supports startup, web chat, analytics, MCP, FIT replay and backup"
        )
    baseline_image, platform = local_image(args.image, args.sha)
    if args.candidate_image:
        candidate_image, candidate_platform = local_image(args.candidate_image, args.candidate_sha)
        if candidate_image == baseline_image or candidate_platform != platform:
            parser.error("Comparison requires distinct images built for the same platform")
        baseline = report_run(args, baseline_image, platform)
        candidate = report_run(
            args, candidate_image, platform, native=True, observer_image=baseline_image
        )
        revision = baseline["migration_revision"]
        if (
            not re.fullmatch(r"[0-9a-f]{12}", revision)
            or revision != candidate["migration_revision"]
        ):
            raise RuntimeError("Benchmark versions migrated to different database schemas")
        if args.backup_mib:
            before, after = baseline["backup_workload"], candidate["backup_workload"]
            if any(
                before[key] != after[key]
                for key in ("raw_sha256", "restored_rows", "no_partial_destination")
            ):
                print(json.dumps({"python": baseline, "rust": candidate}, indent=2))
                raise RuntimeError("Paired backup and restore disagree on retained synthetic data")
        if args.fit_replay:
            try:
                check_fit_replay(
                    args.fit_replay,
                    baseline["fit_replay_workload"],
                    candidate["fit_replay_workload"],
                )
                if args.controls_during_fit:
                    check_fit_controls(candidate["fit_replay_workload"])
            except RuntimeError:
                print(json.dumps({"python": baseline, "rust": candidate}, indent=2))
                raise
        if args.web_chat_messages:
            fields = (
                "notes",
                "message_count",
                "note_digest",
                "visible_messages",
                "maximum_length_messages",
                "maximum_message_utf8_bytes",
                "text_encoding",
            )
            before = baseline["web_chat_workload"]
            after = candidate["web_chat_workload"]
            if any(before[field] != after[field] for field in fields):
                raise RuntimeError("The paired web chat workloads produced different results")
        if args.analytics_rounds:
            before = baseline["http_analytics_workload"]["result"]
            after = candidate["http_analytics_workload"]["result"]
            if before != after:
                raise RuntimeError("The paired HTTP analytics workloads produced different results")
        if args.mcp_idle_seconds:
            before, after = baseline["mcp_workload"], candidate["mcp_workload"]
            if any(before[key] != after[key] for key in ("tool_names", "read_result")):
                raise RuntimeError("The paired MCP sessions produced different results")
        print(json.dumps({"python": baseline, "rust": candidate}, indent=2))
        return 0
    report = report_run(args, baseline_image, platform)
    print(json.dumps(report, indent=2))
    return 1 if report.get("fitlimit_workload", {}).get("exit_code") else 0


if __name__ == "__main__":
    raise SystemExit(main())
