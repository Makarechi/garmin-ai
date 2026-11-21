import io
import json
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = spec_from_file_location(
    "observe_native_soak", Path(__file__).resolve().parents[1] / "migration/observe_native_soak.py"
)
assert spec is not None and spec.loader is not None
observer = module_from_spec(spec)
spec.loader.exec_module(observer)

IMAGE = "sha256:" + "a" * 64
REVISION = "b" * 40
URL = "http://127.0.0.1:37254"
CONTAINERS = dict(zip(observer.SERVICES, (char * 64 for char in "123"), strict=True))
ARGS = SimpleNamespace(compose_project="synthetic", image_id=IMAGE, url=URL, revision=REVISION)


@pytest.mark.parametrize("endpoint", ["tcp://127.0.0.1:2375", "ssh://synthetic.invalid"])
def test_network_docker_endpoint_is_rejected_before_observation(monkeypatch, endpoint):
    def context(*args):
        assert args[:2] == ("context", "inspect")
        return endpoint

    monkeypatch.setattr(observer, "docker", context)
    with pytest.raises(RuntimeError, match="Unix socket"):
        observer.observe(ARGS, io.StringIO(), "synthetic-token")


def test_observation_pins_the_inspected_local_socket(monkeypatch):
    endpoint = "unix:///run/user/1000/docker.sock"

    def docker(*args):
        if args[:2] == ("context", "inspect"):
            return endpoint
        assert args == ("--host", endpoint, "ps")
        return "synthetic-container"

    monkeypatch.setattr(observer, "docker", docker)
    assert observer.local_docker()("ps") == "synthetic-container"


@pytest.fixture
def docker_installation(monkeypatch):
    config = SimpleNamespace(
        label=REVISION, port=URL.removeprefix("http://"), api_running=True, oom_kills=0
    )

    def run_docker(*args):
        if args[0] == "ps":
            service = next(
                part for part in args if part.startswith("label=com.docker.compose.service=")
            )
            return CONTAINERS[service.split("=")[-1]]
        if args[0] == "inspect":
            api_stopped = args[-1] == CONTAINERS["api"] and not config.api_running
            return f"{IMAGE} {str(not api_stopped).lower()} 0 2026-11-21T08:00:00Z {str(api_stopped).lower()}"
        if args[0] == "exec":
            fields = dict.fromkeys(observer.CGROUP_FIELDS, "0")
            count = config.oom_kills if args[1] == CONTAINERS["worker"] else 0
            fields["memory.events"] = f"low 0\noom_kill {count}"
            return "\n".join(f"{field}\n{value}" for field, value in fields.items())
        if args[0] == "port":
            return config.port
        if args[:2] == ("image", "inspect"):
            return config.label
        pytest.fail(f"Unexpected Docker command {args[0]}")

    monkeypatch.setattr(observer, "local_docker", lambda: run_docker)
    return run_docker, config


@pytest.mark.parametrize("label", ["", "<no value>", "c" * 40])
def test_observation_rejects_an_unverified_image(monkeypatch, docker_installation, label):
    _, config = docker_installation
    config.label = label
    monkeypatch.setattr(observer, "http_json", lambda *_args: pytest.fail("Unexpected HTTP probe"))
    with pytest.raises(RuntimeError, match="Git revision label"):
        observer.observe(ARGS, io.StringIO(), "synthetic-token")


def test_stopped_api_records_oom_and_skips_http(monkeypatch, docker_installation):
    docker, config = docker_installation
    config.api_running = False
    monkeypatch.setattr(observer, "http_json", lambda *_args: pytest.fail("Unexpected HTTP probe"))
    sample = observer.sample(docker, ARGS, CONTAINERS, "synthetic-token")
    assert set(sample["failures"]) == {"api:stopped", "api:oom_killed"}
    assert sample["containers"]["api"]["oom_killed"] is True
    assert sample["probe_ms"] == {} and sample["operations"] is None


