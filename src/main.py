import logging
import sys
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# from fastapi.staticfiles import StaticFiles

from src.config.config import FASTAPI_AUTH, FASTAPI_BEARER_TOKEN
from src.routers import (
    health_router,
    guides_router,
    job_router,
    markdown_router,
    mineru_router,
    mineru_task_router,
    mineru_with_images_task_router,
    mineru_sci_router,
    mineru_with_images_router,
    two_stage_router,
)
from src.services.gpu_scheduler import scheduler

load_dotenv()

# 直接配置根日志记录器
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)

# 如果没有处理器，添加一个
if not root_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

# 降低 httpx/httpcore 日志等级以避免打印请求细节
for noisy_logger in ("httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)

bearer_scheme = HTTPBearer()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        yield
    finally:
        scheduler.shutdown(wait=True)


def validate_token(credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)):
    if credentials.scheme != "Bearer" or credentials.credentials != FASTAPI_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return credentials


app = FastAPI(
    title="TianGong AI Unstructure Serve",
    version="1.0",
    description=(
        "TianGong AI Unstructure API Server. "
        "AI integration guide: GET /guides/ai-integration.md; index: GET /llms.txt. "
        "Use the deployed OpenAPI for field locations and supported enums."
    ),
    dependencies=[Depends(validate_token)] if FASTAPI_AUTH else None,
    lifespan=lifespan,
)

origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router.router)
app.include_router(guides_router.router)
app.include_router(job_router.router)
app.include_router(markdown_router.router)
app.include_router(mineru_router.router)
app.include_router(mineru_task_router.router)
app.include_router(mineru_sci_router.router)
app.include_router(mineru_with_images_router.router)
app.include_router(mineru_with_images_task_router.router)
app.include_router(two_stage_router.router)
