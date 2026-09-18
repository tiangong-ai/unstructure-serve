import json
from unittest.mock import Mock

from src.services import job_store
from src.scripts import manage_jobs


def make_job(tmp_path, monkeypatch):
    monkeypatch.setenv("MINERU_JOB_STORE_DIR", str(tmp_path / "jobs"))
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\nprivate bytes")
    return job_store.create_job("parse", source, {})


def test_collection_is_preview_by_default_and_keeps_tombstone(tmp_path, monkeypatch, capsys):
    job = make_job(tmp_path, monkeypatch)
    job_store.save_result(job["job_id"], {"result": []})
    manage_jobs.main(["gc", "--retention-days", "0"])
    assert json.loads(capsys.readouterr().out)["candidates"] == [job["job_id"]]
    assert job_store.source_path(job["job_id"]).exists()
    manage_jobs.main(["gc", "--retention-days", "0", "--apply"])
    assert job_store.status(job["job_id"])["state"] == "EXPIRED"


def test_recover_only_republishes_unconfirmed_outbox(tmp_path, monkeypatch):
    job = make_job(tmp_path, monkeypatch)
    publisher = Mock()
    monkeypatch.setattr(manage_jobs, "publish_existing_job", publisher)
    manage_jobs.main(["recover", job["job_id"]])
    publisher.assert_called_once_with(job["job_id"])
    job_store.update_job(job["job_id"], publication="published")
    manage_jobs.main(["recover", job["job_id"]])
    assert publisher.call_count == 1


def test_status_never_emits_source_options_or_error_details(tmp_path, monkeypatch, capsys):
    job = make_job(tmp_path, monkeypatch)
    job_store.fail_job(job["job_id"], RuntimeError("private credential"))
    manage_jobs.main(["list"])
    output = capsys.readouterr().out
    assert "private" not in output
    assert json.loads(output)["jobs"][0]["state"] == "FAILURE"
