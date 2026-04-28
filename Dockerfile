FROM python:3.12-slim

WORKDIR /app

# Install uv
RUN pip install --no-cache-dir uv

# Copy dependency manifests first — changes to these invalidate the install layer,
# but changes to app code do not, keeping rebuilds fast.
COPY pyproject.toml uv.lock README.md ./

# Copy the package source so uv pip install can build the project.
# Only the package dir is copied here so app-code changes don't bust the dep cache.
COPY app/ app/

# Install the project + all runtime deps into the system Python.
# uv pip install --system puts everything in /usr/local/lib/python3.12/site-packages
# so `python -m uvicorn` finds it without a virtualenv.
# Dev dependencies (defined in [tool.uv.dev-dependencies]) are NOT installed by default.
RUN uv pip install --system .

# Copy the rest of the source (alembic, config files, etc.) after deps are cached.
COPY . .

ENV PYTHONUNBUFFERED=1
ENV APP_ENV=production

EXPOSE 8000

# Railway injects $PORT. Fall back to 8000 for local `docker run`.
# Migrations run first — if alembic fails, the container exits and Railway
# marks the deploy failed before any traffic hits the new code.
CMD alembic upgrade head && python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
