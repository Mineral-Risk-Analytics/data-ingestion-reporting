# Dev Environment Deployment Guide
## Railway (FastAPI) + Vercel (Next.js)

---

## Overview

```
Neon Postgres (already running)
        ↑
Railway — battery-data-intelligence-engine (FastAPI)
        ↑
Vercel  — frontend (Next.js)
```

Both services deploy automatically from a GitHub push to `main`. No manual steps after initial setup.

---

## Part 1: Railway — FastAPI Backend

### 1.1 Create the Railway project

1. Go to [railway.app](https://railway.app) → **New Project**
2. Choose **Deploy from GitHub repo** → select `battery-data-intelligence-engine`
3. Railway detects the `Dockerfile` and `railway.toml` automatically
4. Do **not** provision a Railway Postgres — you are using Neon

### 1.2 Set environment variables

In Railway → your service → **Variables**, add:

| Variable | Value | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://...` | Neon **pooled** connection string |
| `DATABASE_URL_DIRECT` | `postgresql+psycopg://...` | Neon **direct** (unpooled) — for Alembic migrations |
| `APP_ENV` | `production` | Disables auth bypass |
| `CLERK_JWKS_URL` | `https://<your-clerk-domain>/.well-known/jwks.json` | From Clerk dashboard → API Keys |
| `CLERK_ISSUER` | `https://<your-clerk-domain>` | Same domain, no path |
| `CLERK_AUDIENCE` | *(leave blank or set per Clerk docs)* | Usually not required for JWKS flow |
| `FRONTEND_URL` | `https://<your-vercel-app>.vercel.app` | Set after Vercel deploy; update if custom domain added |
| `LOG_LEVEL` | `INFO` | |
| `OPENAI_API_KEY` | `sk-...` | Only if report generation uses embeddings |

> **Neon connection strings:** In Neon dashboard → your project → **Connection Details**.
> - Pooled: use the "Pooled connection" string (ends in `-pooler.neon.tech`)
> - Direct: use the standard connection string (no `-pooler`)
>
> Both need the `postgresql+psycopg://` prefix (not `postgresql://`).

### 1.3 Run Alembic migrations

Railway does not run migrations automatically. Options:

**Option A — Railway one-off command (simplest):**
In Railway dashboard → your service → **Deploy** tab → **Start Command** (temporary override):
```
alembic upgrade head
```
Revert to default after migration completes (Railway will re-read `railway.toml`).

**Option B — Railway CLI:**
```bash
railway run alembic upgrade head
```
Install Railway CLI: `npm install -g @railway/cli` then `railway login`.

**Option C — Add a migration step to Dockerfile:**
Not recommended — migrations should not run automatically on every deploy. Use on-demand.

### 1.4 Confirm deployment

Your Railway service URL will be something like `https://battery-data-intelligence-engine-production.up.railway.app`.

Test it:
```
GET https://<your-railway-url>/api/v1/health
```
Should return `{"status": "ok"}`.

---

## Part 2: Vercel — Next.js Frontend

### 2.1 Create the Vercel project

1. Go to [vercel.com](https://vercel.com) → **Add New Project**
2. Import from GitHub → select your frontend repo
3. Framework: **Next.js** (auto-detected)
4. Root directory: leave as `/` unless your repo has a monorepo structure
5. Do not change build settings — Vercel handles Next.js App Router automatically

### 2.2 Set environment variables

In Vercel → your project → **Settings → Environment Variables**:

| Variable | Value | Environment |
|---|---|---|
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | `pk_test_...` | All |
| `CLERK_SECRET_KEY` | `sk_test_...` | All |
| `NEXT_PUBLIC_CLERK_SIGN_IN_URL` | `/login` | All |
| `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL` | `/dashboard` | All |
| `NEXT_PUBLIC_API_URL` | `https://<your-railway-url>` | All |

> Variables prefixed `NEXT_PUBLIC_` are embedded in the client bundle — never put secrets there.
> `CLERK_SECRET_KEY` is server-only and safe.

### 2.3 Confirm deployment

Vercel gives you a URL like `https://<project>.vercel.app`.

After Vercel deploys, go back to Railway and update `FRONTEND_URL` to this URL. This is required for CORS — the FastAPI backend will reject requests from unknown origins otherwise.

---

## Part 3: Connecting Clerk

### 3.1 Get your JWKS URL

In Clerk dashboard → **API Keys**:
- Your frontend API URL (Publishable Key domain) looks like `https://clerk.your-domain.com`
- JWKS URL: append `/.well-known/jwks.json`

For development Clerk apps the domain looks like `https://flexible-fox-42.clerk.accounts.dev`.

### 3.2 Add your Railway URL to Clerk allowed origins

In Clerk dashboard → **Paths** or **Domains**:
- Add your Railway URL as an allowed origin if Clerk requires it (depends on Clerk version)
- Add your Vercel URL as the application URL

### 3.3 Auth bypass (local dev only)

The backend auth is bypassed when `APP_ENV=development` AND `CLERK_JWKS_URL` is empty.
This means locally you never need a real Clerk token. In Railway, `APP_ENV=production` enforces real JWTs.

---

## Part 4: Ongoing Workflow

### Deploy a backend change
```bash
git push origin main
```
Railway redeploys automatically. Build time is typically 2–3 minutes (Docker layer cache makes subsequent builds faster).

### Deploy a frontend change
```bash
git push origin main
```
Vercel redeploys automatically. Build time is typically 30–60 seconds.

### Run migrations after a schema change
```bash
# Locally: generate the migration
alembic revision --autogenerate -m "describe the change"

# Review the generated file in alembic/versions/ before applying

# Apply locally
alembic upgrade head

# Apply to Railway dev environment
railway run alembic upgrade head
```

### View Railway logs
```bash
railway logs
# or in the Railway dashboard → Deployments → click a deployment → Logs
```

---

## Part 5: Environment variable checklist before first deploy

- [ ] Neon pooled connection string added to Railway as `DATABASE_URL`
- [ ] Neon direct connection string added to Railway as `DATABASE_URL_DIRECT`
- [ ] `APP_ENV=production` set in Railway
- [ ] Clerk JWKS URL, issuer set in Railway
- [ ] Alembic migrations run against Railway's environment (which points at Neon)
- [ ] Railway deploy succeeds, `/api/v1/health` returns 200
- [ ] Vercel project connected to frontend GitHub repo
- [ ] All four Clerk + API URL env vars set in Vercel
- [ ] Vercel URL added to Railway `FRONTEND_URL` (CORS)
- [ ] `/dashboard` loads and Clerk sign-in works

---

## Notes on the dev environment

This setup uses one environment (Railway dev + Vercel preview) pointing at your existing Neon database. That is fine for now — you do not have real client data yet and standing up a second Neon project just for a dev environment adds overhead without benefit.

When you eventually approach a real client delivery, the right move is to add a separate Neon branch (Neon supports DB branching natively) and a Railway staging environment. Do not worry about that until the product is built.
