"""Restartable local parsing and image stages; Celery messages contain references only."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile
import uuid

from src.services import job_store as store
from src.services.gpu_scheduler import run_isolated_call
from src.services.mineru_service_full import parse_doc
from src.services.mineru_with_images_service import (
    _build_context_blocks,
    _build_vision_cache_context,
    _build_vision_prompt,
    _reindex_blocks,
    _resolve_context_windows,
    clean_text,
)
from src.services.vision_prompts import vision_request_key
from src.services.vision_service import vision_completion
from src.utils.file_conversion import maybe_convert_to_pdf
from src.utils.text_output import sanitize_vision_text
from src.utils.mineru_backend import resolve_tier


def _reference(ref):
    return {"job_id": ref["job_id"], "generation": ref["generation"]}


def mark_failure(ref, error):
    """Record a canvas publication failure only for its current generation."""
    try:
        with store.execution(ref["job_id"], ref["generation"]):
            store.fail_job(ref["job_id"], error)
    except store.StaleGeneration:
        pass


def _stage(ref, stage, operation):
    try:
        with store.execution(ref["job_id"], ref["generation"]) as record:
            with store.stage_lock(ref["job_id"], stage):
                store.start_job(ref["job_id"])
                try:
                    return operation(record)
                except Exception as exc:
                    # Still hold the lifetime lease: an old attempt cannot mark
                    # a newer generation failed between validation and write.
                    store.fail_job(ref["job_id"], exc)
                    raise
    except store.StaleGeneration:
        return {**_reference(ref), "stale": True}


def _digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _value_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _execution_profile(record):
    from mineru.config import config
    from src.services import mineru_service_full, mineru_with_images_service, vision_prompts
    from src.services import vision_service, vision_service_vllm

    options = record["options"]
    details = {
        "version": 1,
        "mineru": version("mineru"),
        "docvortex": version("docvortex"),
        "tier": resolve_tier(options.get("backend")),
        "parse_model": config.model.vlm.model,
        "small_backend": config.model.small_backend,
        "ocr_mode": mineru_service_full._env_default_method(),
        "parse_endpoints_sha256": _value_digest(
            mineru_service_full._server_urls_from_env()
            or mineru_service_full._normalize_server_url_input(config.model.vlm.server_url)
            or [mineru_service_full.DEFAULT_VLLM_SERVER_URL]
        ),
        "checkbox_reconcile": os.getenv("MINERU_TEXT_LAYER_CHECKBOX_RECONCILE", "true"),
        # An operator can distinguish new weights served under an unchanged
        # model name. No client can infer that change from a model alias alone.
        "model_revision": os.getenv("MINERU_EXECUTION_PROFILE_REVISION", ""),
    }
    if record["mode"] != "parse":
        provider, model = vision_service._normalize_request_overrides(
            options.get("vision_provider"), options.get("vision_model")
        )
        chosen = vision_service._resolve_provider(provider)
        model = vision_service._resolve_model(chosen, model)
        prompt = (options.get("prompt") or "").strip() or vision_prompts.DEFAULT_VISION_PROMPT
        details["vision"] = {
            "provider": chosen.value,
            "model": model,
            "prompt_sha256": _value_digest(prompt),
            "context_version": getattr(vision_prompts, "VISION_CACHE_CONTEXT_VERSION", 1),
            "context_window": mineru_with_images_service.CONTEXT_WINDOW,
            "configured_context_window": os.getenv("VISION_CONTEXT_WINDOW"),
            "fallbacks": [
                [key, spec.default_model, spec.has_credentials()]
                for key, spec in vision_service.PROVIDER_SPECS.items()
            ],
            "sampling": vision_service_vllm._build_request_options(),
            "extra_body": vision_service_vllm._build_extra_body(),
            "endpoint_keys": sorted(
                key for key, _ in vision_service_vllm._CLIENT_POOL.get_endpoint_clients()
            ),
        }
    return {"sha256": _value_digest(details), "settings": details}


def _assert_profile(record, manifest):
    if manifest.get("profile", {}).get("sha256") != _execution_profile(record)["sha256"]:
        raise RuntimeError(
            "Stored execution profile differs; restore its model/settings or submit a new job"
        )


def _file(job_id, relative):
    root = store.job_dir(job_id).resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root) or Path(relative).is_absolute():
        raise ValueError("Invalid checkpoint path")
    return target


def _checked_json(job_id, descriptor):
    path = _file(job_id, descriptor["path"])
    if _digest(path) != descriptor["sha256"]:
        raise RuntimeError("Stored stage output failed integrity verification")
    return store.read_json(path)


def _write_json(job_id, relative, value):
    path = _file(job_id, relative)
    store.atomic_json(path, value)
    return {"path": relative, "sha256": _digest(path)}


def _manifest(job_id):
    return store.read_json(store.job_dir(job_id) / "parse" / "manifest.json")


def _validate_manifest(job_id, manifest):
    if manifest.get("version") != 1:
        raise RuntimeError("Unsupported parse checkpoint version")
    _checked_json(job_id, manifest["content"])
    for descriptor in manifest["images"].values():
        _checked_json(job_id, descriptor)
    for relative, expected in manifest["assets"].items():
        if _digest(_file(job_id, relative)) != expected:
            raise RuntimeError("Stored parse asset failed integrity verification")


def _parse_input(source, output, *, backend):
    """Run conversion and MinerU inside the same supervised task group."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    previous_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="conversion-", dir=output) as temporary:
        # Interrupted conversion files remain inside this job, not a shared
        # system temp directory; an incomplete parse can safely replace them.
        tempfile.tempdir = temporary
        try:
            converted, _ = maybe_convert_to_pdf(str(source), Path(source).suffix.lower())
            return parse_doc([Path(converted)], output / "artifacts", backend=backend)
        finally:
            tempfile.tempdir = previous_tempdir


