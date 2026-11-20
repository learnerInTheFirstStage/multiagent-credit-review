# Build from the repo root: docker compose build orchestrator
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

# The fastembed model for search. Off for a bare `docker build`; docker-compose.yml turns it on.
# Before the copies below, so an edit to contracts or data/ does not fetch the model again.
ARG WITH_EMBEDDINGS=0
COPY services/orchestrator/requirements-embed.txt .
RUN if [ "$WITH_EMBEDDINGS" = "1" ]; then \
      pip install --no-cache-dir -r requirements-embed.txt && \
      FASTEMBED_CACHE_PATH=/opt/fastembed python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"; \
    fi
ENV FASTEMBED_CACHE_PATH=/opt/fastembed

COPY packages/contracts /packages/contracts
# The stand-in specialists answer from the shared customer data.
COPY data /data
# The reviews the customers' histories start from. The evaluation mounts the same file over it.
COPY eval/history.yaml /eval/history.yaml
# The built-in embedder needs no package, no model file and no network: search is on wherever this image runs.
ENV EMBEDDINGS_MODEL=hash:384
COPY services/orchestrator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt /packages/contracts

COPY services/orchestrator/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
