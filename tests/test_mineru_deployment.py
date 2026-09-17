"""Keep the three-GPU deployment from silently reverting to a single replica."""

import json
import os
from pathlib import Path
import shutil
import shlex
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
    mapped = {item["source"] for item in service.get("devices", [])}
    assert {"/dev/nvidia-uvm", "/dev/nvidia-uvm-tools"} <= mapped
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
    # Exercise the launcher without requiring NVIDIA devices on the test host.
    # /dev/null is a real character device; only the hardware paths are replaced.
    repo = tmp_path / "repo"
    launcher = repo / "deploy/mineru-vllm/serve.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        (ROOT / "deploy/mineru-vllm/serve.sh")
        .read_text()
        .replace("/dev/nvidia-uvm-tools", "/dev/null")
        .replace("/dev/nvidia-uvm", "/dev/null")
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\n')
    docker.chmod(0o755)
    capture = tmp_path / "args"
    subprocess.run(
        ["bash", str(launcher), "parallel"],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "CAPTURE": str(capture)},
        check=True,
    )
    args = capture.read_text().splitlines()
    assert str(repo / "compose.mineru.parallel.yaml") in args
    assert "mineru-vlm-parallel" in args
    assert "--exit-code-from" in args
    assert "--abort-on-container-exit" in args
    assert "-d" not in args


def test_launcher_fails_when_uvm_devices_never_appear(tmp_path):
    launcher = tmp_path / "repo/deploy/mineru-vllm/serve.sh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        (ROOT / "deploy/mineru-vllm/serve.sh")
        .read_text()
        .replace("/dev/nvidia-uvm", str(tmp_path / "missing-device"))
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, body in {
        "sleep": '#!/bin/sh\necho waiting >> "$CAPTURE"\n',
        "docker": '#!/bin/sh\necho docker >> "$CAPTURE"\n',
    }.items():
        executable = fake_bin / name
        executable.write_text(body)
        executable.chmod(0o755)
    capture = tmp_path / "calls"
    result = subprocess.run(
        ["bash", str(launcher), "parallel"],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "CAPTURE": str(capture)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 1
    assert "CUDA UVM devices missing" in result.stderr
    assert capture.read_text().splitlines() == ["waiting"] * 59


def test_parse_workers_have_unique_names_and_bounded_concurrency():
    apps = json.loads((ROOT / "ecosystem.two_stage.celery.json").read_text())["apps"]
    parsers = [app for app in apps if app["name"].startswith("celery-two-stage-parse")]
    assert len(parsers) == 3
    names = set()
    for app in parsers:
        args = shlex.split(app["args"])
        names.add(args[args.index("-n") + 1])
        assert args[args.index("-P") + 1] == "solo"
        assert args[args.index("-c") + 1] == "1"
        assert args[args.index("-Q") + 1] == "queue_parse_urgent,queue_parse_gpu"
        assert "--prefetch-multiplier=1" in args
        assert app["kill_timeout"] >= 1900000
        assert app["env"]["MINERU_PROCESSING_WINDOW_SIZE"] == "64"
        assert app["env"]["MINERU_MODEL_VLM_MAX_CONCURRENCY"] == "8"
        assert app["env"]["MINERU_INTRA_OP_NUM_THREADS"] == "16"
        assert app["env"]["MINERU_INTER_OP_NUM_THREADS"] == "1"
    assert len(names) == 3
    api = json.loads((ROOT / "ecosystem.config.json").read_text())["apps"][0]
    assert api["env"]["MINERU_PROCESSING_WINDOW_SIZE"] == "64"
    assert api["env"]["MINERU_INTRA_OP_NUM_THREADS"] == "16"
    assert api["env"]["MINERU_INTER_OP_NUM_THREADS"] == "1"
