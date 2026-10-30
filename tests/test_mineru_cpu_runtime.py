"""Exercise real MinerU configuration in fresh processes without loading models."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]


def _run(tmp_path, script, overrides=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("MINERU_")}
    config = tmp_path / "config.yaml"
    config.write_text("model:\n  small_backend: auto\n  onnx_device: auto\n")
    env.update(MINERU_CONFIG=str(config), MINERU_HOME=str(tmp_path / "home"))
    env.update(overrides or {})
    return subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("tier", ["flash", "basic", "standard", "advanced"])
def test_every_tier_defaults_to_cpu_before_entering_parser(tmp_path, tier):
    result = _run(
        tmp_path,
        f"""
import json, sys
from pathlib import Path
from src.services import mineru_service_full as service
from mineru.config import get_config
class Observed(Exception): pass
def observe(*args, **kwargs):
    config = get_config()
    print(json.dumps([config.model.small_backend, config.model.onnx_device]))
    raise Observed
service.mineru_parse = observe
try:
    service.parse_doc([Path(sys.argv[1]) / 'sample.pdf'], Path(sys.argv[1]) / 'result', tier={tier!r})
except Observed:
    pass
""",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == ["onnx", "cpu"]


@pytest.mark.parametrize(
    "override",
    [
        {"MINERU_MODEL_ONNX_DEVICE": "gpu"},
        {"MINERU_MODEL_ONNX_DEVICE": "auto"},
        {"MINERU_MODEL_SMALL_BACKEND": "torch"},
    ],
)
def test_explicit_non_cpu_execution_is_rejected_before_parser(tmp_path, override):
    result = _run(
        tmp_path,
        """
import sys
from pathlib import Path
from src.services import mineru_service_full as service
def unexpected(*args, **kwargs):
    raise AssertionError('Parser must not execute for an unsupported device')
service.mineru_parse = unexpected
try:
    service.parse_doc([Path(sys.argv[1]) / 'sample.pdf'], Path(sys.argv[1]) / 'result', tier='basic')
except RuntimeError as error:
    assert 'CPU' in str(error)
else:
    raise AssertionError('Invalid execution configuration was accepted')
""",
        override,
    )
    assert result.returncode == 0, result.stderr


def test_sdk_config_cached_before_application_cannot_enable_auto_gpu(tmp_path):
    result = _run(
        tmp_path,
        """
import os, sys
from pathlib import Path
from mineru.config import get_config
assert get_config().model.onnx_device == 'auto'
os.environ['MINERU_MODEL_ONNX_DEVICE'] = 'cpu'
from src.services import mineru_service_full as service
try:
    service.parse_doc([Path(sys.argv[1]) / 'sample.pdf'], Path(sys.argv[1]) / 'result', tier='flash')
except RuntimeError as error:
    assert 'CPU' in str(error)
else:
    raise AssertionError('Previously cached non-CPU configuration was accepted')
""",
    )
    assert result.returncode == 0, result.stderr
