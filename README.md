
# TianGong AI Unstructure Serve

<!-- tiangong-ai-migration-20260914:start -->

## GitHub organization migration / GitHub 组织迁移

This original repository now belongs to the [tiangong-ai organization](https://github.com/tiangong-ai), with its repository identity and history retained. The CLI package `@tiangong-ai/cli` and command `tiangong-ai` are unchanged. Wiki is now published as `@tiangong-ai/wiki`; its commands remain unchanged. See the [migration and upgrade notes](https://github.com/tiangong-ai/cli-toolkit/releases/tag/v0.0.63).

该仓库已迁入 [tiangong-ai 组织](https://github.com/tiangong-ai)，仓库身份与历史保留。CLI 包名 `@tiangong-ai/cli` 和命令 `tiangong-ai` 不变；Wiki 新包名为 `@tiangong-ai/wiki`，命令不变。升级方式见[迁移说明](https://github.com/tiangong-ai/cli-toolkit/releases/tag/v0.0.63)。原个人账号 `tiangong-ai-legacy` 保留历史；请自行 Follow 新组织。

<!-- tiangong-ai-migration-20260914:end -->

## Env Preparing

Use [uv](https://docs.astral.sh/uv/) to manage Python and project dependencies:

```bash
# (optional) install uv if it is not available yet
curl -LsSf https://astral.sh/uv/install.sh | sh

# ensure CPython 3.12 is available locally
uv python install 3.12

# install or upgrade all project dependencies into .venv/
uv sync
uv sync --upgrade
```

`uv sync` reads `pyproject.toml` (and `uv.lock` when present) to create a virtual environment at `.venv/`.  
All runtime and development dependencies now live in `pyproject.toml`; the legacy requirement files are retained only for reference.
Activate it with `source .venv/bin/activate` or prefer `uv run …` / `uv venv` for ephemeral shells.

Download MinerU models (first run only):

```bash
wget https://gcore.jsdelivr.net/gh/opendatalab/MinerU@master/scripts/download_models_hf.py -O download_models_hf.py
uv run python download_models_hf.py
```

### Development helpers

```bash
uv run --group dev black .
uv run --group dev ruff check src
uv run --group dev pytest
```

```bash
sudo apt update

sudo apt install -y libmagic-dev
sudo apt install -y poppler-utils
sudo apt install -y libreoffice
sudo apt install -y pandoc
sudo apt install -y graphicsmagick
```

### MinerU runtime defaults (.env)

- `.env` is loaded automatically; adjust defaults there without touching code.
- `MINERU_DEFAULT_BACKEND` controls the parsing backend (default `vlm-http-client`; options: `pipeline`, `vlm-transformers`, `vlm-vllm-engine`, `vlm-lmdeploy-engine`, `vlm-http-client`, `vlm-mlx-engine`).
- `MINERU_DEFAULT_LANG` sets the OCR language hint for pipeline mode (default `ch`).
- `MINERU_DEFAULT_METHOD` sets the pipeline parse method (`auto`/`txt`/`ocr`, default `auto`).
- Vision defaults are now vLLM-only: `.env` / `.env.example` set `VISION_PROVIDER=vllm` and `VISION_PROVIDER_CHOICES=vllm`; image-aware routes no longer fall back to OpenAI / Gemini by default.
- `VLLM_BASE_URLS` / `VLLM_BASE_URL` is now required for the vLLM vision provider; `VLLM_API_KEY` is optional auth only. When multiple URLs are configured, each image request tries them in rotated order until one succeeds or all fail.
- `/mineru_with_images`, `/mineru_with_images/task`, and `/two_stage/task` no longer degrade vision failures back to caption/footnote text. If the vision stage raises, the sync API returns 500 and async/Celery tasks fail.
- MinerU 后端通过 `MINERU_DEFAULT_BACKEND` 环境变量配置；允许值：`pipeline`/`vlm-transformers`/`vlm-vllm-engine`/`vlm-lmdeploy-engine`/`vlm-http-client`/`vlm-mlx-engine`，额外接受 `hybrid-auto-engine`/`hybrid-http-client`。在当前 MinerU 3.x 适配层中，`hybrid-*` 会直接透传给官方 `do_parse`，不再回退到 `vlm-*`。API 不再接受表单参数覆盖后端。
- `MINERU_VLLM_SERVER_URLS` / `MINERU_VLLM_SERVER_URL` (or `MINERU_VLM_SERVER_URLS` / `MINERU_VLM_SERVER_URL`) list VLM endpoints; comma-separated or JSON array values are accepted. If unset, the service falls back to `http://127.0.0.1:30000`.
- `MINERU_HYBRID_BATCH_RATIO` 控制 hybrid-* 后端小模型 batch 倍率（默认 8）；仅在 hybrid 模式有效，用于权衡显存占用。
- `MINERU_HYBRID_FORCE_PIPELINE_ENABLE` 强制 hybrid-* 的文本提取使用小模型（默认 false），在极端场景可降低幻觉。

Test Cuda (optional):

```bash
watch -n 1 nvidia-smi
```

Start Server:

```bash
# run from within the uv-managed environment (activate .venv or prefix with `uv run`)
MINERU_MODEL_SOURCE=modelscope uvicorn src.main:app --host 0.0.0.0 --port 7770

MINERU_MODEL_SOURCE=modelscope CUDA_VISIBLE_DEVICES=0 uvicorn src.main:app --host 0.0.0.0 --port 8770
MINERU_MODEL_SOURCE=modelscope CUDA_VISIBLE_DEVICES=1 uvicorn src.main:app --host 0.0.0.0 --port 8771
MINERU_MODEL_SOURCE=modelscope CUDA_VISIBLE_DEVICES=2 uvicorn src.main:app --host 0.0.0.0 --port 8772

# run in background

nohup env MINERU_MODEL_SOURCE=modelscope uvicorn src.main:app --host 0.0.0.0 --port 7770 > uvicorn.log 2>&1 &

nohup env MINERU_MODEL_SOURCE=modelscope CUDA_VISIBLE_DEVICES=0 uvicorn src.main:app --host 0.0.0.0 --port 8770 > uvicorn.log 2>&1 &
nohup env MINERU_MODEL_SOURCE=modelscope CUDA_VISIBLE_DEVICES=1 uvicorn src.main:app --host 0.0.0.0 --port 8771 > uvicorn.log 2>&1 &
nohup env MINERU_MODEL_SOURCE=modelscope CUDA_VISIBLE_DEVICES=2 uvicorn src.main:app --host 0.0.0.0 --port 8772 > uvicorn.log 2>&1 &

npm i -g pm2
watch -n 1 nvidia-smi

# 使用 pm2 管理进程
pm2 save
pm2 resurrect

# 启动所有服务
pm2 start ecosystem.vllm.config.json
pm2 start ecosystem.config.json
pm2 start ecosystem.celery.json # 普通一队列
pm2 start ecosystem.two_stage.celery.json # 两队列

pm2 start ecosystem.two_stage.flower.json  # includes separate dispatch + merge workers; dispatch 不再订阅 default，避免阻塞 merge。用不上！
pm2 start ecosystem.celery.flower.json # 用这个开启flower监控

pm2 stop ecosystem.two_stage.celery.json # 停掉 two_stage celery
pm2 delete ecosystem.two_stage.celery.json


pm2 stop ecosystem.celery.flower.json # 停掉 flower
pm2 delete ecosystem.celery.flower.json

pm2 stop ecosystem.config.json # 停掉 unstructured-gunicorn
pm2 delete ecosystem.config.json

pm2 list # 查看状态

# 清理/清空队列（选择对应 broker）
# purge via celery (会连到 CELERY_BROKER_URL)
celery -A src.services.celery_app purge -f



pm2 start ecosystem.vllm.quatro.json
pm2 start ecosystem.quatro.json

pm2 restart all

pm2 status

pm2 restart all

pm2 status

pm2 delete all

#清空队列
uv run celery -A src.services.two_stage_pipeline purge -Q queue_parse_gpu,queue_vision,queue_dispatch,default
# 清空redis
redis-cli -n 0 flushdb
# 删除暂存文件
rm -rf /tmp/tiangong_mineru_tasks/*
# 转成json
python3 src/scripts/read_pickle.py "pickle/41-Life cycle assessment of lithium nickel cobalt manganese oxide batteries and lithium iron phosphate batteries for electric vehicles in China. JES 2022.pkl"

# 使用 for 循环和 lsof
for port in {8770..8773}
do
  # lsof -t 选项只会输出PID，方便后续处理
  PID=$(sudo lsof -t -i:$port)
  
  if [ -n "$PID" ]; then
    echo "找到占用端口 $port 的进程，PID: $PID。正在终止..."
    sudo kill -9 $PID
  else
    echo "端口 $port 未被占用。"
  fi
done

# 使用 lsof 清理 7770 端口
port=7770
# lsof -t 选项只会输出PID，方便后续处理
PID=$(sudo lsof -t -i:$port)

if [ -n "$PID" ]; then
  echo "找到占用端口 $port 的进程，PID: $PID。正在终止..."
  sudo kill -9 $PID
else
  echo "端口 $port 未被占用。"
fi

```

# Kroki Server
```bash
docker run -d -p --restart unless-stopped 7999:8000 yuzutech/kroki
```
# Quickchart Server
```bash
docker run -d -p --restart unless-stopped 7998:3400 ianw/quickchart
```

# MinIO Server
```bash
docker run -d \
  -p 9000:9000 \
  -p 9001:9001 \
  --name minio \
  -e MINIO_ROOT_USER=minioadmin \
  -e MINIO_ROOT_PASSWORD=yourpassword \
  --restart unless-stopped \
  -v "$(pwd)/minio/data:/data" \
  quay.io/minio/minio server /data --console-address ":9001"

```

# MinerU vLLM Server
```bash
# Run first time to download models
MINERU_MODEL_SOURCE=modelscope CUDA_VISIBLE_DEVICES=0 mineru-vllm-server --port 30000
```

# Redis Server
```bash
docker run -d --name redis -p 6379:6379 redis:8 
```

# Celery Worker
```bash
# GPU 调度内部会再起子进程，Celery worker 请用非 daemonic 池
# 监听 urgent + normal + default 队列，priority=urgent 会落到 queue_urgent
CELERY_BROKER_URL=redis://localhost:6379/0 \
CELERY_TASK_MINERU_QUEUE=queue_normal \
CELERY_TASK_URGENT_QUEUE=queue_urgent \
uv run celery -A src.services.celery_app worker \
-l info -Q queue_urgent,queue_normal,default -P solo -c 1 --prefetch-multiplier=1
```

# Celery Flower Monitoring
```bash
uv run celery -A src.services.celery_app flower --address=0.0.0.0 --port=5555
```

# redis自启动
docker run -d --name redis --restart=always -p 6379:6379 redis:8
