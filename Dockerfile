FROM python:3.13-slim

WORKDIR /src

RUN python3.13 -m venv /opt/venv

ENV PATH="/opt/venv/bin$PATH"

RUN apt update && apt install libpq-dev build-essential python3-dev gcc

COPY ./requirements.txt ./

RUN pip install -r requirements.txt

COPY . .