"""Application restart groups retain the durable handoff recovery process."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[1]


def test_handoff_recovery_has_dedicated_process():
    app = json.loads((ROOT / "deploy/pm2/ecosystem.handoff_recovery.json").read_text())["apps"][0]
    assert app["name"] == "parse-handoff-recovery"
    assert app["script"] == ".venv/bin/python"
    assert app["args"] == "-m src.services.handoff_recovery"
    assert app["env"]["MINERU_MODEL_SMALL_BACKEND"] == "onnx"
    assert app["env"]["MINERU_MODEL_ONNX_DEVICE"] == "cpu"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node required for PM2 config")
def test_handoff_recovery_uses_the_same_project_paths():
    script = 'console.log(JSON.stringify(require(process.argv[1]).apps.filter(a=>a.name==="parse-handoff-recovery")))'
    apps = json.loads(
        subprocess.check_output(
            ["node", "-e", script, str(ROOT / "deploy/pm2/ecosystem.config.cjs")], text=True
        )
    )
    assert len(apps) == 1
    assert apps[0]["cwd"] == str(ROOT)
    assert apps[0]["script"] == str(ROOT / ".venv/bin/python")


def test_application_groups_include_handoff_recovery():
    source = (ROOT / "deploy/manage.sh").read_text()
    for line in source.splitlines():
        if line.strip().startswith(("app)", "app4)")):
            assert "parse-handoff-recovery" in line
    assert "handoff-recovery) names=parse-handoff-recovery" in source
