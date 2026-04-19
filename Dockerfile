FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency manifests first — changes to these invalidate the install layer,
# but changes to app code do not, keeping rebuilds fast.
COPY pyproject.toml uv.lock README.md ./

# Copy the package source so uv sync can install the project itself.
# We copy only the package (not the full repo) so app-code changes don't bust
# the dependency cache layer.
COPY app/ app/

# Tell uv to install into the system Python rather than creating a venv.
# UV_SYSTEM_PYTHON is the correct way to do this for uv sync (--system is a
# uv pip flag and does not exist on uv sync).
ENV UV_SYSTEM_PYTHON=1

# Install production dependencies only (skip dev group).
RUN uv sync --frozen --no-dev

# Copy the rest of the source (alembic, config files, etc.) after deps are cached.
COPY . .

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

EXPOSE 8000

# Railway injects $PORT. Fall back to 8000 for local `docker run`.
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
