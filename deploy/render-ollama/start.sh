#!/usr/bin/env sh
set -eu

export OLLAMA_HOST="0.0.0.0:${PORT:-11434}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-/var/lib/ollama}"
MODEL="${OLLAMA_MODEL:-qwen3:8b}"

mkdir -p "${OLLAMA_MODELS}"

ollama serve &
SERVER_PID="$!"

echo "Waiting for Ollama on ${OLLAMA_HOST}..."
until ollama list >/dev/null 2>&1; do
  sleep 1
done

if ! ollama list | awk '{print $1}' | grep -qx "${MODEL}"; then
  echo "Pulling ${MODEL} into ${OLLAMA_MODELS}..."
  ollama pull "${MODEL}"
fi

echo "Ollama ready with model ${MODEL}"
wait "${SERVER_PID}"
