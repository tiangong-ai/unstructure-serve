"""Checkpoints must survive failures, duplicates and obsolete queue messages."""

import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.services import job_store


def _pipeline():
    return importlib.import_module("src.services.durable_pipeline")


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    monkeypatch.setenv("MINERU_JOB_STORE_DIR", str(tmp_path / "jobs"))
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"private-pdf")
    calls = {"parse": 0, "vision": []}

    def fake_isolated(_target, source, output, *, hard_timeout, backend):
        calls["parse"] += 1
        directory = Path(output) / "sample" / "advanced"
        directory.mkdir(parents=True)
        (directory / "middle_json.json").write_text('{"pages": [0, 1]}')
        for number in (1, 2):
            (directory / f"{number}.png").write_bytes(bytes([number]) * 20)
        return (
            [
                {"type": "text", "text": "title", "text_level": 1, "page_idx": 0},
                {"type": "image", "img_path": "1.png", "img_caption": ["first"], "page_idx": 0},
                {"type": "image", "img_path": "2.png", "img_caption": ["second"], "page_idx": 1},
                {"type": "text", "text": "end", "page_idx": 1},
            ],
            str(directory),
            None,
        )

    def vision(path, *_args, **_kwargs):
        calls["vision"].append(Path(path).name)
        return "visible value: 42 kg"

    def make(mode="images"):
        pipeline = _pipeline()
        monkeypatch.setattr(pipeline, "run_isolated_call", fake_isolated)
        monkeypatch.setattr(pipeline, "vision_completion", vision)
        record = job_store.create_job(
            mode, source, {"backend": "advanced", "chunk_type": True, "return_txt": True}
        )
        return pipeline, {"job_id": record["job_id"], "generation": 0}, calls

    return make


def test_images_replay_reuses_durable_parse_and_all_image_results(prepared):
    pipeline, ref, calls = prepared()
    first = pipeline.run_durable_job(ref)
    second = pipeline.run_durable_job(ref)
    assert first == second == job_store.result_reference(ref["job_id"])
    assert calls["parse"] == 1
    assert sorted(calls["vision"]) == ["1.png", "2.png"]
    result = job_store.read_result(ref["job_id"])
    assert [item["page_number"] for item in result["result"]] == [1, 1, 2, 2]
    assert len([item for item in result["result"] if item["type"] == "image"]) == 2
    assert job_store.source_path(ref["job_id"]).is_file()


def test_vision_failure_resumes_only_missing_image(prepared, monkeypatch):
    pipeline, ref, calls = prepared()
    monkeypatch.setenv("VISION_BATCH_SIZE", "1")
    initial_vision = pipeline.vision_completion

    def fail_second(path, *args, **kwargs):
        if Path(path).name == "2.png":
            raise TimeoutError("endpoint interrupted")
        return initial_vision(path, *args, **kwargs)

    monkeypatch.setattr(pipeline, "vision_completion", fail_second)
    with pytest.raises(TimeoutError):
        pipeline.run_durable_job(ref)
    assert job_store.status(ref["job_id"])["state"] == "FAILURE"
    resumed = job_store.resume_job(ref["job_id"])
    monkeypatch.setattr(pipeline, "vision_completion", initial_vision)
    pipeline.run_durable_job({"job_id": ref["job_id"], "generation": resumed["generation"]})
    assert calls["parse"] == 1
    assert sorted(calls["vision"]) == ["1.png", "2.png"]


def test_obsolete_generation_cannot_parse_or_write_failure(prepared):
    pipeline, ref, calls = prepared()
    record = job_store.resume_job(ref["job_id"])
    assert pipeline.run_durable_job(ref).get("stale") is True
    assert pipeline.ensure_parsed(ref).get("stale") is True
    assert pipeline.run_vision({**ref, "seq": 1}).get("stale") is True
    assert calls == {"parse": 0, "vision": []}
    assert job_store.read_job(ref["job_id"]) == record


