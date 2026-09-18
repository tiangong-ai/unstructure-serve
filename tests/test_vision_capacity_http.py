"""Wire-level local HTTP integration; endpoints return fixtures, never use a model."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing as mp
from threading import Lock, Thread
import time


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
