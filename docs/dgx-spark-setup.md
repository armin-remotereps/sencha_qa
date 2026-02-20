# DGX Spark — Docker Model Runner Setup Guide

## Prerequisites

- NVIDIA DGX Spark with Ubuntu and Docker Desktop installed
- Docker Model Runner enabled (Docker Desktop > Settings > Features > Model Runner)
- SSH access to the DGX Spark from your development machine

## 1. Pull Required Models

```bash
# Main agent brain — text-only model for reasoning + tool calling
docker model pull ai/qwen3:32B-Q8_K_XL

# Vision model — for screenshot analysis (only needed if VISION_BACKEND=dmr)
docker model pull ai/qwen3-vl:32B-Q8_K_XL

# Summarizer model — lightweight model for output/context summarization
docker model pull ai/mistral
```

### Verify models are pulled

```bash
docker model ls
```

## 2. Configure Context Window

Docker Model Runner defaults to **4096 tokens** context — far too small for an agent loop.
DGX Spark has 128GB unified memory, so we can afford a much larger context.

### Set context size per model

```bash
# Agent brain: 32k context (32B Q8 model uses ~35GB for weights + ~4GB for 32k context)
docker model configure --context-size 32768 ai/qwen3:32B-Q8_K_XL

# Vision model (if using DMR vision): 8k is enough since it only processes one screenshot at a time
docker model configure --context-size 8192 ai/qwen3-vl:32B-Q8_K_XL

# Summarizer: 8k is plenty for summarization tasks
docker model configure --context-size 8192 ai/mistral
```

### Verify context configuration

After sending a request to each model, check the logs:

```bash
docker compose logs 2>&1 | grep -i "n_ctx"
```

Or test with a request and check if long prompts work without errors.

### Alternative: Repackage with baked-in context

If `docker model configure` doesn't stick across restarts:

```bash
docker model package --from ai/qwen3:32B-Q8_K_XL --context-size 32768 ai/qwen3:32B-Q8_K_XL-32k
docker model package --from ai/qwen3-vl:32B-Q8_K_XL --context-size 8192 ai/qwen3-vl:32B-Q8_K_XL-8k
docker model package --from ai/mistral --context-size 8192 ai/mistral:8k
```

Then update your `.env` to use the repackaged model names.

## 3. Expose DMR API

Docker Model Runner listens on `localhost:12434` by default inside the DGX Spark.

### For remote access via SSH tunnel (from dev machine)

```bash
ssh -L 12435:localhost:12434 user@dgx-spark-ip
```

This forwards your local port `12435` to the DGX Spark's DMR port `12434`.
Set `DMR_PORT=12435` in your `.env` on the dev machine.

### For direct network access (production)

If the Django app runs on the same DGX Spark, use `DMR_HOST=localhost` and `DMR_PORT=12434`.

## 4. Memory Budget (128GB DGX Spark)

| Component | Estimated Memory |
|---|---|
| qwen3:32B-Q8_K_XL weights | ~35 GB |
| qwen3:32B-Q8_K_XL 32k context | ~4 GB |
| qwen3-vl:32B-Q8_K_XL weights | ~35 GB |
| qwen3-vl:32B-Q8_K_XL 8k context | ~1 GB |
| mistral weights | ~4 GB |
| mistral 8k context | ~0.5 GB |
| OS + system overhead | ~8 GB |
| **Total** | **~88 GB** |

This leaves ~40GB headroom. If memory is tight, you can:
- Use `qwen3-vl` only when `VISION_BACKEND=dmr` (skip pulling it if using OpenAI for vision)
- Use a smaller quantization (Q4_K_M) for the summarizer model

Note: Docker Model Runner loads models on demand and may unload idle models.
Only the actively-used models consume memory at any given time.

## 5. Verify Everything Works

### Test the agent brain model

```bash
curl http://localhost:12434/engines/llama.cpp/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ai/qwen3:32B-Q8_K_XL",
    "messages": [{"role": "user", "content": "Say hello"}],
    "max_tokens": 100,
    "temperature": 0.1
  }'
```

### Test tool calling

```bash
curl http://localhost:12434/engines/llama.cpp/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ai/qwen3:32B-Q8_K_XL",
    "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}],
    "tools": [{"type": "function", "function": {"name": "get_weather", "description": "Get weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
    "tool_choice": "auto",
    "max_tokens": 200,
    "temperature": 0.1
  }'
```

Verify the response contains a `tool_calls` array with `get_weather` and `{"city": "Tokyo"}`.

### Test the summarizer

```bash
curl http://localhost:12434/engines/llama.cpp/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "ai/mistral",
    "messages": [{"role": "user", "content": "Summarize: The quick brown fox jumped over the lazy dog."}],
    "max_tokens": 100,
    "temperature": 0.0
  }'
```

## 6. Troubleshooting

| Symptom | Likely Cause | Fix |
|---|---|---|
| Empty or truncated responses | `max_tokens` too high relative to context window | Lower `DMR_MAX_TOKENS` to 4096 |
| Model returns garbage / malformed JSON tool calls | Temperature too high | Set `DMR_TEMPERATURE=0.1` |
| "context window exceeded" errors | Context size not configured | Run `docker model configure --context-size` |
| Slow first request (~30s+) | Model loading into memory | Normal on first request; subsequent requests are fast |
| Summarizer fails | `ai/mistral` not pulled | Run `docker model pull ai/mistral` |
| Tool calls never generated | Using VL model for text-only reasoning | Switch `DMR_MODEL` to `ai/qwen3:32B-Q8_K_XL` (text model) |

## References

- [Docker Model Runner Docs](https://docs.docker.com/ai/model-runner/)
- [Docker Model Runner Configuration](https://docs.docker.com/ai/model-runner/configuration/)
- [Docker Model Runner on DGX Spark](https://www.docker.com/blog/new-nvidia-dgx-spark-docker-model-runner/)
- [Context Size Config Guide](https://www.glukhov.org/post/2025/11/context-size-in-docker-model-runner/)
- [Context Packing with DMR](https://www.docker.com/blog/context-packing-context-window/)
- [Qwen3 on Docker Hub](https://hub.docker.com/r/ai/qwen3)
- [Qwen3-VL on Docker Hub](https://hub.docker.com/r/ai/qwen3-vl)