def test_result_publication_failure_preserves_parse_and_vision(prepared, monkeypatch):
    pipeline, ref, calls = prepared()
    original = job_store.save_result
    monkeypatch.setattr(
        job_store, "save_result", lambda *args: (_ for _ in ()).throw(OSError("disk"))
    )
    with pytest.raises(OSError):
        pipeline.run_durable_job(ref)
    assert job_store.source_path(ref["job_id"]).is_file()
    resumed = job_store.resume_job(ref["job_id"])
    monkeypatch.setattr(job_store, "save_result", original)
    pipeline.run_durable_job({"job_id": ref["job_id"], "generation": resumed["generation"]})
    assert calls["parse"] == 1
    assert sorted(calls["vision"]) == ["1.png", "2.png"]


def test_plain_parse_preserves_existing_image_type_contract(prepared):
    pipeline, ref, calls = prepared("parse")
    pipeline.run_durable_job(ref)
    result = job_store.read_result(ref["job_id"])
    assert result["result"][0]["type"] == "title"
    assert result["result"][1].get("type") is None
    assert calls["vision"] == []


def test_missing_parse_asset_is_not_reused_or_silently_skipped(prepared):
    pipeline, ref, calls = prepared()
    pipeline.ensure_parsed(ref)
    next(job_store.job_dir(ref["job_id"]).rglob("1.png")).unlink()
    with pytest.raises((RuntimeError, FileNotFoundError)):
        pipeline.run_durable_job(ref)
    assert calls["parse"] == 1
    assert calls["vision"] == []


def test_duplicate_image_messages_make_only_one_model_request(prepared, monkeypatch):
    pipeline, ref, calls = prepared()
    pipeline.ensure_parsed(ref)
    entered, release = threading.Event(), threading.Event()
    original = pipeline.vision_completion

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "vision_completion", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(pipeline.run_vision, {**ref, "seq": 1})
        assert entered.wait(5)
        second = pool.submit(pipeline.run_vision, {**ref, "seq": 1})
        with pytest.raises(job_store.JobBusy):
            job_store.resume_job(ref["job_id"])
        release.set()
        assert first.result() == second.result() == {**ref, "seq": 1}
    assert calls["vision"] == ["1.png"]


def test_two_stage_task_branches_use_references_only(prepared, monkeypatch):
    pipeline, ref, calls = prepared("two-stage")
    from src.services import two_stage_pipeline as tasks

    monkeypatch.setattr(tasks, "_build_image_jobs", pipeline._all_image_jobs)
    assert tasks.parse_task.run(ref) == ref
    assert tasks.vision_task.run({**ref, "seq": 1}) == {**ref, "seq": 1}
    assert pipeline.pending_vision(ref, limit=32) == [2]
    tasks.vision_task.run({**ref, "seq": 2})
    assert tasks.merge_task.run([], ref) == job_store.result_reference(ref["job_id"])
    assert calls["parse"] == 1


def test_dispatch_bounds_each_wave_and_preserves_urgent_queues(prepared, monkeypatch):
    pipeline, ref, _ = prepared("two-stage")
    from src.services import two_stage_pipeline as tasks

    monkeypatch.setattr(tasks, "_build_image_jobs", pipeline._all_image_jobs)
    pipeline.ensure_parsed(ref)
    monkeypatch.setenv("MINERU_VISION_WAVE_SIZE", "1")
    replacements = []

    from celery.exceptions import Ignore

    class Replaced(Ignore):
        pass

    def replace(signature):
        replacements.append(signature)
        raise Replaced

    monkeypatch.setattr(tasks.dispatch, "replace", replace)
    with pytest.raises(Replaced):
        tasks.dispatch.run(
            ref, vision_queue="vision_u", merge_queue="merge_u", dispatch_queue="dispatch_u"
        )
    wave = replacements[0]
    assert len(wave.tasks) == 1
    assert wave.tasks[0].args == ({**ref, "seq": 1},)
    assert wave.tasks[0].options["queue"] == "vision_u"
    assert wave.body.task == "two_stage.dispatch"
    assert wave.body.immutable is True
    assert wave.body.args == (ref,)
    assert wave.body.options["queue"] == "dispatch_u"
    tasks.vision_task.run({**ref, "seq": 1})
    with pytest.raises(Replaced):
        tasks.dispatch.run(
            ref, vision_queue="vision_u", merge_queue="merge_u", dispatch_queue="dispatch_u"
        )
    assert replacements[1].tasks[0].args == ({**ref, "seq": 2},)


