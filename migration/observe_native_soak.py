"""Record native release health and resource samples from an existing local Compose install."""

import argparse
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

WEEK_SECONDS = 7 * 24 * 60 * 60
SERVICES = ("api", "worker", "db")
CGROUP_FIELDS = (
    "memory.current",
    "memory.peak",
    "memory.max",
    "memory.events",
    "memory.swap.current",
    "memory.swap.max",
    "memory.pressure",
    "cpu.stat",
    "io.stat",
    "pids.current",
)
OPERATIONS_FIELDS = (
    "runtime_heartbeat_age_seconds",
    "backup_age_seconds",
    "garmin_connection",
    "backup_capacity",
    "queue_due",
    "jobs",
    "sources",
)


class LocalOnlyRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


OPENER = build_opener(ProxyHandler({}), LocalOnlyRedirects())


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=8, check=True
    ).stdout.strip()


def local_docker():
    endpoint = docker("context", "inspect", "--format", "{{.Endpoints.docker.Host}}")
    target = urlsplit(endpoint)
    if (
        target.scheme != "unix"
        or target.netloc
        or target.query
        or target.fragment
        or not Path(target.path).is_absolute()
    ):
        raise RuntimeError("Loopback observation requires Docker on this host via a Unix socket")
    return partial(docker, "--host", endpoint)


def service_id(run_docker, project, service):
    matches = run_docker(
        "ps",
        "--all",
        "--quiet",
        "--no-trunc",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--filter",
        f"label=com.docker.compose.service={service}",
        "--filter",
        "label=com.docker.compose.oneoff=False",
    ).splitlines()
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-f]{64}", matches[0]):
        raise ValueError(f"Expected one container for the {service} service")
    return matches[0]


def container_state(run_docker, container):
    image, state, restarts, started, oom_killed = run_docker(
        "inspect",
        "--format",
        "{{.Image}} {{.State.Running}} {{.RestartCount}} {{.State.StartedAt}} {{.State.OOMKilled}}",
        container,
    ).split()
    return {
        "image": image,
        "running": state == "true",
        "restarts": int(restarts),
        "started_at": started,
        "oom_killed": oom_killed == "true",
    }


def container_cgroup(run_docker, container):
    parts = run_docker(
        "exec",
        container,
        "sh",
        "-ec",
        'cd /sys/fs/cgroup; for name do printf "%s\\n" "$name"; cat "$name"; done',
        "cgroup-snapshot",
        *CGROUP_FIELDS,
    ).splitlines()
    values = {}
    current = None
    for line in parts:
        if line in CGROUP_FIELDS:
            current = line
            values[current] = []
        elif current is not None:
            values[current].append(line)
    if set(values) != set(CGROUP_FIELDS):
        raise ValueError("The container requires readable cgroup v2 counters")
    return {name: "\n".join(lines) for name, lines in values.items()}


