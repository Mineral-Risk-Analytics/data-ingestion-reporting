#!/usr/bin/env bash
# V1 (SCORING_VERSION 4.0) full rescore — run CO-LOCATED with Neon (us-east-1).
# Order is load-bearing: L0 (hs nodes) -> L1 (market/material×geo) -> L2 (global).
# Chemistry (L3) and company scores are NOT run — off the launch roadmap.
# Cleanup of superseded 3.x / stale rows is a SEPARATE post-verification step
# (run_v1_cleanup.sql) applied only after the fresh scores look right.
#
# Requires: DATABASE_URL (Neon pooler string) and APP_ENV=production (keeps
# SQLAlchemy echo OFF — the Docker image sets this by default).
# Runs inside the app image: bdi-ingest + alembic are on PATH.
set -euo pipefail

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
: "${DATABASE_URL:?set DATABASE_URL to the Neon pooler connection string}"
export APP_ENV="${APP_ENV:-production}"
log "DB host: $(echo "$DATABASE_URL" | sed -E 's#.*@([^/?]+).*#\1#')   APP_ENV=$APP_ENV"

log "── migrations (alembic upgrade head) ──"
alembic upgrade head

log "── L0: rescore-hs-nodes ──"
time bdi-ingest rescore-hs-nodes

log "── L1: rescore-market (all materials) ──"
time bdi-ingest rescore-market

log "── L2: rescore-global-rollups ──"
time bdi-ingest rescore-global-rollups

log "── RESCORE COMPLETE — scoring_version 4.0 written across L0/L1/L2. ──"
log "Next: verify scores, then apply run_v1_cleanup.sql to drop 3.x + the"
log "pre-restriction 4.0 (2026-07-17) rows. Chemistry/company scores skipped."
