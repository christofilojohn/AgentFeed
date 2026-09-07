# AgentFeed. Ollama runs alongside rather than inside: models are large,
# and baking them into an image makes it unusable.
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential libxml2-dev libxslt1-dev curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY agentfeed ./agentfeed
COPY ui ./ui
RUN pip install --no-cache-dir -e .

ENV AGENTFEED_DATA_DIR=/data \
    AGENTFEED_HOST=0.0.0.0 \
    AGENTFEED_PORT=8770 \
    AGENTFEED_LLM_BASE_URL=http://ollama:11434/v1
VOLUME ["/data"]
EXPOSE 8770

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://localhost:8770/api/health || exit 1

CMD ["agentfeed", "serve"]
