# V1 Rescore Runbook (Railway one-off, co-located with Neon)

Runs the SCORING_VERSION 4.0 chain (L0 → L1 → L2) close to the database so
the thousands of small queries pay ~1–2 ms each instead of ~50–100 ms of
internet round-trip from a laptop. The script is `scripts/run_full_rescore.sh`.

## 0. Gate: region MUST be US-East
Co-location only helps if the Railway service runs in **`us-east4`** (Virginia)
— the same metro as Neon `us-east-1`. If the project is in `us-west1` / Europe,
running there is *slower* than your Mac. Check the service's region in Railway
settings; if wrong, set a US-East region for the job service below.

## 1. Create a one-off "rescore" service (same project as the API)
Same project = it shares the region and the `DATABASE_URL` variable.
- New service → deploy from the same repo/branch.
- **Custom Start Command** (overrides the Dockerfile's uvicorn CMD):
  ```
  bash scripts/run_full_rescore.sh
  ```
- **Restart policy: Never** (it's a job — run once and stop, don't loop).
- Ignore the healthcheck failing: this service serves no HTTP; when the script
  finishes, the container exits. The work is done — read the deploy logs.

## 2. Variables
- `DATABASE_URL` = the Neon **pooler** connection string
  (`...-pooler.c-5.us-east-1.aws.neon.tech/neondb?sslmode=require`).
  Reference the shared project variable if it already exists.
- `APP_ENV=production` (keeps SQLAlchemy echo OFF — critical; echo-on was half
  of why the first cobalt run crawled).

## 3. Deploy & watch
Trigger the deploy. The logs show, in order:
`alembic upgrade head` → `rescore-hs-nodes` (L0) → `rescore-market` (L1) →
`rescore-global-rollups` (L2), each with a `time` summary. With the geo-universe
restriction (~5× fewer pairs) + echo off + co-location, expect minutes, not hours.

## 4. Verify, then clean up
- Spot-check a few materials' 4.0 rows (cobalt should still match: CD 66.2 >
  CN 60.7 > ID 41.2; concentration CD 83.2 / CN 85.0).
- When they look right, apply `scripts/run_v1_cleanup.sql` (drops superseded 3.x
  rows + the pre-restriction 4.0 rows dated before today). Destructive — only
  after verification.

## 5. Alternative: `railway ssh`
If your Railway CLI supports SSH into the running API service (already in
us-east), you can skip the extra service and just run
`bash scripts/run_full_rescore.sh` inside it — same co-location, no cleanup of a
job service afterward. Verify the command exists in your CLI version first.

## Notes
- Chemistry (L3) and company scores are intentionally NOT run — off the launch roadmap.
- The image already contains the script (`COPY . .`) and `bdi-ingest` / `alembic` on PATH.
- Re-runnable any time: scoring is a pure recompute (upserts by key), idempotent.
