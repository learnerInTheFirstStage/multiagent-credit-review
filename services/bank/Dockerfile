# Build from the repo root: docker compose build bank
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY packages/contracts /packages/contracts
COPY services/bank/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt /packages/contracts

COPY services/bank/app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
