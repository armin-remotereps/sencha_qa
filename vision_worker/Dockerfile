# Vision worker image (CUDA). Build context: vision_worker/.
#   docker compose build            (from vision_worker/)
# The host needs only the NVIDIA driver plus the NVIDIA Container Toolkit
# (Linux) or Docker Desktop with the WSL2 backend (Windows): the torch wheels
# bundle the CUDA runtime. The OmniParser weights are not baked in; mount them
# read-only at /weights.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libgl1/libglib2.0-0: OpenCV (pulled in by ultralytics and easyocr).
# libgomp1: paddlepaddle's OpenMP runtime.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# CUDA 12.8 wheels for torch 2.10 (pytorch.org also publishes cu126 and cu130).
# Override with --build-arg TORCH_INDEX_URL=... for another CUDA build or
# https://download.pytorch.org/whl/cpu for a CPU-only image.
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128
RUN pip install --index-url "${TORCH_INDEX_URL}" "torch~=2.10.0" "torchvision~=0.25.0"

# Everything else; the dev tools (mypy, black, isort) stay out of the image.
COPY requirements.txt /tmp/requirements.txt
RUN grep -vE '^(mypy|black|isort)([~=<> ]|$)' /tmp/requirements.txt > /tmp/runtime-requirements.txt \
    && pip install -r /tmp/runtime-requirements.txt \
    && rm /tmp/requirements.txt /tmp/runtime-requirements.txt

RUN useradd --create-home --uid 1000 worker
WORKDIR /app

# The vendored OmniParser code first, so the OCR warm-up layer below survives
# edits to the worker's own source.
COPY omniparser/ /app/vision_worker/omniparser/

# Bake the OCR models (EasyOCR, PaddleOCR) and the Florence-2 processor into
# the image, as the user that runs the worker so they land in its home cache.
# No GPU is needed for this step.
USER worker
RUN python -c "import sys; sys.path.insert(0, '/app/vision_worker/omniparser'); import util.utils; from transformers import AutoProcessor; AutoProcessor.from_pretrained('microsoft/Florence-2-base', trust_remote_code=True)"

USER root
COPY . /app/vision_worker/
USER worker

ENV OMNIPARSER_WEIGHTS_DIR=/weights

ENTRYPOINT ["python", "-m", "vision_worker"]