def _all_image_jobs(content, output, *, keep_positions):
    """Ordinary images mode retains every image, without two-stage filtering."""
    blocks = _build_context_blocks(content)
    index = _reindex_blocks(blocks)
    seen, jobs = {}, []
    for item in content:
        item.pop("__image_seq", None)
        if item.get("type") != "image" or not (item.get("img_path") or "").strip():
            continue
        image = Path(output) / item["img_path"]
        contexts = _resolve_context_windows(blocks, index.get(id(item)), item)
        context, _ = _build_vision_prompt(item, contexts)
        key = vision_request_key(
            _digest(image),
            _build_vision_cache_context(
                blocks, index.get(id(item)), item, keep_positions=keep_positions
            ),
        )
        if key not in seen:
            seq = len(jobs) + 1
            seen[key] = seq
            jobs.append({"seq": seq, "img_path": str(image), "context_payload": context})
        item["__image_seq"] = seen[key]
    return jobs, content


def ensure_parsed(ref):
    def execute(record):
        job_id = ref["job_id"]
        if store.status(job_id)["state"] == "SUCCESS":
            return _reference(ref)
        root = store.job_dir(job_id)
        directory = root / "parse"
        marker = directory / "manifest.json"
        source = store.source_path(job_id)
        if _digest(source) != record["source_sha256"]:
            raise RuntimeError("Stored job input failed integrity verification")
        if marker.exists():
            manifest = store.read_json(marker)
            _assert_profile(record, manifest)
            _validate_manifest(job_id, manifest)
            return _reference(ref)
        directory.mkdir(exist_ok=True)
        # A parent that died may leave a child finishing its PDEATHSIG cleanup.
        # A unique attempt path prevents that child from touching a new parse,
        # even when recovery starts before all old descendants have exited.
        attempt = directory / f"attempt-{uuid.uuid4().hex}"
        attempt.mkdir()
        store.update_job(job_id, state="STARTED", stage="parse")
        options = record["options"]
        profile = _execution_profile(record)
        default_timeout = os.getenv("MINERU_TASK_HARD_TIMEOUT_SECONDS", "1800")
        timeout_key = {
            "parse": "MINERU_DEFAULT_HARD_TIMEOUT_SECONDS",
            "images": "MINERU_IMAGES_HARD_TIMEOUT_SECONDS",
            "two-stage": "MINERU_TWO_STAGE_HARD_TIMEOUT_SECONDS",
        }[record["mode"]]
        response = run_isolated_call(
            _parse_input,
            source,
            attempt,
            hard_timeout=float(os.getenv(timeout_key, default_timeout)),
            backend=options.get("backend"),
        )
        if not response or len(response) != 3 or not isinstance(response[0], list):
            raise RuntimeError("Invalid durable parse result")
        content, output, _ = response
        if record["mode"] == "two-stage":
            from src.services.two_stage_pipeline import _build_image_jobs

            jobs, content = _build_image_jobs(
                content, output, keep_positions=bool((options.get("prompt") or "").strip())
            )
        elif record["mode"] == "images":
            jobs, content = _all_image_jobs(
                content, output, keep_positions=bool((options.get("prompt") or "").strip())
            )
        else:
            jobs = []
        assets = {}
        asset_paths = {path for path in Path(output).rglob("*") if path.is_file()}
        asset_paths.add(Path(output) / "middle_json.json")
        asset_paths.update(
            Path(output) / item["img_path"] for item in content if item.get("img_path")
        )
        asset_directories = set()
        for path in asset_paths:
            relative = str(path.resolve().relative_to(root.resolve()))
            path = _file(job_id, relative)
            assets[relative] = _digest(path)
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            parent = path.parent
            while parent.is_relative_to(root.resolve()):
                asset_directories.add(parent)
                parent = parent.parent
        for parent in sorted(asset_directories, key=lambda path: len(path.parts), reverse=True):
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        image_records = {}
        for job in jobs:
            relative = str(Path(job["img_path"]).resolve().relative_to(root.resolve()))
            job["img_path"] = relative
            job["asset_sha256"] = assets[relative]
            job["profile_sha256"] = profile["sha256"]
            seq = int(job["seq"])
            image_records[str(seq)] = _write_json(job_id, f"parse/images/{seq}.json", job)
        manifest = {
            "version": 1,
            "profile": profile,
            "source_sha256": record["source_sha256"],
            "content": _write_json(job_id, "parse/content.json", content),
            "images": image_records,
            "assets": assets,
        }
        _assert_profile(record, manifest)
        store.atomic_json(marker, manifest)
        store.update_job(
            job_id,
            stage="parsed",
            progress={"images_total": len(jobs), "images_completed": 0},
        )
        return _reference(ref)

    return _stage(ref, "parse", execute)


