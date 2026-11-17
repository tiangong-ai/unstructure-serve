"""Checkpoints must survive failures, duplicates and obsolete queue messages."""

import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.services import job_store
from src.utils.text_output import UNRECOGNIZED_IMAGE_TEXT, UnusableVisionOutput


def _pipeline():
    return importlib.import_module("src.services.durable_pipeline")


def test_conversion_tempdir_keeps_default_host_parse_capacity(monkeypatch, tmp_path):
    import os
    import tempfile

    from src.services import parse_capacity

    pipeline = _pipeline()
    shared_directory = tmp_path / "host-capacity"
    monkeypatch.delenv("MINERU_PARSE_SLOT_DIR", raising=False)
    monkeypatch.setenv("MINERU_PARSE_SLOTS", "1")
    monkeypatch.setattr(parse_capacity, "_DEFAULT_SLOT_DIRECTORY", shared_directory, raising=False)
    source = tmp_path / "sample.pdf"
    source.write_bytes(b"private-pdf")
    attempt = tmp_path / "attempt"
    original_tempdir = tempfile.tempdir

    def parsed(*_args, **_kwargs):
        assert Path(tempfile.gettempdir()).is_relative_to(attempt)
        with parse_capacity.parse_slot(wait_seconds=0.1):
            lock_paths = [Path(os.readlink(f"/proc/self/fd/{fd}")) for fd in parse_capacity._fds]
            assert lock_paths
            assert all(path.parent == shared_directory for path in lock_paths)
        return [], str(attempt), None

    monkeypatch.setattr(pipeline, "parse_doc", parsed)
    assert pipeline._parse_input(source, attempt, backend="advanced") == ([], str(attempt), None)
    assert tempfile.tempdir == original_tempdir


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


def test_unrecognized_image_is_checkpointed_without_failing_document(prepared, monkeypatch):
    pipeline, ref, calls = prepared()
    monkeypatch.setenv("VISION_BATCH_SIZE", "1")
    initial_vision = pipeline.vision_completion
    attempts = []

    def maybe_unrecognized(path, *args, **kwargs):
        attempts.append(Path(path).name)
        if Path(path).name == "2.png":
            raise UnusableVisionOutput("no usable facts")
        return initial_vision(path, *args, **kwargs)

    monkeypatch.setattr(pipeline, "vision_completion", maybe_unrecognized)
    pipeline.run_durable_job(ref)
    result = job_store.read_result(ref["job_id"])
    marked = [item for item in result["result"] if UNRECOGNIZED_IMAGE_TEXT in item["text"]]
    assert job_store.status(ref["job_id"])["state"] == "SUCCESS"
    assert len(marked) == 1 and marked[0]["page_number"] == 2
    assert UNRECOGNIZED_IMAGE_TEXT in result["txt"]
    checkpoint = job_store.read_json(job_store.job_dir(ref["job_id"]) / "vision/2.json")
    assert checkpoint["vision_text"] == UNRECOGNIZED_IMAGE_TEXT
    pipeline.run_durable_job(ref)
    assert attempts == ["1.png", "2.png"]
    assert calls["parse"] == 1


def test_incomplete_reasoning_fails_without_checkpoint_and_resumes(prepared, monkeypatch):
    pipeline, ref, calls = prepared()
    monkeypatch.setenv("VISION_BATCH_SIZE", "1")
    initial_vision = pipeline.vision_completion
    attempts = []

    def incomplete_second(path, *args, **kwargs):
        attempts.append(Path(path).name)
        if Path(path).name == "2.png":
            return "<think>unfinished reasoning"
        return initial_vision(path, *args, **kwargs)

    monkeypatch.setattr(pipeline, "vision_completion", incomplete_second)
    with pytest.raises(RuntimeError, match="reasoning"):
        pipeline.run_durable_job(ref)
    assert job_store.status(ref["job_id"])["state"] == "FAILURE"
    vision_directory = job_store.job_dir(ref["job_id"]) / "vision"
    completed = (vision_directory / "1.json").read_bytes()
    assert not (vision_directory / "2.json").exists()

    resumed = job_store.resume_job(ref["job_id"])
    monkeypatch.setattr(pipeline, "vision_completion", initial_vision)
    pipeline.run_durable_job({"job_id": ref["job_id"], "generation": resumed["generation"]})
    assert job_store.status(ref["job_id"])["state"] == "SUCCESS"
    assert (vision_directory / "1.json").read_bytes() == completed
    assert UNRECOGNIZED_IMAGE_TEXT not in job_store.read_result(ref["job_id"])["txt"]
    assert attempts == ["1.png", "2.png"]
    assert calls == {"parse": 1, "vision": ["1.png", "2.png"]}


