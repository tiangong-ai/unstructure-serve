import os
from pathlib import Path

import requests

API = os.getenv("MINERU_TASK_API", "http://localhost:8770/mineru/task")
INPUT_DIR = Path("input")
TOKEN = os.getenv("FASTAPI_BEARER_TOKEN")

headers = {}
if TOKEN:
    headers["Authorization"] = f"Bearer {TOKEN}"

for file_path in INPUT_DIR.iterdir():
    if not file_path.is_file():
        continue

    # 常规队列
    data_normal = {
        "chunk_type": "true",
        "return_txt": "false",
        "priority": "normal",
    }
    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f)}
        resp = requests.post(API, headers=headers, data=data_normal, files=files)
    resp.raise_for_status()
    print(f"[normal] {file_path.name}: {resp.json()}")

    # 加塞队列
    data_urgent = {
        "chunk_type": "true",
        "return_txt": "false",
        "priority": "urgent",
    }
    with file_path.open("rb") as f:
        files = {"file": (file_path.name, f)}
        resp = requests.post(API, headers=headers, data=data_urgent, files=files)
    resp.raise_for_status()
    print(f"[urgent] {file_path.name}: {resp.json()}")
