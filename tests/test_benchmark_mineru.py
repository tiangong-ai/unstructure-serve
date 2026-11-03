"""Benchmark contracts that protect tier comparisons and worker shutdown."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
from PIL import Image

from src.scripts import benchmark_mineru as benchmark


@pytest.fixture(autouse=True)
def isolated_benchmark_worker_environment(monkeypatch):
    # Mocked executors run worker initialization in pytest's own process.
    # Register both keys before its direct assignments so teardown restores
    # the caller's original values, including originally absent variables.
    monkeypatch.setenv("MINERU_MODEL_VLM_MAX_CONCURRENCY", "4")
    monkeypatch.setenv("MINERU_PROCESSING_WINDOW_SIZE", "32")


def test_runtime_metadata_records_effective_config_without_connection_secrets(monkeypatch):
    config = SimpleNamespace(
        model=SimpleNamespace(
            small_backend="onnx",
            onnx_device="cpu",
            vlm=SimpleNamespace(
                engine="auto",
                model="parser-model",
                max_concurrency=8,
                http_timeout=600,
                server_url="https://private-model-endpoint.example",
                api_key="private-api-key",
            ),
        )
    )
    monkeypatch.setitem(
        sys.modules,
        "src.services.mineru_service_full",
        SimpleNamespace(get_mineru_config=lambda: config),
    )
    monkeypatch.setattr(benchmark, "version", lambda name: "test-version")
    metadata = benchmark._runtime_metadata()
    assert metadata["small_backend"] == "onnx"
    assert metadata["onnx_device"] == "cpu"
    assert metadata["vlm_max_concurrency"] == 8
    assert metadata["packages"]["docvortex"] == "test-version"
    serialized = json.dumps(metadata)
    assert "private-api-key" not in serialized
    assert "private-model-endpoint" not in serialized


class Barrier:
    def __init__(self):
        self.aborted = False
        self.waited = []

    def wait(self, timeout):
        self.waited.append(timeout)

    def abort(self):
        self.aborted = True


@pytest.mark.parametrize("tier", ["standard", "advanced"])
def test_parse_uses_selected_tier_and_checks_materialized_assets(monkeypatch, tmp_path, tier):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"a stable private input")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "middle_json.json").write_text(
        json.dumps({"is_full_document": True, "pages": [{"page_idx": 0}, {"page_idx": 1}]})
    )
    Image.new("RGB", (3, 4)).save(artifacts / "figure.png")
    calls = []

    def parse_doc(paths, output, *, tier):
        calls.append((paths, output, tier))
        return [{"img_path": "figure.png"}], str(artifacts), None

    class Document:
        def __init__(self, path):
            assert path == str(source)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def __len__(self):
            return 2

    monkeypatch.setitem(
        sys.modules, "src.services.mineru_service_full", SimpleNamespace(parse_doc=parse_doc)
    )
    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=Document))
    monkeypatch.setattr(benchmark, "_runtime_metadata", lambda: {"python": "test"})
    result = benchmark._parse((str(source), str(tmp_path / "result"), 0.0, tier))
    assert calls == [([source], tmp_path / "result", tier)]
    assert result["tier"] == tier
    assert result["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result["pages"] == 2
    assert result["decoded_images"] == 1
    assert result["parse_seconds"] <= result["service_seconds"]

    # A nonempty file is insufficient: damaged pixels must fail the benchmark.
    (artifacts / "figure.png").write_bytes(b"not a decodable image")
    with pytest.raises(OSError):
        benchmark._parse((str(source), str(tmp_path / "result"), 0.0, tier))


@pytest.mark.parametrize("arguments,tier", [([], "advanced"), (["--tier", "standard"], "standard")])
def test_cli_applies_tier_to_warmup_jobs_and_report(monkeypatch, tmp_path, arguments, tier):
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"source")
    output = tmp_path / "benchmark"
    init_calls = []
    jobs = []

    class Future:
        def result(self, timeout):
            return 123

    class Executor:
        def __init__(self, *, max_workers, mp_context, initializer, initargs):
            assert max_workers == 1
            init_calls.append(initargs)
            initializer(*initargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, function):
            function()
            return Future()

        def map(self, function, values):
            return [function(value) for value in values]

    def parse(job):
        jobs.append(job)
        return {
            "pid": 123,
            "tier": job[3],
            "pages": 1,
            "source_sha256": "test-hash",
            "service_seconds": 0.1,
            "completion_seconds": 0.2,
            "runtime": {"python": "test", "packages": {"mineru": "test"}},
        }

    monkeypatch.setattr(benchmark, "ProcessPoolExecutor", Executor)
    monkeypatch.setattr(benchmark, "_parse", parse)
    monkeypatch.setattr(benchmark, "_cleanup_worker", lambda: None)
    monkeypatch.setattr(
        benchmark.multiprocessing,
        "get_context",
        lambda method: SimpleNamespace(Barrier=lambda count: Barrier()),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark_mineru",
            "--files",
            str(source),
            "--warmup",
            str(source),
            "--jobs",
            "2",
            "--output",
            str(output),
            *arguments,
        ],
    )
    benchmark.main()
    assert init_calls[0][-1] == tier
    assert len(jobs) == 3  # one warmup, then two measured jobs
    assert all(job[3] == tier for job in jobs)
    report = json.loads((output / "report.json").read_text())
    assert report["summary"]["tier"] == tier
    assert report["summary"]["warmup"]["parses_per_worker"] == 1
    assert len(report["warmup_jobs"]) == 1
    assert len(report["jobs"]) == 2


def test_invalid_tier_fails_before_creating_output_or_workers(monkeypatch, tmp_path):
    output = tmp_path / "benchmark"
    monkeypatch.setattr(
        sys, "argv", ["benchmark_mineru", "--output", str(output), "--tier", "auto"]
    )
    with pytest.raises(SystemExit) as exc:
        benchmark.main()
    assert exc.value.code == 2
    assert not output.exists()


@pytest.mark.parametrize("sdk_fails", [False, True])
def test_cleanup_closes_sdk_before_renderer_even_after_sdk_error(monkeypatch, sdk_fails):
    events = []

    def close_sdk():
        events.append("sdk")
        if sdk_fails:
            raise RuntimeError("private upstream details")

    monkeypatch.setitem(
        sys.modules, "mineru.model.vlm.runtime", SimpleNamespace(shutdown_cached_models=close_sdk)
    )
    monkeypatch.setitem(
        sys.modules,
        "docvortex.document.pdf.images",
        SimpleNamespace(shutdown_pdf_render_executor=lambda: events.append("renderer")),
    )
    benchmark._cleanup_worker()
    assert events == ["sdk", "renderer"]


def test_cleanup_does_not_import_unused_model_or_renderer(monkeypatch):
    names = ("mineru.model.vlm.runtime", "docvortex.document.pdf.images")
    for name in names:
        monkeypatch.delitem(sys.modules, name, raising=False)
    benchmark._cleanup_worker()
    assert all(name not in sys.modules for name in names)


def test_warmup_failure_aborts_peers_and_cleans_loaded_resources(monkeypatch, tmp_path):
    events = []
    barrier = Barrier()

    def failed_parse(job):
        assert job[3] == "basic"
        raise ValueError("warmup validation failed")

    monkeypatch.setattr(benchmark, "_parse", failed_parse)
    monkeypatch.setattr(benchmark, "_cleanup_worker", lambda: events.append("cleanup"))
    with pytest.raises(ValueError, match="warmup validation failed"):
        benchmark._init_worker(8, 64, "input.pdf", str(tmp_path), barrier, "basic")
    assert barrier.aborted
    assert events == ["cleanup"]