def test_version_one_checkpoints_resume_with_unrecognized_marker(prepared, monkeypatch):
    pipeline, ref, calls = prepared()
    pipeline.ensure_parsed(ref)
    path = job_store.job_dir(ref["job_id"]) / "parse/manifest.json"
    manifest = job_store.read_json(path)
    legacy_settings = {**manifest["profile"]["settings"], "version": 1}
    manifest["profile"] = {
        "settings": legacy_settings,
        "sha256": pipeline._value_digest(legacy_settings),
    }
    job_store.atomic_json(path, manifest)
    pipeline.run_vision({**ref, "seq": 1})
    job_store.fail_job(ref["job_id"], RuntimeError("old image failed"))
    resumed = job_store.resume_job(ref["job_id"])

    def no_content(path, *args, **kwargs):
        if Path(path).name == "2.png":
            raise UnusableVisionOutput("no usable facts")
        return "unexpected repeated call"

    monkeypatch.setattr(pipeline, "vision_completion", no_content)
    pipeline.run_durable_job({"job_id": ref["job_id"], "generation": resumed["generation"]})
    result = job_store.read_result(ref["job_id"])
    assert job_store.status(ref["job_id"])["state"] == "SUCCESS"
    assert UNRECOGNIZED_IMAGE_TEXT in result["result"][2]["text"]
    assert calls == {"parse": 1, "vision": ["1.png"]}


@pytest.mark.parametrize("mode", ["parse", "images", "two-stage"])
@pytest.mark.parametrize("legacy_field", ["mineru", "docvortex", "onnx_device"])
def test_sdk_upgrade_rejects_old_stage_checkpoints(prepared, mode, legacy_field):
    pipeline, ref, calls = prepared(mode)
    pipeline.ensure_parsed(ref)
    path = job_store.job_dir(ref["job_id"]) / "parse/manifest.json"
    manifest = job_store.read_json(path)
    settings = manifest["profile"]["settings"]
    assert settings["onnx_device"] == "cpu"
    # Include the legacy vision-version compatibility path: it must not allow
    # checkpoints from another SDK or a profile without explicit CPU selection.
    settings["version"] = 1
    if legacy_field == "onnx_device":
        del settings[legacy_field]
    else:
        settings[legacy_field] = {"mineru": "4.0.12", "docvortex": "0.5.16"}[legacy_field]
    manifest["profile"]["sha256"] = pipeline._value_digest(settings)
    job_store.atomic_json(path, manifest)

    with pytest.raises(RuntimeError, match="profile"):
        pipeline.ensure_parsed(ref)
    with pytest.raises(RuntimeError, match="profile"):
        pipeline.assemble(ref)
    if mode != "parse":
        with pytest.raises(RuntimeError, match="profile"):
            pipeline.run_vision({**ref, "seq": 1})
    assert calls == {"parse": 1, "vision": []}


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


def test_ordinary_finish_rejects_missing_checkpoint_without_parsing(prepared):
    pipeline, ref, calls = prepared()
    with pytest.raises(FileNotFoundError):
        pipeline.finish_durable_job(ref)
    assert calls["parse"] == 0
    assert not calls["vision"]
    assert job_store.status(ref["job_id"])["state"] == "FAILURE"


def test_recover_finds_published_shared_job_after_manifest_write(prepared, monkeypatch):
    from src.config.config import CELERY_TASK_MINERU_QUEUE
    from src.scripts import manage_jobs
    from src.services.tasks import mineru_tasks

    pipeline, ref, calls = prepared()
    job_id = ref["job_id"]
    job_store.update_job(job_id, workflow="shared-parse", publication="published")
    pipeline.ensure_parsed(ref)
    job_store.update_job(job_id, stage="parse")
    sent = []

    def publish(*, args, queue, retry, retry_policy):
        assert retry and retry_policy["max_retries"] == 2
        sent.append((args, queue))

    monkeypatch.setattr(mineru_tasks.finish_mineru_task, "apply_async", publish)
    manage_jobs.main(["recover", job_id])
    manage_jobs.main(["recover", job_id])
    assert sent == [([ref], CELERY_TASK_MINERU_QUEUE)]
    assert calls["parse"] == 1
    assert job_store.status(job_id)["handoff"] == "published"


