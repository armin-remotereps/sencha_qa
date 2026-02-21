FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04 AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY omniparser_service/requirements.txt .
RUN python3 -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir \
        torch torchvision --index-url https://download.pytorch.org/whl/cu128 && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


FROM nvidia/cuda:12.8.1-runtime-ubuntu24.04

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 libgl1 libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY OmniParser/ /app/OmniParser/
COPY omniparser_service/ /app/omniparser_service/

EXPOSE 8080

CMD ["uvicorn", "omniparser_service.main:app", "--host", "0.0.0.0", "--port", "8080"]
