FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency manifests first — changes to these invalidate the install layer,
# but changes to app code do not, keeping rebuilds fast.
COPY pyproject.toml uv.lock ./

# Install production dependencies only (no dev extras).
# --no-editable installs the package itself from the sdist, not a live editable link.
RUN uv pip install --system --no-dev --no-editable .

# Copy the rest of the source after dependencies are installed.
COPY . .

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

EXPOSE 8000

# Railway injects $PORT. Fall back to 8000 for local `docker run`.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