def test_shared_parse_replays_uncertain_handoff_without_parsing_twice(prepared, monkeypatch):
    from src.services import shared_parse

    pipeline, ref, calls = prepared()
    job_id = ref["job_id"]
    first = True

    def publish(record):
        nonlocal first
        if first:
            first = False
            raise OSError("private broker detail")

    monkeypatch.setattr(shared_parse, "_publish_followup", publish)
    with pytest.raises(job_store.PublishUncertain) as raised:
        shared_parse.run_parse(ref)
    assert raised.value.job_id == job_id
    assert "private broker detail" not in str(raised.value)
    assert job_store.status(job_id)["handoff"] == "uncertain"
    assert job_store.status(job_id)["state"] == "STARTED"

    shared_parse.run_parse(ref)
    assert job_store.status(job_id)["handoff"] == "published"
    assert job_store.status(job_id)["state"] == "STARTED"
    pipeline.finish_durable_job(ref)
    pipeline.finish_durable_job(ref)
    assert job_store.status(job_id)["state"] == "SUCCESS"
    assert calls["parse"] == 1
    assert sorted(calls["vision"]) == ["1.png", "2.png"]


def test_shared_parse_duplicate_does_not_wait_on_active_attempt(prepared, monkeypatch):
    from src.services import shared_parse

    _, ref, calls = prepared("parse")
    handoffs = []
    monkeypatch.setattr(shared_parse, "_publish_followup", handoffs.append)
    with job_store.stage_lock(ref["job_id"], "shared-parse"):
        assert shared_parse.run_parse(ref)["busy"] is True
    assert handoffs == []
    assert calls["parse"] == 0

    shared_parse.run_parse(ref)
    shared_parse.run_parse(ref)
    assert calls["parse"] == 1
    assert len(handoffs) == 1


def test_shared_parse_skips_old_generation_without_handoff(prepared, monkeypatch):
    from src.services import shared_parse

    _, ref, calls = prepared()
    job_id = ref["job_id"]
    job_store.update_job(job_id, handoff={"generation": 0, "state": "published"})
    job_store.resume_job(job_id)
    handoffs = []
    monkeypatch.setattr(shared_parse, "_publish_followup", handoffs.append)

    assert shared_parse.run_parse(ref)["stale"] is True
    assert "handoff" not in job_store.status(job_id)
    shared_parse.run_parse({**ref, "generation": 1})
    assert job_store.status(job_id)["handoff"] == "published"
    assert calls["parse"] == 1
    assert len(handoffs) == 1


@pytest.mark.parametrize("outcome", ["SUCCESS", "FAILURE"])
def test_lost_publish_response_does_not_replace_receiver_result(prepared, monkeypatch, outcome):
    from src.services import shared_parse

    pipeline, ref, calls = prepared("images")
    job_id = ref["job_id"]

    def publish(record):
        if outcome == "FAILURE":

            def fail(*_args, **_kwargs):
                raise RuntimeError("actual downstream failure")

            monkeypatch.setattr(pipeline, "vision_completion", fail)
            with pytest.raises(RuntimeError, match="actual downstream failure"):
                pipeline.finish_durable_job(ref)
        else:
            pipeline.finish_durable_job(ref)
        raise OSError("lost broker response after receiver accepted")

    monkeypatch.setattr(shared_parse, "_publish_followup", publish)
    shared_parse.run_parse(ref)

    status = job_store.status(job_id)
    assert status["handoff"] == "published"
    assert status["state"] == outcome
    if outcome == "FAILURE":
        assert "actual downstream failure" in status["error"]
    assert calls["parse"] == 1


