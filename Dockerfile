FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency manifests first — changes to these invalidate the install layer,
# but changes to app code do not, keeping rebuilds fast.
COPY pyproject.toml uv.lock ./

# Copy the package source so uv sync can install the project itself.
# We copy only the package (not the full repo) so app-code changes don't bust
# the dependency cache layer.
COPY app/ app/

# Install production dependencies only (skip dev group).
# uv sync reads uv.lock and installs everything into the system Python.
RUN uv sync --frozen --no-dev --system

# Copy the rest of the source (alembic, config files, etc.) after deps are cached.
COPY . .

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

EXPOSE 8000

# Railway injects $PORT. Fall back to 8000 for local `docker run`.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