def test_submit_uses_stable_final_id_and_small_message(prepared, monkeypatch):
    _, ref, _ = prepared("two-stage")
    from src.services import two_stage_pipeline as tasks
    from celery.canvas import _chain
    from types import SimpleNamespace

    captured = []

    def publish(workflow, **_kwargs):
        captured.append(workflow)
        return SimpleNamespace(id=workflow.tasks[-1].options["task_id"])

    monkeypatch.setattr(_chain, "apply_async", publish)
    result = tasks.submit_durable_two_stage(ref, parse_queue="parse_u", dispatch_queue="dispatch_u")
    assert result.id == ref["job_id"]
    assert captured[0].tasks[0].args == (ref,)
    assert captured[0].tasks[0].options["queue"] == "parse_u"
    assert captured[0].tasks[-1].options["queue"] == "dispatch_u"


@pytest.mark.parametrize("change", ["prompt", "model", "sampling"])
def test_execution_profile_changes_cannot_mix_checkpoint_results(prepared, monkeypatch, change):
    pipeline, ref, calls = prepared()
    pipeline.ensure_parsed(ref)
    pipeline.run_vision({**ref, "seq": 1})
    if change == "prompt":
        from src.services import vision_prompts

        monkeypatch.setattr(vision_prompts, "DEFAULT_VISION_PROMPT", "changed prompt")
    elif change == "model":
        from src.services import vision_service

        original = vision_service._resolve_model
        monkeypatch.setattr(
            vision_service, "_resolve_model", lambda *args: original(*args) + "-new"
        )
    else:
        monkeypatch.setenv("VLLM_VISION_TEMPERATURE", "0.456")
    with pytest.raises(RuntimeError, match="profile"):
        pipeline.run_vision({**ref, "seq": 2})
    assert calls["vision"] == ["1.png"]
    with pytest.raises(RuntimeError, match="profile"):
        pipeline.ensure_parsed(ref)
    with pytest.raises(RuntimeError, match="profile"):
        pipeline.assemble(ref)
    assert calls["parse"] == 1


def test_changed_vision_text_fails_integrity_check(prepared):
    pipeline, ref, _ = prepared()
    pipeline.ensure_parsed(ref)
    pipeline.run_vision({**ref, "seq": 1})
    path = job_store.job_dir(ref["job_id"]) / "vision" / "1.json"
    result = job_store.read_json(path)
    result["vision_text"] = "corrupted value"
    job_store.atomic_json(path, result)
    with pytest.raises(RuntimeError, match="vision result"):
        pipeline.run_vision({**ref, "seq": 1})


def test_wave_publication_failure_is_visible_in_durable_status(prepared, monkeypatch):
    pipeline, ref, _ = prepared("two-stage")
    from src.services import two_stage_pipeline as tasks

    monkeypatch.setattr(tasks, "_build_image_jobs", pipeline._all_image_jobs)
    pipeline.ensure_parsed(ref)

    def fail_publish(_signature):
        raise OSError("broker publication interrupted")

    monkeypatch.setattr(tasks.dispatch, "replace", fail_publish)
    with pytest.raises(OSError):
        tasks.dispatch.run(ref)
    assert job_store.status(ref["job_id"])["state"] == "FAILURE"
    assert job_store.source_path(ref["job_id"]).is_file()


def test_resumed_checkpoint_is_started_without_erasing_other_stage_failure(prepared):
    pipeline, ref, _ = prepared()
    pipeline.ensure_parsed(ref)
    job_store.fail_job(ref["job_id"], RuntimeError("interrupted"))
    record = job_store.resume_job(ref["job_id"])
    resumed = {"job_id": ref["job_id"], "generation": record["generation"]}
    pipeline.ensure_parsed(resumed)
    assert job_store.status(ref["job_id"])["state"] == "STARTED"
    job_store.fail_job(ref["job_id"], RuntimeError("another image failed"))
    pipeline.run_vision({**resumed, "seq": 1})
    assert job_store.status(ref["job_id"])["state"] == "FAILURE"


def test_completed_ordinary_images_reports_complete_image_progress(prepared):
    pipeline, ref, _ = prepared()
    pipeline.run_durable_job(ref)
    assert job_store.status(ref["job_id"])["progress"] == {
        "images_total": 2,
        "images_completed": 2,
    }
