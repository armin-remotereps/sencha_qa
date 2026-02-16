FROM python:3.13-slim AS builder

WORKDIR /src

RUN python3.13 -m venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH"

RUN apt update && apt install -y libpq-dev build-essential python3-dev gcc

COPY ./requirements.txt ./

RUN pip install -r requirements.txt

FROM python:3.13-slim

WORKDIR /src

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY . .