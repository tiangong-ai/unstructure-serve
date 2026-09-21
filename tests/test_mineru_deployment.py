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
            str(ROOT / "deploy/mineru-vllm/compose.mineru.yaml"),
            "-f",
            str(ROOT / "deploy/mineru-vllm/compose.mineru.parallel.yaml"),
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
    apps = json.loads((ROOT / "deploy/pm2/ecosystem.vllm.parallele.config.json").read_text())[
        "apps"
    ]
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
    assert str(repo / "deploy/mineru-vllm/compose.mineru.parallel.yaml") in args
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
    apps = json.loads((ROOT / "deploy/pm2/ecosystem.two_stage.celery.json").read_text())["apps"]
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
    api = json.loads((ROOT / "deploy/pm2/ecosystem.config.json").read_text())["apps"][0]
    assert api["env"]["MINERU_PROCESSING_WINDOW_SIZE"] == "64"
    assert api["env"]["MINERU_INTRA_OP_NUM_THREADS"] == "16"
    assert api["env"]["MINERU_INTER_OP_NUM_THREADS"] == "1"


def test_ordinary_worker_does_not_steal_two_stage_merge_tasks():
    ordinary = json.loads((ROOT / "deploy/pm2/ecosystem.celery.json").read_text())["apps"]
    staged = json.loads((ROOT / "deploy/pm2/ecosystem.two_stage.celery.json").read_text())["apps"]

    def queues(apps):
        result = set()
        for app in apps:
            args = shlex.split(app["args"])
            result.update(args[args.index("-Q") + 1].split(","))
        return result

    assert not (queues(ordinary) & queues(staged))


def test_api_stop_window_covers_gunicorn_graceful_timeout(monkeypatch):
    import runpy

    monkeypatch.setenv("API_WORKERS", "4")
    monkeypatch.setenv("API_WORKER_TIMEOUT", "1900")
    monkeypatch.setenv("API_MAX_REQUESTS", "5000")
    config = runpy.run_path(str(ROOT / "deploy/gunicorn.conf.py"))
    api = json.loads((ROOT / "deploy/pm2/ecosystem.config.json").read_text())["apps"][0]
    assert config["worker_class"] == "uvicorn_worker.UvicornWorker"
    assert config["preload_app"] is False
    assert config["workers"] == 4
    assert config["max_requests"] == 5000
    assert api["kill_timeout"] >= config["graceful_timeout"] * 1000


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required for PM2 config")
def test_pm2_config_has_absolute_paths_from_any_directory(tmp_path):
    config = ROOT / "deploy/pm2/ecosystem.config.cjs"
    result = subprocess.run(
        ["node", "-e", "console.log(JSON.stringify(require(process.argv[1])))", str(config)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    apps = json.loads(result.stdout)["apps"]
    assert len({app["name"] for app in apps}) == len(apps)
    for app in apps:
        assert app["cwd"] == str(ROOT)
        assert Path(app["script"]).is_absolute()
        assert Path(app["out_file"]).is_absolute()


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required by PM2 launcher")
@pytest.mark.parametrize("group", ["app", "vision-health"])
def test_manage_starts_shared_vision_monitor(tmp_path, group):
    fake = tmp_path / "pm2"
    fake.write_text("""#!/usr/bin/python3
import json,os,sys
if sys.argv[1]=='jlist': print('[]')
else:
 with open(os.environ['TEST_PM2_CAPTURE'],'w') as f: json.dump(sys.argv[1:],f)
""")
    fake.chmod(0o755)
    capture = tmp_path / "calls.json"
    subprocess.run(
        ["bash", str(ROOT / "deploy/manage.sh"), "start", group],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f'{tmp_path}:{os.environ["PATH"]}',
            "TEST_PM2_CAPTURE": str(capture),
        },
        check=True,
        capture_output=True,
    )
    names = json.loads(capture.read_text())[-1].split(",")
    assert "vision-health-monitor" in names
    assert "mineru-vlm-docker-parallel" not in names
    app = json.loads((ROOT / "deploy/pm2/ecosystem.vision_health.json").read_text())["apps"][0]
    assert app["args"] == "-m src.services.vision_health"
    assert "VLLM_BASE_URLS" not in app.get("env", {})


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required by PM2 launcher")
@pytest.mark.parametrize("state", ["online", "launching", "stopped"])
def test_manage_start_does_not_restart_healthy_api(tmp_path, state):
    fake = tmp_path / "pm2"
    fake.write_text("""#!/usr/bin/python3
import json,os,sys
if sys.argv[1]=='jlist':
 print(json.dumps([{'name':'unstructured-gunicorn','pm2_env':{'status':os.environ['TEST_PM2_STATE']}}]))
else:
 with open(os.environ['TEST_PM2_CAPTURE'],'w') as f: json.dump(sys.argv[1:],f)
""")
    fake.chmod(0o755)
    capture = tmp_path / "calls.json"
    subprocess.run(
        ["bash", str(ROOT / "deploy/manage.sh"), "start", "api"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f'{tmp_path}:{os.environ["PATH"]}',
            "TEST_PM2_STATE": state,
            "TEST_PM2_CAPTURE": str(capture),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    if state in {"online", "launching"}:
        assert not capture.exists()
    else:
        args = json.loads(capture.read_text())
        assert args == [
            "start",
            str(ROOT / "deploy/pm2/ecosystem.config.cjs"),
            "--only",
            "unstructured-gunicorn",
        ]
