FROM ollama/ollama:latest

ENV OLLAMA_MODELS=/var/lib/ollama

COPY start.sh /start.sh
RUN chmod +x /start.sh

ENTRYPOINT ["/start.sh"]
