"""Exercise the production publisher and task registration with real Redis and solo workers."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time

import pytest
import redis

pytestmark = [
    pytest.mark.mineru_integration,
    pytest.mark.skipif(
        os.environ.get("MINERU_RUN_SHARED_PARSE_REDIS") != "1",
        reason="Set MINERU_RUN_SHARED_PARSE_REDIS=1 to launch an isolated Redis instance.",
    ),
]

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("shared_parse_probe.py").resolve()
QUEUE = "queue_parse_shared"


def _wait_until(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _stop(processes):
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 3
    for process in processes:
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@contextmanager
def _isolated_redis(directory):
    binary = shutil.which("redis-server")
    if binary is None:
        pytest.fail("redis-server is required for the opted-in shared queue regression")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"redis://127.0.0.1:{port}/0"
    with (directory / "redis.log").open("wb") as log:
        process = subprocess.Popen(
            [
                binary,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--dir",
                str(directory),
                "--save",
                "",
                "--appendonly",
                "no",
                "--pidfile",
                str(directory / "redis.pid"),
            ],
            cwd=directory,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    client = redis.Redis.from_url(url, socket_timeout=20, socket_connect_timeout=1)

    def owns_listener():
        if process.poll() is not None:
            pytest.fail(f"Isolated Redis exited; see {directory / 'redis.log'}")
        try:
            return client.info("server")["process_id"] == process.pid
        except redis.ConnectionError:
            return False

    try:
        assert _wait_until(owns_listener), "Failed to start the dedicated test Redis"
        yield url, client
    finally:
        client.close()
        _stop([process])


def _publish(directory, env, spec):
    completed = subprocess.run(
        [sys.executable, str(PROBE), "publish", json.dumps(spec)],
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _event(client, name):
    event = client.blpop(f"probe:{name}", timeout=12)
    assert event is not None, f"No {name} event arrived"
    return event[1].decode()


def _round(directory, env, client, round_number):
    jobs = {}
    workers = []
    log_paths = []
    waiting = [
        ("E", "images", "normal"),
        ("F", "two-stage", None),
        ("X", "parse", "urgent"),
        ("Y", "images", "urgent"),
        ("Z", "two-stage", "urgent"),
    ]
    if round_number == 1:
        # Change urgent arrival order on the restarted consumers.
        waiting[2:] = [waiting[4], waiting[2], waiting[3]]
    try:
        jobs.update(
            _publish(
                directory,
                env,
                [
                    ("A", "parse", None),
                    ("B", "images", None),
                    ("C", "two-stage", None),
                    ("D", "parse", None),
                ],
            )
        )
        assert client.llen(QUEUE + "\x06\x169") == 4
        for index in range(4):
            name = f"probe-{round_number}-{index}"
            log_path = directory / f"{name}.log"
            log_paths.append(log_path)
            with log_path.open("wb") as log:
                workers.append(
                    subprocess.Popen(
                        [sys.executable, str(PROBE), "worker", name],
                        cwd=directory,
                        env={**env, "PYTHONHASHSEED": str(1 + round_number * 19 + index)},
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                )

        assert {_event(client, "started") for _ in range(4)} == set(jobs.values())
        jobs.update(_publish(directory, env, waiting))
        assert client.llen(QUEUE) == 3
        assert client.llen(QUEUE + "\x06\x169") == 2
        assert client.llen("probe:finished") == 0
        assert client.hlen("unacked") == 4

        # Each release makes exactly one execution unit available. The other
        # tasks remain blocked so log/CPU races cannot reorder these observations.
        expected = [name for name, _, _ in waiting[2:]] + ["E"]
        for blocker, next_up in zip("ABCD", expected, strict=True):
            client.rpush(f"probe:release:{jobs[blocker]}", "go")
            assert _event(client, "finished") == jobs[blocker]
            assert _event(client, "started") == jobs[next_up], f"Expected {next_up}"
        # E began while all urgent tasks were still running. Release one of
        # them so the remaining normal task can start, then drain this round.
        first_urgent = expected[0]
        client.rpush(f"probe:release:{jobs[first_urgent]}", "go")
        assert _event(client, "finished") == jobs[first_urgent]
        assert _event(client, "started") == jobs["F"]
        remaining = expected[1:] + ["F"]
        for name in remaining:
            client.rpush(f"probe:release:{jobs[name]}", "go")
        assert {_event(client, "finished") for _ in remaining} == {jobs[name] for name in remaining}
        assert _wait_until(lambda: client.hlen("unacked") == 0)
        assert client.llen(QUEUE) == client.llen(QUEUE + "\x06\x169") == 0
        assert client.llen("probe:started") == 0
    except AssertionError as exc:
        logs = "\n".join(f"{path.name}:\n{path.read_text()[-3000:]}" for path in log_paths)
        raise AssertionError(f"{exc}\n{logs}") from exc
    finally:
        for job_id in jobs.values():
            client.rpush(f"probe:release:{job_id}", "go")
        _stop(workers)


def test_urgent_outranks_waiting_normal_across_modes_and_worker_restarts(tmp_path):
    secrets = tmp_path / ".secrets"
    secrets.mkdir()
    shutil.copyfile(REPO / "deploy/secrets.example.toml", secrets / "secrets.toml")
    with _isolated_redis(tmp_path) as (url, client):
        # Launch outside the checkout using only public config and explicit test
        # endpoints. Do not inherit private runtime config or broker credentials.
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(REPO),
            "PYTHON_DOTENV_DISABLED": "1",
            "CELERY_BROKER_URL": url,
            "CELERY_RESULT_BACKEND": url,
            "CELERY_SHARED_PARSE_QUEUE": QUEUE,
            # Legacy vision modules construct provider clients at import.
            # Stub values are never used to make requests.
            "GENIMI_API_KEY": "unused-test-key",
            "OPENAI_API_KEY": "unused-test-key",
            "MINERU_JOB_STORE_DIR": str(tmp_path / "jobs"),
            "MINERU_PARSE_SLOT_DIR": str(tmp_path / "parse-slots"),
            "VLLM_VISION_SLOT_DIR": str(tmp_path / "vision-slots"),
        }
        _round(tmp_path, env, client, round_number=0)
        _round(tmp_path, env, client, round_number=1)