def _vision_result(job_id, seq, descriptor):
    path = store.job_dir(job_id) / "vision" / f"{seq}.json"
    if not path.exists():
        return None
    result = store.read_json(path)
    if (
        result.get("input_sha256") != descriptor["sha256"]
        or result.get("seq") != seq
        or not isinstance(result.get("vision_text"), str)
        or not result["vision_text"].strip()
        or result.get("text_sha256") != _value_digest(result["vision_text"])
    ):
        raise RuntimeError("Invalid persisted vision result")
    return result


def run_vision(ref):
    seq = int(ref["seq"])

    def execute(record):
        job_id = ref["job_id"]
        manifest = _manifest(job_id)
        _assert_profile(record, manifest)
        descriptor = manifest["images"][str(seq)]
        if _vision_result(job_id, seq, descriptor) is not None:
            return {**_reference(ref), "seq": seq}
        job = _checked_json(job_id, descriptor)
        image = _file(job_id, job["img_path"])
        if _digest(image) != job["asset_sha256"]:
            raise RuntimeError("Stored vision asset failed integrity verification")
        options = record["options"]
        text = sanitize_vision_text(
            clean_text(
                vision_completion(
                    str(image),
                    job.get("context_payload", ""),
                    prompt=(options.get("prompt") or "").strip() or None,
                    provider=options.get("vision_provider"),
                    model=options.get("vision_model"),
                )
            )
        )
        if not text.strip():
            raise RuntimeError("Vision result was empty after normalization")
        _assert_profile(record, manifest)
        store.atomic_json(
            store.job_dir(job_id) / "vision" / f"{seq}.json",
            {
                "seq": seq,
                "vision_text": text,
                "input_sha256": descriptor["sha256"],
                "text_sha256": _value_digest(text),
            },
        )
        return {**_reference(ref), "seq": seq}

    return _stage(ref, f"vision-{seq}", execute)


