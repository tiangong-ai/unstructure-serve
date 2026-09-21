# MinerU 4.0's documented CUDA 13 / Blackwell-compatible serving base.
ARG VLLM_IMAGE=vllm/vllm-openai:v0.21.0
FROM ${VLLM_IMAGE}

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libcairo2-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Reuse the image's vLLM/Torch pair. The application SDK has a separate lockfile.
# Instrumentator 8 supports the current Starlette router representation.
RUN --mount=type=cache,target=/root/.cache/pip python3 -m pip install \
    'mineru[torch]==4.0.5' 'docvortex==0.4.21' 'mineru-vl-utils==2.0.5' 'fastapi==0.141.1' 'starlette==1.6.0' \
    'prometheus-fastapi-instrumentator==8.1.0' 'openai==2.54.0' 'pycairo==1.29.1' \
    && python3 -m pip check \
    && python3 -c "from importlib.metadata import version; assert version('mineru') == '4.0.5'; assert version('docvortex') == '0.4.21'; assert version('mineru-vl-utils') == '2.0.5'; assert version('vllm') == '0.21.0'"

ENV MINERU_HOME=/opt/mineru \
    MINERU_MODEL_SOURCE=modelscope \
    MINERU_MODEL_VLM_ENGINE=vllm

ENTRYPOINT ["mineru-kit", "vlm-server", "--engine", "vllm"]
CMD ["--host", "0.0.0.0", "--port", "30000", "--served-model-name", "mineru4"]