def test_late_delivery_after_uncertain_handoff_recovers_state(prepared, monkeypatch):
    from src.services import shared_parse

    pipeline, ref, calls = prepared("images")

    def ambiguous(record):
        raise OSError("receiver may have accepted")

    monkeypatch.setattr(shared_parse, "_publish_followup", ambiguous)
    with pytest.raises(job_store.PublishUncertain):
        shared_parse.run_parse(ref)

    pipeline.finish_durable_job(ref)
    status = job_store.status(ref["job_id"])
    assert status["handoff"] == "published"
    assert status["state"] == "SUCCESS"
    assert calls["parse"] == 1


def test_shared_two_stage_handoff_preserves_priority_routes(prepared, monkeypatch):
    from src.services import shared_parse, two_stage_pipeline as tasks

    _, ref, _ = prepared("two-stage")
    record = job_store.read_job(ref["job_id"])
    record["options"]["priority"] = "urgent"
    job_store.update_job(ref["job_id"], options=record["options"])
    routes = tasks.resolve_two_stage_queues("urgent")
    sent = []
    monkeypatch.setattr(tasks.dispatch, "apply_async", lambda **kwargs: sent.append(kwargs))

    shared_parse.run_parse(ref)

    assert sent == [
        {
            "args": [ref],
            "queue": routes["dispatch"],
            "kwargs": {
                "dispatch_queue": routes["dispatch"],
                "vision_queue": routes["vision"],
                "merge_queue": routes["merge"],
            },
            "retry": True,
            "retry_policy": {"max_retries": 2},
        }
    ]


def test_recover_cli_does_not_follow_a_concurrent_resume(prepared, monkeypatch):
    from src.scripts import manage_jobs
    from src.services import shared_parse

    pipeline, ref, _ = prepared()
    job_id = ref["job_id"]
    job_store.update_job(job_id, workflow=shared_parse.WORKFLOW, publication="published")
    pipeline.ensure_parsed(ref)
    recover = shared_parse.recover_handoff

    def resume_before_recovery(job_id, **kwargs):
        resumed = job_store.resume_job(job_id)
        job_store.update_job(job_id, publication="published")
        # The concurrent generation has already been claimed. Its state
        # permits recovery, so only the original-generation check fences it.
        pipeline.ensure_parsed({"job_id": job_id, "generation": resumed["generation"]})
        return recover(job_id, **kwargs)

    publications = []
    monkeypatch.setattr(shared_parse, "recover_handoff", resume_before_recovery)
    monkeypatch.setattr(shared_parse, "_publish_followup", publications.append)

    status = manage_jobs.main(["recover", job_id])

    assert status["generation"] == 1
    assert publications == []
    assert "handoff" not in status


def test_recover_handoff_requires_confirmed_initial_publication(prepared, monkeypatch):
    from src.services import shared_parse

    pipeline, ref, _ = prepared()
    job_id = ref["job_id"]
    job_store.update_job(job_id, workflow=shared_parse.WORKFLOW)
    pipeline.ensure_parsed(ref)
    publications = []
    monkeypatch.setattr(shared_parse, "_publish_followup", publications.append)

    shared_parse.recover_handoff(job_id, expected_generation=ref["generation"])

    assert publications == []
    assert job_store.status(job_id)["publication"] == "pending"
    assert "handoff" not in job_store.status(job_id)


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_recover_cli_redacts_checkpoint_validation_errors(
    prepared, monkeypatch, capsys, error_type
):
    from src.scripts import manage_jobs
    from src.services import shared_parse

    pipeline, ref, _ = prepared()
    job_id = ref["job_id"]
    job_store.update_job(job_id, workflow=shared_parse.WORKFLOW, publication="published")
    pipeline.ensure_parsed(ref)

    def invalid_checkpoint(_record):
        raise error_type("private path or configuration")

    monkeypatch.setattr(pipeline, "validate_parse_checkpoint", invalid_checkpoint)
    with pytest.raises(SystemExit) as raised:
        manage_jobs.main(["recover", job_id])
    output = capsys.readouterr()
    assert raised.value.code == 1
    assert output.out == ""
    assert output.err == f"{error_type.__name__}: job action could not be completed\n"


