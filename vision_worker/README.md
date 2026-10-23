# Vision worker

The vision worker runs OmniParser (the "visual engine") on a GPU machine and
parses Test Runner screenshots for the Punk Hazard server. Like the Test
Runner, it connects **out** to the server over a WebSocket
(`wss://<panel>/ws/vision-worker/`), so the GPU machine needs no open port.
Several workers can be connected at once (one per GPU); the server spreads
parse requests over the ready ones round robin. Runs cannot start until at
least one worker is ready.

Screenshots stay in memory: the worker never writes images to disk and logs
only sizes and timings.

## 1. Server side

Generate a token and put it in the **server's** `.env`, then restart the server:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
# server .env
VISION_WORKER_TOKEN=<that value>
```

Every worker uses the same token. When it is empty the server rejects every
worker. To rotate it, change it in the server `.env` and in every worker
`.env`, then restart the server and the workers.

## 2. Set up the worker

Requires Python 3.13 and, for a GPU, a recent NVIDIA driver. From the
repository root:

```bash
# Linux or macOS
vision_worker/scripts/setup.sh             # or: --python python3.13
# Windows (PowerShell)
.\vision_worker\scripts\setup.ps1          # or: -Python <full path to python.exe 3.13>
```

The script creates `vision_worker/.venv`, installs torch (the CUDA 12.8 build
when `nvidia-smi` is found, otherwise CPU; the standard build on macOS),
installs `requirements.txt`, downloads the model weights (~1.5 GB),
pre-downloads the OCR models, and creates `vision_worker/.env` from
`example.env`.

Then edit `vision_worker/.env`:

```
VISION_SERVER_URL=wss://<your-panel-domain>/ws/vision-worker/
VISION_WORKER_TOKEN=<same token as the server>
```

`VISION_SERVER_URL` must be `wss://` unless the server is on localhost or a
private (LAN) address. The other settings in `example.env` have working
defaults and are explained there.

## 3. Run

From the repository root:

```bash
# Linux or macOS
vision_worker/.venv/bin/python -m vision_worker.main
# Windows (PowerShell)
vision_worker\.venv\Scripts\python -m vision_worker.main
```

The worker reconnects on its own after network drops. It stops for good when
the server rejects its token or version, or when another worker connects with
the same name.

## 4. Several GPUs

Start one worker per GPU. `CUDA_VISIBLE_DEVICES` picks the GPU, and the
default worker name includes it, so each process gets its own name:

```bash
# Linux
CUDA_VISIBLE_DEVICES=0 vision_worker/.venv/bin/python -m vision_worker.main
CUDA_VISIBLE_DEVICES=1 vision_worker/.venv/bin/python -m vision_worker.main
```

```powershell
# Windows (PowerShell), each in its own window
$env:CUDA_VISIBLE_DEVICES="0"; vision_worker\.venv\Scripts\python -m vision_worker.main
$env:CUDA_VISIBLE_DEVICES="1"; vision_worker\.venv\Scripts\python -m vision_worker.main
```

## 5. Diagnose

```bash
vision_worker/.venv/bin/python -m vision_worker.diagnose                 # generated test image
vision_worker/.venv/bin/python -m vision_worker.diagnose --image shot.png
vision_worker/.venv/bin/python -m vision_worker.diagnose --check-server  # also connect to the server
```

It checks, stopping at the first failure: the imports, the weights, the
device (with GPU name and VRAM), the model load, one inference, the result
size, and optionally the server handshake. The handshake uses the name
`<worker name>-diagnose`, so it does not replace a running worker.

## 6. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Invalid configuration: VISION_SERVER_URL uses ws:// to the public host` | Use `wss://`, or set `VISION_ALLOW_INSECURE=true` if you accept unencrypted screenshots. |
| `Handshake rejected (error): Invalid token` | The worker token differs from the server's `VISION_WORKER_TOKEN`, or the server's is empty. |
| `Handshake rejected (incompatible)` | The worker is older or newer than the server expects. Update both from the same checkout. |
| `Another vision worker connected with the same name` | Two workers share `VISION_WORKER_NAME`. Give each its own. |
| Worker stays `loading` for minutes on first start | The OCR models are downloading. Re-run the setup script so they are cached. |
| `VISION_DEVICE=cuda but torch sees no CUDA device` | Check `nvidia-smi`, then delete `vision_worker/.venv` and re-run the setup script (a plain PyPI torch on Windows is CPU-only). |
| `OmniParser weights not found` | Re-run the setup script, or point `OMNIPARSER_WEIGHTS_DIR` at a folder with `icon_detect/` and `icon_caption_florence/`. |
| CUDA out of memory | One worker per GPU; lower `OMNIPARSER_CAPTION_BATCH_SIZE`. |
| Server shows the worker as offline although it runs | No heartbeat reached the server for 90 s. Check the network path and the worker log for reconnects. |

## Development

The worker has its own strict mypy config:

```bash
vision_worker/.venv/bin/mypy --config-file vision_worker/setup.cfg vision_worker/
```

`protocol.py` and `exceptions.py` are standard-library only, because the
server imports `vision_worker.protocol` directly. Keep torch and the vendored
`omniparser/` code out of them.
