# Render Ollama Service

This folder is a separate Render service for running Ollama with `qwen3:8b`.

Recommended Render setup:

- Service type: Private Service if your API is also on Render.
- Runtime: Docker.
- Root Directory: `deploy/render-ollama`.
- Dockerfile path: `Dockerfile`.
- Disk mount path: `/var/lib/ollama`.
- Environment:

```env
OLLAMA_MODEL=qwen3:8b
OLLAMA_MODELS=/var/lib/ollama
```

If Render gives this service a private URL such as:

```text
http://ollama-qwen:11434
```

set the FastAPI service environment:

```env
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama-qwen:11434
OLLAMA_MODEL=qwen3:8b
ENABLE_LLM_EXPLANATIONS=true
ENABLE_LLM_CARRIER_NORMALIZATION=true
```

Do not expose this service publicly unless you put authentication in front of it.