def pending_vision(ref, *, limit):
    def execute(record):
        if store.status(ref["job_id"])["state"] == "SUCCESS":
            return []
        manifest = _manifest(ref["job_id"])
        _assert_profile(record, manifest)
        pending = [
            int(seq)
            for seq, descriptor in manifest["images"].items()
            if _vision_result(ref["job_id"], int(seq), descriptor) is None
        ]
        store.update_job(
            ref["job_id"],
            stage="vision" if pending else "merge",
            progress={
                "images_total": len(manifest["images"]),
                "images_completed": len(manifest["images"]) - len(pending),
            },
        )
        return pending[:limit]

    if limit < 1:
        raise ValueError("Vision wave size must be positive")
    return _stage(ref, "dispatch", execute)


def assemble(ref):
    def execute(record):
        job_id = ref["job_id"]
        if store.status(job_id)["state"] == "SUCCESS":
            store.read_result(job_id)
            return store.result_reference(job_id)
        from src.services.two_stage_pipeline import _merge_content

        manifest = _manifest(job_id)
        _assert_profile(record, manifest)
        content = _checked_json(job_id, manifest["content"])
        results = []
        for seq, descriptor in manifest["images"].items():
            result = _vision_result(job_id, int(seq), descriptor)
            if result is None:
                raise RuntimeError(f"Missing persisted vision result for seq={seq}")
            results.append(result)
        options = record["options"]
        items, txt = _merge_content(
            content,
            results,
            chunk_type=bool(options.get("chunk_type")),
            return_txt=bool(options.get("return_txt")),
        )
        payload = {"result": [item.model_dump() for item in items], "txt": txt}
        if record["mode"] == "parse":
            for item in payload["result"]:
                if item.get("type") == "image":
                    item["type"] = None
        store.update_job(
            job_id,
            stage="merge",
            progress={"images_total": len(results), "images_completed": len(results)},
        )
        return store.save_result(job_id, payload)

    return _stage(ref, "merge", execute)


def run_durable_job(ref):
    """Ordinary parse/images worker: reuse completed stages after a retry."""

    def execute(_record):
        if store.status(ref["job_id"])["state"] == "SUCCESS":
            store.read_result(ref["job_id"])
            return store.result_reference(ref["job_id"])
        ensure_parsed(ref)
        window = max(1, int(os.getenv("VISION_BATCH_SIZE", "3")))
        pending = iter(pending_vision(ref, limit=2**31))
        with ThreadPoolExecutor(max_workers=window) as pool:
            futures = set()

            def submit_next():
                seq = next(pending, None)
                if seq is not None:
                    futures.add(pool.submit(run_vision, {**_reference(ref), "seq": seq}))

            for _ in range(window):
                submit_next()
            try:
                while futures:
                    completed, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in completed:
                        futures.remove(future)
                        future.result()
                    for _ in completed:
                        submit_next()
            finally:
                for future in futures:
                    future.cancel()
        return assemble(ref)

    return _stage(ref, "ordinary", execute)