def http_json(url, token=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = Request(url, headers=headers)
    start = time.monotonic()
    with OPENER.open(request, timeout=4) as response:
        payload = response.read(256 * 1024 + 1)
    if len(payload) > 256 * 1024:
        raise ValueError("The local probe response exceeded its byte limit")
    return json.loads(payload), round((time.monotonic() - start) * 1000, 3)


def verify_api_port(run_docker, container, url):
    ports = run_docker("port", container, "8080/tcp").splitlines()
    if urlsplit(url).netloc not in ports:
        raise RuntimeError("The supplied address is not bound to the selected API container")


def sample(run_docker, args, selected, token):
    states = {}
    failures = []
    for service in SERVICES:
        try:
            container = service_id(run_docker, args.compose_project, service)
            if container != selected[service]:
                raise RuntimeError(f"Compose replaced the selected {service} container")
            state = container_state(run_docker, container)
            if service != "db" and state["image"] != args.image_id:
                raise RuntimeError("The application image changed")
            states[service] = state
            if state["running"]:
                state["cgroup"] = container_cgroup(run_docker, container)
                count = re.search(r"(?m)^oom_kill ([0-9]+)$", state["cgroup"]["memory.events"])
                if count is None:
                    failures.append(f"{service}:oom_counter_unavailable")
                else:
                    state["oom_kills"] = int(count[1])
            else:
                failures.append(f"{service}:stopped")
            if state["oom_killed"]:
                failures.append(f"{service}:oom_killed")
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            failures.append(f"{service}:{type(error).__name__}")
    probes, snapshot = {}, None
    if states.get("api", {}).get("running"):
        try:
            verify_api_port(run_docker, selected["api"], args.url)
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            failures.append(f"api:port:{type(error).__name__}")
        else:
            probes, snapshot = sample_http(args.url, token, failures)
    return {"containers": states, "probe_ms": probes, "operations": snapshot, "failures": failures}


def sample_http(url, token, failures):
    probes = {}
    for route, wanted in (("live", "alive"), ("ready", "ready")):
        try:
            value, elapsed = http_json(f"{url}/health/{route}")
            if value != {"status": wanted}:
                raise ValueError("Unrecognized health response")
            probes[route] = elapsed
        except (OSError, ValueError) as error:
            failures.append(f"{route}:{type(error).__name__}")
    try:
        operations, elapsed = http_json(f"{url}/operations", token)
        probes["operations"] = elapsed
        snapshot = {field: operations[field] for field in OPERATIONS_FIELDS}
        heartbeat = snapshot["runtime_heartbeat_age_seconds"]
        if heartbeat is None or heartbeat > 180:
            failures.append("worker:stale_heartbeat")
    except (KeyError, OSError, TypeError, ValueError) as error:
        snapshot = None
        failures.append(f"operations:{type(error).__name__}")
    return probes, snapshot


def track_oom_kills(current, seen):
    for service, state in current["containers"].items():
        if (count := state.get("oom_kills")) is None:
            continue
        previous = seen.get(service, count)
        if count > previous:
            current["failures"].append(f"{service}:oom_kills_increased")
        elif count < previous:
            current["failures"].append(f"{service}:oom_counter_reset")
        seen[service] = count


def track_container_restarts(current, generations):
    for service, state in current["containers"].items():
        generation = state["started_at"], state["restarts"]
        if generation != generations[service]:
            current["failures"].append(f"{service}:restarted")
        generations[service] = generation


def record(output, kind, **fields):
    value = {"kind": kind, "observed_at": datetime.now(UTC).isoformat(), **fields}
    output.write(json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n")
    output.flush()
    os.fsync(output.fileno())


def observe(args, output, token):
    run_docker = local_docker()
    selected = {name: service_id(run_docker, args.compose_project, name) for name in SERVICES}
    generations = {}
    for name in SERVICES:
        state = container_state(run_docker, selected[name])
        if name != "db" and (state["image"] != args.image_id or not state["running"]):
            raise RuntimeError(f"The selected {name} container is not running the supplied image")
        generations[name] = state["started_at"], state["restarts"]
    verify_api_port(run_docker, selected["api"], args.url)
    label = run_docker(
        "image",
        "inspect",
        "--format",
        '{{index .Config.Labels "org.opencontainers.image.revision"}}',
        args.image_id,
    )
    if label != args.revision:
        raise RuntimeError("The selected image must carry the supplied Git revision label")
    record(
        output,
        "begin",
        revision=args.revision,
        image=args.image_id,
        image_revision=label,
        compose_project=args.compose_project,
        containers=selected,
        interval_seconds=args.interval_seconds,
        requested_seconds=args.duration_seconds,
    )
    started, started_wall = time.monotonic(), time.time()
    previous, previous_wall = started, started_wall
    total_samples = incomplete_samples = gap_samples = 0
    oom_counts = {}
    while True:
        clock, wall = time.monotonic(), time.time()
        current = sample(run_docker, args, selected, token)
        track_container_restarts(current, generations)
        track_oom_kills(current, oom_counts)
        elapsed = clock - started
        wall_elapsed = wall - started_wall
        spacing = max(clock - previous, wall - previous_wall)
        if total_samples and spacing > args.interval_seconds + max(2, args.interval_seconds / 2):
            gap_samples += 1
        total_samples += 1
        incomplete_samples += bool(current["failures"])
        record(
            output,
            "sample",
            elapsed_seconds=round(elapsed, 3),
            sample_spacing_seconds=round(spacing, 3),
            **current,
        )
        if total_samples == 1 and current["failures"]:
            raise RuntimeError("Initial health check failed: " + ", ".join(current["failures"]))
        if min(elapsed, wall_elapsed) >= args.duration_seconds:
            duration = min(elapsed, wall_elapsed)
            window = "seven_day_observation" if duration >= WEEK_SECONDS else "short_observation"
            if gap_samples:
                window = "gapped_observation"
            elif incomplete_samples:
                window = "unhealthy_observation"
            record(
                output,
                "end",
                observed_seconds=round(duration, 3),
                total_samples=total_samples,
                incomplete_samples=incomplete_samples,
                excessive_sample_gaps=gap_samples,
                window=window,
            )
            return gap_samples == 0 and incomplete_samples == 0
        previous, previous_wall = clock, wall
        time.sleep(max(0, clock + args.interval_seconds - time.monotonic()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-project", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=int, default=WEEK_SECONDS)
    parser.add_argument("--interval-seconds", type=int, default=60)
    args = parser.parse_args()
    try:
        url = urlsplit(args.url)
        bind_port = url.port
    except ValueError:
        parser.error("Supply a valid loopback API origin")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.compose_project):
        parser.error("Supply the exact local Compose project")
    if url.scheme != "http" or url.netloc not in (f"127.0.0.1:{bind_port}", f"[::1]:{bind_port}"):
        parser.error("A loopback HTTP address with an explicit port is required")
    if url.path not in ("", "/") or url.query or url.fragment or not bind_port:
        parser.error("Supply only the local API origin")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image_id):
        parser.error("Supply the native image ID")
    if not re.fullmatch(r"[a-f0-9]{40}", args.revision):
        parser.error("Supply the Git revision used for the image")
    if not 1 <= args.interval_seconds <= 300 or args.duration_seconds < args.interval_seconds:
        parser.error("Use a positive interval up to 300 seconds and a duration at least that long")
    token = os.environ.get("GA_SOAK_API_KEY")
    if not token:
        parser.error("Set GA_SOAK_API_KEY in the observer environment")
    args.url = args.url.rstrip("/")
    descriptor = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        try:
            if not observe(args, output, token):
                parser.exit(1, "Observation contains incomplete samples or gaps.\n")
        except (
            KeyboardInterrupt,
            OSError,
            RuntimeError,
            subprocess.SubprocessError,
            ValueError,
        ) as error:
            reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            record(output, "interrupted", error=reason)
            parser.exit(1, "Observation ended before the requested continuous window.\n")


if __name__ == "__main__":
    main()
