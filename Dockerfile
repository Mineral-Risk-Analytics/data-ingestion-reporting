# Optional: production-style image for API + workers (Phase 1: API only)
FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv && uv pip install --system -e ".[dev]"

COPY . .

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
