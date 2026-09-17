"""Keep the three-GPU deployment from silently reverting to a single replica."""

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose CLI required")
def test_parallel_compose_exposes_three_gpus_and_internal_load_balancing():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("MINERU_", "COMPOSE_"))
    }
    env["MINERU_DOCKER_GPU_ID"] = "0"  # Legacy single-GPU setting must not win.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(ROOT / "compose.mineru.yaml"),
            "-f",
            str(ROOT / "compose.mineru.parallel.yaml"),
            "config",
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    services = json.loads(result.stdout)["services"]
    assert set(services) == {"mineru-vlm"}
    service = services["mineru-vlm"]
    devices = service["deploy"]["resources"]["reservations"]["devices"]
    assert len(devices) == 1
    assert devices[0]["device_ids"] == ["0", "1", "2"]
    command = service["command"]
    assert command[command.index("--data-parallel-size") + 1] == "3"
    assert command[command.index("--tensor-parallel-size") + 1] == "1"
    assert "--data-parallel-rank" not in command
    assert "--data-parallel-hybrid-lb" not in command
    assert "--headless" not in command


def test_pm2_parallel_runs_one_foreground_compose_service():
    apps = json.loads((ROOT / "ecosystem.vllm.parallele.config.json").read_text())["apps"]
    assert len(apps) == 1
    app = apps[0]
    assert app["name"] == "mineru-vlm-docker-parallel"
    assert app["script"] == "deploy/mineru-vllm/serve.sh"
    assert app["args"] == "parallel"
    assert app["kill_timeout"] >= 70000


def test_launcher_resolves_repo_and_keeps_compose_attached(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
    docker.chmod(0o755)
    capture = tmp_path / "args"
    subprocess.run(
        ["bash", str(ROOT / "deploy/mineru-vllm/serve.sh"), "parallel"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "CAPTURE": str(capture)},
        check=True,
    )
    args = capture.read_text().splitlines()
    assert str(ROOT / "compose.mineru.parallel.yaml") in args
    assert "mineru-vlm-parallel" in args
    assert "--exit-code-from" in args
    assert "--abort-on-container-exit" in args
    assert "-d" not in args