def test_changed_api_binding_is_rejected_before_http(monkeypatch, docker_installation):
    docker, config = docker_installation
    config.port = "127.0.0.1:37255"
    monkeypatch.setattr(observer, "http_json", lambda *_args: pytest.fail("Unexpected HTTP probe"))
    with pytest.raises(RuntimeError, match="not bound"):
        observer.sample(docker, ARGS, CONTAINERS, "synthetic-token")


def test_selected_api_receives_http_probes(monkeypatch, docker_installation):
    docker, _ = docker_installation
    calls = []

    def http(url, token=None):
        calls.append((url, token))
        if url.endswith("/operations"):
            document = dict.fromkeys(observer.OPERATIONS_FIELDS)
            document["runtime_heartbeat_age_seconds"] = 1
            return document, 0.5
        return {"status": "alive" if url.endswith("/live") else "ready"}, 0.5

    monkeypatch.setattr(observer, "http_json", http)
    sample = observer.sample(docker, ARGS, CONTAINERS, "synthetic-token")
    assert calls == [
        (f"{URL}/health/live", None),
        (f"{URL}/health/ready", None),
        (f"{URL}/operations", "synthetic-token"),
    ]
    assert sample["failures"] == [] and sample["operations"]["runtime_heartbeat_age_seconds"] == 1


@pytest.mark.parametrize(
    ("duration", "interval", "missed_seconds", "event", "window", "failure"),
    [
        (2, 1, 0, None, "short_observation", None),
        (observer.WEEK_SECONDS, 300, 0, None, "seven_day_observation", None),
        (observer.WEEK_SECONDS, 60, observer.WEEK_SECONDS, None, "gapped_observation", None),
        (4, 1, 0, "readiness", "unhealthy_observation", "ready:OSError"),
        (4, 1, 0, "oom", "unhealthy_observation", "worker:oom_kills_increased"),
        (4, 1, 0, "reset", "unhealthy_observation", "worker:oom_counter_reset"),
        (4, 1, 5, "oom", "gapped_observation", "worker:oom_kills_increased"),
    ],
)
def test_window_result_requires_contiguous_samples(
    monkeypatch,
    tmp_path,
    docker_installation,
    duration,
    interval,
    missed_seconds,
    event,
    window,
    failure,
):
    now = 0
    _, config = docker_installation
    config.oom_kills = 4

    def sleep(seconds):
        nonlocal now
        if now == 0:
            now += missed_seconds
            if event == "oom":
                config.oom_kills += 1
            if event == "reset":
                config.oom_kills = 0
        now += seconds

    def http(url, token=None):
        if url.endswith("/operations"):
            operations = dict.fromkeys(observer.OPERATIONS_FIELDS)
            operations["runtime_heartbeat_age_seconds"] = 2
            return operations, 0.5
        if url.endswith("/ready") and now == interval and event == "readiness":
            raise OSError("Synthetic API outage")
        return {"status": "alive" if url.endswith("/live") else "ready"}, 0.5

    monkeypatch.setattr(
        observer, "time", SimpleNamespace(monotonic=lambda: now, time=lambda: now, sleep=sleep)
    )
    monkeypatch.setattr(observer, "http_json", http)
    monkeypatch.setenv("GA_SOAK_API_KEY", "synthetic-token")
    report = tmp_path / "observed.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "observe_native_soak.py",
            "--compose-project",
            ARGS.compose_project,
            "--url",
            URL,
            "--image-id",
            IMAGE,
            "--revision",
            REVISION,
            "--report",
            str(report),
            "--duration-seconds",
            str(duration),
            "--interval-seconds",
            str(interval),
        ],
    )

    if missed_seconds or event:
        with pytest.raises(SystemExit) as error:
            observer.main()
        assert error.value.code == 1
    else:
        observer.main()
    records = [json.loads(line) for line in report.read_text().splitlines()]
    assert records[0]["kind"] == "begin" and records[-1]["kind"] == "end"
    assert records[-1]["observed_seconds"] >= duration
    assert records[-1]["window"] == window
    assert records[-1]["excessive_sample_gaps"] == int(bool(missed_seconds))
    assert records[-1]["incomplete_samples"] == int(bool(failure))
    assert [issue for sample in records[1:-1] for issue in sample["failures"]] == (
        [failure] if failure else []
    )