@pytest.mark.parametrize("busy_stage", ["shared-parse", "lifetime"])
def test_handoff_recovery_scan_repairs_manifest_gap_after_lease_is_released(
    prepared, monkeypatch, busy_stage
):
    from src.services import shared_parse
    from src.services.handoff_recovery import HandoffRecovery

    pipeline, ref, calls = prepared()
    job_id = ref["job_id"]
    job_store.publish_job(job_id, lambda record: None, workflow=shared_parse.WORKFLOW)
    pipeline.ensure_parsed(ref)  # Simulate process exit before recording the handoff.
    publications = []
    monkeypatch.setattr(shared_parse, "_publish_followup", publications.append)
    worker = HandoffRecovery()
    try:
        with job_store.stage_lock(job_id, busy_stage):
            assert worker.scan(limit=64) == 1
            assert publications == []
            assert len(list((job_store.store_root() / ".handoffs").glob("*.json"))) == 1
        assert worker.scan(limit=64) == 1
        assert calls["parse"] == 1
        assert len(publications) == 1
        assert publications[0]["generation"] == ref["generation"]
        assert job_store.status(job_id)["handoff"] == "published"
        assert not list((job_store.store_root() / ".handoffs").glob("*.json"))
    finally:
        worker.close()


def test_delayed_handoff_keeps_batch_client_on_original_job(prepared, monkeypatch, tmp_path):
    import httpx

    from src.scripts import batch_parse
    from src.services import shared_parse

    pipeline, ref, calls = prepared("parse")
    job_id = ref["job_id"]
    job_store.publish_job(job_id, lambda record: None, workflow=shared_parse.WORKFLOW)

    def ambiguous(_record):
        raise OSError("broker accepted the message before losing its response")

    monkeypatch.setattr(shared_parse, "_publish_followup", ambiguous)
    with pytest.raises(job_store.PublishUncertain):
        shared_parse.run_parse(ref)

    inputs = tmp_path / "batch-inputs"
    inputs.mkdir()
    (inputs / "sample.pdf").write_bytes(b"private-pdf")
    opts = batch_parse.Options(
        input_dir=inputs,
        output_dir=tmp_path / "batch-output",
        base_url="http://service",
        mode="parse",
        poll_interval=0,
        poll_timeout=2,
        max_attempts=2,
    )
    post_keys = []
    states = []

    def handle(request):
        if request.method == "POST":
            post_keys.append(request.headers["Idempotency-Key"])
            return httpx.Response(200, json={"task_id": job_id, "state": "PENDING"})
        if request.url.path.endswith("/result"):
            return httpx.Response(200, json=job_store.read_result(job_id))
        if states:
            pipeline.finish_durable_job(ref)
        status = job_store.status(job_id)
        states.append(status["state"])
        return httpx.Response(200, json=status)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert batch_parse.run(opts, client=client, token="")["successes"] == 1
    assert states == ["STARTED", "SUCCESS"]
    assert len(post_keys) == 1
    assert calls["parse"] == 1


def test_resumed_generation_cannot_skip_shared_queue_before_claim(prepared, monkeypatch):
    from src.scripts import manage_jobs
    from src.services import shared_parse
    from src.services.handoff_recovery import HandoffRecovery

    pipeline, ref, calls = prepared()
    job_id = ref["job_id"]
    pipeline.ensure_parsed(ref)
    resumed = job_store.resume_job(job_id)
    new_ref = {"job_id": job_id, "generation": resumed["generation"]}
    job_store.publish_job(job_id, lambda record: None, workflow=shared_parse.WORKFLOW)
    publications = []
    monkeypatch.setattr(shared_parse, "_publish_followup", publications.append)

    worker = HandoffRecovery()
    try:
        assert worker.scan(limit=64) == 1
        assert publications == []
        assert manage_jobs.main(["recover", job_id])["state"] == "PENDING"
        assert publications == []
        assert job_store.status(job_id)["state"] == "PENDING"

        class ProcessLost(BaseException):
            pass

        def interrupt_before_handoff(_record):
            raise ProcessLost()

        with monkeypatch.context() as edit:
            edit.setattr(shared_parse, "_handoff", interrupt_before_handoff)
            with pytest.raises(ProcessLost):
                shared_parse.run_parse(new_ref)

        assert job_store.status(job_id)["state"] == "STARTED"
        assert worker.scan(limit=64) == 1
        assert len(publications) == 1 and publications[0]["generation"] == new_ref["generation"]
        assert job_store.status(job_id)["handoff"] == "published"
        assert calls["parse"] == 1
    finally:
        worker.close()
