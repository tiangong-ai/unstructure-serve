"""Production ASGI settings; API workers and parse capacity are independent."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
worker_class = "uvicorn_worker.UvicornWorker"
workers = int(os.getenv("API_WORKERS", "4"))
bind = os.getenv("API_BIND", "0.0.0.0:7770")
timeout = int(os.getenv("API_WORKER_TIMEOUT", "1900"))
graceful_timeout = timeout
keepalive = 30
# Polling/status requests count too. Retain periodic recycling without a
# restart every ~500 cheap requests; tune against measured RSS over time.
max_requests = int(os.getenv("API_MAX_REQUESTS", "5000"))
max_requests_jitter = int(os.getenv("API_MAX_REQUESTS_JITTER", "500"))
preload_app = False  # Do not fork an initialized scheduler/client pool.
