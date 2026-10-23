# Vision worker

The vision worker runs OmniParser (the "visual engine") on a GPU machine and
parses Test Runner screenshots for the Punk Hazard server. It connects **out**
to the server over a WebSocket (`wss://<panel>/ws/vision-worker/`), so the GPU
machine needs no open port. Several workers can be connected at once (one per
GPU); the server spreads parse requests over the ready ones round robin.

What it does:

1. Connects, authenticates with the shared token, and reports its GPU.
2. Loads the OmniParser models and reports `loading` → `ready` (or `failed`).
3. Sends a status heartbeat every 30 s.
4. Parses the screenshots the server sends, one at a time, and sends back the
   elements and an annotated image. A worker holds at most one running and
   `VISION_MAX_QUEUE` waiting requests; beyond that it answers `BUSY` and the
   server tries another worker.

Screenshots stay in memory: the worker never writes images to disk and logs
only sizes and timings. Runs cannot start until at least one worker is ready.

## 1. Server side

Generate a token and put it in the **server's** `.env`, then restart Daphne:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# server .env
VISION_WORKER_TOKEN=<that value>
```

Every worker uses the same token. When it is empty the server rejects every
worker. To rotate it, change it in the server `.env` and in every worker
`.env`, then restart Daphne and the workers.

## 2. Worker configuration

All settings come from `vision_worker/.env` (copy `example.env`) or the
environment:

| Setting | Default | Meaning |
|---|---|---|
| `VISION_SERVER_URL` | — | e.g. `wss://panel.example.com/ws/vision-worker/`. Must be `wss://` unless the host is loopback or a private address. |
| `VISION_ALLOW_INSECURE` | `false` | Allow `ws://` to a public host. Screenshots then travel unencrypted. |
| `VISION_WORKER_TOKEN` | — | Must equal the server's `VISION_WORKER_TOKEN`. Never logged. |
| `VISION_WORKER_NAME` | `<hostname>-<device>` | Stable, unique per worker. A second worker with the same name replaces the first, which then exits. |
| `VISION_DEVICE` | `auto` | `auto` (cuda → mps → cpu), `cuda`, `mps` or `cpu`. |
| `VISION_MAX_QUEUE` | `1` | Requests that may wait behind the running one. |
| `VISION_MAX_RESULT_BYTES` | `8388608` | Upper bound of one result message; the annotated image is downscaled to fit. Keep it below 32 MiB. |
| `VISION_RECONNECT_INTERVAL` / `VISION_RECONNECT_MAX_INTERVAL` | `5` / `60` | Reconnect backoff, in seconds. The worker reconnects forever. |
| `VISION_LOG_LEVEL` | `INFO` | |
| `OMNIPARSER_WEIGHTS_DIR` | `vision_worker/omniparser/weights` | Folder with `icon_detect/` and `icon_caption_florence/`. Docker uses `/weights`. |
| `OMNIPARSER_BOX_THRESHOLD`, `OMNIPARSER_IOU_THRESHOLD`, `OMNIPARSER_CAPTION_BATCH_SIZE` | `0.05`, `0.7`, `64` | Model settings. |

The worker stops for good (exit code 0) when the server rejects the token or
the version, or when another worker with the same name takes over. It exits
with code 2 on an invalid configuration. Network drops are retried.

## 3. Run in a virtual environment

Requires Python 3.13 and, for a GPU, a recent NVIDIA driver.

```bash
# Linux, WSL2 or macOS, from the repository root
vision_worker/scripts/setup.sh            # or: --python python3.13
# Windows (PowerShell), natively
.\vision_worker\scripts\setup.ps1          # or: -Python py -3.13
```

The setup script creates `vision_worker/.venv`, installs torch, installs
`requirements.txt`, downloads the weights (~1.5 GB) with
`scripts/download_weights.sh`, pre-downloads the OCR models and the Florence-2
processor, and creates `.env` from `example.env`.

**Torch builds.** torch and torchvision are installed first, from the index
that matches the machine, and `requirements.txt` then keeps those versions:

```bash
# NVIDIA GPU (Linux or Windows): CUDA 12.8 wheels, which bundle the CUDA runtime
pip install --index-url https://download.pytorch.org/whl/cu128 "torch~=2.10.0" "torchvision~=0.25.0"
# CPU only
pip install --index-url https://download.pytorch.org/whl/cpu "torch~=2.10.0" "torchvision~=0.25.0"
# macOS (MPS): plain PyPI
pip install "torch~=2.10.0" "torchvision~=0.25.0"
pip install -r vision_worker/requirements.txt
```

For torch 2.10, pytorch.org publishes `cu126`, `cu128` and `cu130` wheels.
`cu128` is used here because it covers current GPUs (including RTX 50-series)
with drivers from early 2025 on. Use `cu126` for an older driver.

Then, from the repository root:

```bash
vision_worker/.venv/bin/python -m vision_worker            # Windows: vision_worker\.venv\Scripts\python -m vision_worker
```

## 4. Run with Docker

The image (`vision_worker/Dockerfile`, build context `vision_worker/`) is
`python:3.13-slim` with torch from the CUDA 12.8 wheel index, the OCR models
baked in, and a non-root user. The weights are **not** in the image: download
them once on the host and mount them read-only at `/weights`.

```bash
cd vision_worker
cp example.env .env                     # set VISION_SERVER_URL and VISION_WORKER_TOKEN
scripts/download_weights.sh             # needs `pip install "huggingface_hub[cli]~=0.36.2"` without the venv
docker compose up -d --build
docker compose logs -f
```

