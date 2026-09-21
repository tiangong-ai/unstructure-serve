"""Wire-level local HTTP integration; endpoints return fixtures, never use a model."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing as mp
from socketserver import BaseRequestHandler, ThreadingTCPServer
from threading import Event, Lock, Thread
import time

import pytest


def _request_once(urls, image, directory, queue, start):
    import os

    os.environ["VLLM_VISION_SLOT_DIR"] = directory
    os.environ["VLLM_VISION_ENDPOINT_SLOTS"] = "2"
    os.environ["VLLM_VISION_SLOT_WAIT_SECONDS"] = "5"
    from src.services import vision_service_vllm as service
    from src.services.vision_service_openai_compatible import OpenAICompatibleClientPool

    service._CLIENT_POOL = OpenAICompatibleClientPool("local-test", urls, timeout=5, max_retries=0)
    queue.put("ready")
    start.wait(10)
    try:
        queue.put(service.vision_completion_vllm(image))
    except Exception as exc:
        queue.put(type(exc).__name__)


def test_isolated_callers_balance_real_http_and_share_capacity(tmp_path):
    guard = Lock()
    calls, active, peak = Counter(), Counter(), Counter()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            port = self.server.server_port
            with guard:
                calls[port] += 1
                active[port] += 1
                peak[port] = max(peak[port], active[port])
            try:
                time.sleep(0.1)
                body = json.dumps(
                    {
                        "id": "test",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "fixture",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "52%"},
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            finally:
                with guard:
                    active[port] -= 1

        def log_message(self, *_):
            pass

    servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
    threads = [Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    image = tmp_path / "image.png"
    image.write_bytes(b"wire fixture; the local HTTP server does not perform inference")
    ctx = mp.get_context("spawn")
    queue, start = ctx.Queue(), ctx.Event()
    urls = [f"http://127.0.0.1:{server.server_port}/v1" for server in servers]
    processes = [
        ctx.Process(
            target=_request_once, args=(urls, str(image), str(tmp_path / "slots"), queue, start)
        )
        for _ in range(8)
    ]
    try:
        for process in processes:
            process.start()
        assert [queue.get(timeout=20) for _ in processes] == ["ready"] * len(processes)
        start.set()
        assert [queue.get(timeout=20) for _ in processes] == ["52%"] * len(processes)
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert sorted(calls.values()) == [4, 4]
        assert max(peak.values()) <= 2
        for state_file in (tmp_path / "slots").iterdir():
            assert "127.0.0.1" not in state_file.name
            assert "local-test" not in state_file.read_text()
            assert "127.0.0.1" not in state_file.read_text()
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(10)
        for server in servers:
            server.shutdown()
            server.server_close()
        queue.close()
        queue.join_thread()


@pytest.mark.parametrize("failure", ["tls_stall", "http_503"])
def test_failed_endpoint_is_skipped_and_recovered_endpoint_rejoins(tmp_path, monkeypatch, failure):
    from src.services import vision_service_vllm as service
    from src.services.vision_service_openai_compatible import OpenAICompatibleClientPool

    monkeypatch.setenv("VLLM_VISION_SLOT_DIR", str(tmp_path / "slots"))
    monkeypatch.setenv("VLLM_VISION_COOLDOWN_SECONDS", "30")
    monkeypatch.setenv("VLLM_VISION_TIMEOUT_SECONDS", "2")
    monkeypatch.setenv("VLLM_VISION_CONNECT_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setenv("VLLM_VISION_MAX_RETRIES", "0")
    release = Event()
    calls = Counter()
    recovering = Event()

    class Healthy(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls["healthy"] += 1
            # Healthy generation may take longer than the connection budget.
            time.sleep(0.1)
            body = json.dumps(
                {
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "52%"},
                        }
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    class Unavailable(Healthy):
        def do_POST(self):
            calls["failed_endpoint"] += 1
            if recovering.is_set():
                return super().do_POST()
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()

    class StalledTLS(BaseRequestHandler):
        def handle(self):
            calls["failed_endpoint"] += 1
            self.request.recv(8192)  # Receive ClientHello, never complete TLS.
            release.wait(5)

    healthy = ThreadingHTTPServer(("127.0.0.1", 0), Healthy)
    failed = (
        ThreadingTCPServer(("127.0.0.1", 0), StalledTLS)
        if failure == "tls_stall"
        else ThreadingHTTPServer(("127.0.0.1", 0), Unavailable)
    )
    for server in (healthy, failed):
        Thread(target=server.serve_forever, daemon=True).start()
    urls = [
        f"http://127.0.0.1:{healthy.server_port}/v1",
        f"{'https' if failure == 'tls_stall' else 'http'}://127.0.0.1:{failed.server_address[1]}/v1",
    ]
    pool = OpenAICompatibleClientPool("test-key", urls, **service._client_budgets())
    monkeypatch.setattr(service, "_CLIENT_POOL", pool)
    image = tmp_path / "wire.png"
    image.write_bytes(b"HTTP fixture only; actual PDF inference is a separate opt-in test")
    try:
        started = time.monotonic()
        for _ in range(3):
            assert service.vision_completion_vllm(str(image)) == "52%"
        assert time.monotonic() - started < 1.5
        assert calls["failed_endpoint"] == 1, "Cooldown must skip repeated attempts"
        if failure == "http_503":
            recovering.set()
            now = time.time()
            monkeypatch.setattr("src.services.vision_capacity.time.time", lambda: now + 31)
            for _ in range(2):
                assert service.vision_completion_vllm(str(image)) == "52%"
            assert calls["failed_endpoint"] == 2, "Recovered endpoint must rejoin rotation"
    finally:
        release.set()
        for _, client in pool.get_endpoint_clients():
            client.close()
        for server in (healthy, failed):
            server.shutdown()
            server.server_close()