`VISION_WEIGHTS_PATH` (in the shell or in `.env`) points the mount at another
host folder; the default is `./omniparser/weights`. `VISION_WORKER_NAME_PREFIX`
sets the first part of the worker names (`<prefix>-gpu0`, `<prefix>-gpu1`;
default `gpu-pc`). Pass `--build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126`
for another CUDA build.

From inside a container, a server on the same machine is not `localhost`. Use
its LAN address (a private address may use `ws://`) or its public `wss://`
URL. `host.docker.internal` counts as a public name, so it needs `wss://` or
`VISION_ALLOW_INSECURE=true`.

### Linux

Install the NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
then check that containers see the GPU:

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

### Windows with Docker Desktop

- Use the **WSL2 backend** (Settings → General → "Use the WSL 2 based
  engine"). GPU access does not work with the Hyper-V backend.
- Install a recent **NVIDIA Windows driver**. It provides CUDA inside WSL2. Do
  **not** install a Linux NVIDIA driver inside WSL, and no separate Container
  Toolkit is needed: Docker Desktop brings GPU support.
- Check it in PowerShell: `docker run --rm --gpus all ubuntu nvidia-smi`.
- Run `docker compose up -d --build` from `vision_worker\` as above. Download
  the weights with `setup.ps1`, or with `hf download microsoft/OmniParser-v2.0 --local-dir omniparser\weights`
  and then rename `icon_caption` to `icon_caption_florence`.
- **Weights location.** A bind mount from a Windows drive (`C:\...`) crosses
  the WSL2 file-system boundary and makes the model load noticeably slower.
  For faster starts keep the weights inside the WSL file system (for example
  clone the repo under `\\wsl$\Ubuntu\home\<you>\` and run compose from there)
  or copy them into a named Docker volume and point the mount at it.

## 5. Several GPUs

Run one worker per GPU, each with its own name:

- **Docker:** `docker-compose.yml` has one service per GPU, pinned with
  `device_ids`. GPU 0 starts by default; start GPU 1 with
  `docker compose --profile gpu1 up -d`. Copy the `vision-worker-gpu1` block
  for more GPUs.
- **venv:** start one process per GPU with `CUDA_VISIBLE_DEVICES` and a name:

  ```bash
  CUDA_VISIBLE_DEVICES=0 VISION_WORKER_NAME=my-pc-gpu0 vision_worker/.venv/bin/python -m vision_worker
  CUDA_VISIBLE_DEVICES=1 VISION_WORKER_NAME=my-pc-gpu1 vision_worker/.venv/bin/python -m vision_worker
  ```

  `cuda:N` device names are not supported: EasyOCR always uses the default
  device, so the GPU is selected by hiding the others.

A CPU worker can sit next to GPU workers, but every request that lands on it
is much slower.

## 6. Diagnose

```bash
vision_worker/.venv/bin/python -m vision_worker.diagnose                 # generated test image
vision_worker/.venv/bin/python -m vision_worker.diagnose --image shot.png
vision_worker/.venv/bin/python -m vision_worker.diagnose --check-server  # also complete a handshake
docker compose run --rm --entrypoint python vision-worker-gpu0 -m vision_worker.diagnose --check-server
```

It checks, stopping at the first failure: the imports, the weights, the
device (with GPU name and VRAM), the model load, one inference, the result
size against `VISION_MAX_RESULT_BYTES`, and optionally the server handshake.
The handshake uses the name `<worker name>-diagnose`, so it does not replace a
running worker; it shows up briefly on the Environment page.

## 7. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Invalid configuration: VISION_SERVER_URL uses ws:// to the public host` | Use `wss://`, or set `VISION_ALLOW_INSECURE=true` if you accept unencrypted screenshots. |
| `Handshake rejected (error): Invalid token` | The worker token differs from the server's `VISION_WORKER_TOKEN`, or the server's is empty. |
| `Handshake rejected (incompatible)` | The worker is older or newer than the server expects. Update both from the same checkout. |
| `Another vision worker connected with the same name` | Two workers share `VISION_WORKER_NAME`. Give each its own. |
| Worker stays `loading` for minutes on first start | The OCR models or the Florence-2 processor are downloading. Re-run the setup script (venv) or rebuild the image so they are cached. |
| `VISION_DEVICE=cuda but torch sees no CUDA device` | Check `nvidia-smi`; reinstall torch from the CUDA index (a plain PyPI install on Windows is CPU-only); in Docker check `docker run --rm --gpus all ubuntu nvidia-smi`. |
| `OmniParser weights not found` | Run `scripts/download_weights.sh`, or point `OMNIPARSER_WEIGHTS_DIR` / the `/weights` mount at the folder with `icon_detect/` and `icon_caption_florence/`. |
| `parse result is ... bytes, above the ...-byte limit` | Very large screenshot; raise `VISION_MAX_RESULT_BYTES` (below 32 MiB). |
| CUDA out of memory | One worker per GPU; lower `OMNIPARSER_CAPTION_BATCH_SIZE`. |
| `VISION_DEVICE=cpu` still uses the GPU | OCR and the detector pick CUDA when they see it. Also set `CUDA_VISIBLE_DEVICES=` (empty). |
| Server shows the worker as offline although it runs | No heartbeat reached the server for 90 s. Check the network path and the worker log for reconnects. |

## Development

The worker has its own strict mypy config:

```bash
vision_worker/.venv/bin/mypy --config-file vision_worker/setup.cfg vision_worker/
```

`protocol.py` and `exceptions.py` are standard-library only, because the
server imports `vision_worker.protocol` directly. Keep torch and the vendored
`omniparser/` code out of them.
