# Full Autopilot Guide — Error Sweep, Fixes, and End-to-End Deployment

This is the complete, copy-pasteable path from a fresh checkout to a live,
verified, auto-deploying production site. Every step has been run against this
repo — the errors that were found are already fixed in the codebase.

---

## 0. Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.11+ (3.12 in CI) | Backend |
| Node | 22+ | Frontend tests, Wrangler CLI |
| GitHub CLI (`gh`) | any recent | Secrets sync, repo automation |
| Docker Desktop | optional | Local full-stack with Postgres |
| Cloudflare account | free tier | Edge frontend |
| Render account | free tier | Backend hosting |
| Neon account | free tier | Postgres that doesn't auto-delete |

All accounts can be free-tier. The only paid upgrades needed are if you outgrow
them (Render free sleeps after 15 min idle; Neon free autosuspends compute but
never deletes data).

---

## 1. Local environment setup

### Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements-dev.txt   # pulls in requirements.txt too
```

Copy `backend/.env.example` to `backend/.env`. With nothing set, the app runs
on SQLite and logs emails to the console instead of sending them.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://127.0.0.1:8000/` — the frontend is served from the same origin in
local dev.

### Frontend (static, no build step)

```bash
cd frontend
npm install        # devDependencies for vitest only
npm test
```

The shipped site has no build step — `index.html`, `admin.html`, `css/`,
`js/` are served as-is. `npm` here is only for running the test suite in CI.

### Docker Compose (optional, local Postgres)

```bash
docker compose up --build
# open http://localhost:8000/
```

---

## 2. Verify everything passes locally

```bash
# Backend lint + tests
cd backend
python -m ruff check .          # must print: All checks passed!
python -m pytest tests -v        # ~120 tests, all should pass

# Frontend tests
cd ../frontend
npm test                        # 29 tests, all should pass
```

If any of these fail, stop and fix before deploying — CI runs the exact same
commands and will block the deploy on the same failures.

---

## 3. What was found and fixed (error sweep results)

Run this against any clone of the repo to confirm the fixes are in place:

| Problem | Where | Fix |
|---------|-------|-----|
| **Duplicate class definitions** — `ApplicantProfile`, `SubmissionLedger`, `ApplicationChecklist` each defined twice | `backend/app/models.py` | Removed the second (duplicate) block; the first definition is canonical |
| **Duplicate schema definitions** — 7 Pydantic models each defined twice | `backend/app/schemas.py` | Removed the second (duplicate) block |
| **Admin listings router never mounted** — every `/api/v1/admin/opportunities/*` endpoint returned 405 | `backend/app/main.py` | Added `admin_listings` to the import list and `app.include_router(admin_listings.router, prefix="/api/v1")` |
| **39 ruff lint errors** — unused imports, quoted type annotations (UP037), long lines (E501) | across `models.py`, `schemas.py`, `routes/applicant.py`, `scrapers/ranking.py`, `main.py` | Auto-fixed 22 with `ruff check --fix`; manually fixed the rest (extracted long regex patterns into module constants, removed unused imports, unquoted forward refs) |
| **Frontend tests** | `frontend/tests/` | All 29 pass — no fixes needed (jsdom `window.scrollTo` warning is a known jsdom limitation, not a failure) |
| **GitHub Actions workflows** | `.github/workflows/` | No issues — CI runs ruff + pytest + frontend tests; CodeQL scans weekly; keep-alive pings Render every 10 min |

After the fixes:
- `ruff check .` → **All checks passed!**
- `pytest tests` → **123 passed**
- `npm test` → **29 passed**
- `from app.main import app` → imports cleanly

---

## 4. Set up GitHub repository secrets

These secrets are used by CI for optional scraping keys. CI runs without them
(SQLite + smoke tests), but adding them improves production scraping.

```bash
gh auth login
gh secret set GOOGLE_API_KEY --repo <your-org>/<your-repo> --body "YOUR_KEY"
gh secret set GOOGLE_CSE_ID  --repo <your-org>/<your-repo> --body "YOUR_CX_ID"
```

Or sync a local `.env` in bulk (PowerShell):

```powershell
.\scripts\sync-github-secrets.ps1 -Repo <your-org>/<your-repo> -EnvFile backend\.env
```

The script skips placeholder values (`your_*_here`) and empty/commented lines.
See `.github/SECRETS.md` for the full secret list.

---

## 5. Deploy the backend to Render

### 5a. Create the database (Neon — not Render's own Postgres)

1. Sign up at [neon.tech](https://neon.tech), create a free project.
2. Copy the connection string (starts with `postgres://` or `postgresql://`).
3. **Why Neon, not Render Postgres:** Render's free Postgres auto-deletes the
   database 30 days after creation — this already caused a production outage.
   Neon's free tier only autosuspends compute when idle; data persists forever.

### 5b. Deploy the backend

1. Sign up at [render.com](https://render.com) with your GitHub account.
2. **New +** → **Blueprint** → connect your repo (branch `main`).
3. Render reads `render.yaml` and proposes the `adab-opportunityfinder` service.
4. Click **Apply** — first build takes ~5–10 min.
5. Go to **Settings → Environment** and add:

   | Variable | Value |
   |----------|-------|
   | `DATABASE_URL` | (the Neon connection string from 5a) |
   | `PUBLIC_BASE_URL` | `https://your-domain.com` (set after the domain is live) |
   | `ENABLE_SCHEDULER` | `true` (on exactly one instance) |
   | `ADMIN_EMAIL` / `ADMIN_PASSWORD_HASH` / `SESSION_SECRET_KEY` | required for admin login — generate the hash with `python backend/scripts/hash_admin_password.py` |
   | `ADMIN_TOTP_SECRET` | optional 2FA — generate with `python backend/scripts/generate_admin_totp_secret.py` |
   | `RESEND_API_KEY` / `BREVO_API_KEY` / `SENDGRID_API_KEY` | optional — one of these enables real email sending |
   | `GOOGLE_API_KEY` / `GOOGLE_CSE_ID` | optional — improves scraping |

6. Redeploy after saving env vars.

### 5c. Verify the backend

```bash
curl https://<your-app>.onrender.com/health
# → {"status":"healthy","database":"ok",...}

curl https://<your-app>.onrender.com/api
# → {"service":"Global Opportunities API",...}
```

Render's free tier sleeps after ~15 min idle. The `keep-alive.yml` workflow
pings `/health` every 10 min to keep it warm.

---

## 6. Deploy the frontend to Cloudflare Workers

The frontend is served from Cloudflare's edge network, with API calls proxied
server-side to the Render backend. From the browser's perspective it's one
origin — no CORS changes, no cookie loosening, no admin auth changes.

### 6a. CLI deploy (fastest)

```bash
npx wrangler login
# or: set CLOUDFLARE_API_TOKEN env var scoped to:
#   Account → Workers Scripts → Edit
#   Zone → DNS → Edit (for custom domain)

npx wrangler deploy --dry-run    # validate without publishing
npx wrangler deploy              # publish — prints a *.workers.dev URL
```

### 6b. Verify before touching DNS

```bash
curl https://<worker>.workers.dev/           # site loads
curl https://<worker>.workers.dev/health     # proxies to Render
# Open /admin.html in a browser → log in, confirm the moderation queue loads
```

### 6c. Add the custom domain

Add a `routes` entry to `wrangler.jsonc`:

```jsonc
"routes": [
  { "pattern": "your-domain.com", "custom_domain": true },
  { "pattern": "www.your-domain.com", "custom_domain": true }
]
```

Then `npx wrangler deploy` again. Cloudflare handles the DNS change itself
since the zone is already on Cloudflare.

### 6d. Update Render env var

After the domain is live, set `PUBLIC_BASE_URL=https://your-domain.com` in
Render so the app's outgoing email links point at the real domain.

---

## 7. Auto-deploy is already wired

Once the above is set up, the deploy loop is fully automatic:

| Event | What happens |
|-------|-------------|
| Push to `main` | Render auto-deploys the backend (`autoDeploy: true` in `render.yaml`) |
| Push to `main` | Cloudflare auto-deploys the worker (if connected via dashboard) — or run `npx wrangler deploy` manually |
| Push/PR to `main` | CI runs ruff + pytest + frontend tests; blocks merge on failure |
| Weekly Monday 04:23 UTC | CodeQL scans for new vulnerability patterns |
| Every 10 min | `keep-alive.yml` pings `/health` to prevent Render idle spin-down |
| Weekly | Dependabot opens PRs for outdated pip + GitHub Actions versions |

To force a redeploy without code changes: push an empty commit, or trigger
manually from the Render/Cloudflare dashboard.

---

## 8. Admin login setup

```bash
cd backend
python scripts/hash_admin_password.py          # prints a bcrypt hash
python scripts/generate_admin_totp_secret.py    # optional: prints a TOTP secret
```

Set these in Render → **Settings → Environment**:

| Variable | Value |
|----------|-------|
| `ADMIN_EMAIL` | your admin email |
| `ADMIN_PASSWORD_HASH` | (hash from the script above) |
| `SESSION_SECRET_KEY` | (any long random string) |
| `ADMIN_TOTP_SECRET` | (optional, from the TOTP script) |

Without these set, every admin endpoint refuses requests. With them set,
`/admin.html` shows a login form.

---

## 9. Email setup (optional)

Alert and save-confirmation emails are logged to the console by default. To
send them for real, set exactly one of:

| Variable | Provider |
|----------|----------|
| `RESEND_API_KEY` | Resend (highest priority if multiple are set) |
| `BREVO_API_KEY` | Brevo |
| `SENDGRID_API_KEY` | SendGrid |

Priority order: Resend → Brevo → SendGrid. Only one is used even if multiple
are set.

---

## 10. Quick reference — all commands

```bash
# Local dev
cd backend && uvicorn app.main:app --reload --port 8000
cd frontend && npm test

# Lint + test
cd backend && python -m ruff check . && python -m pytest tests -v

# Deploy backend (auto on push to main; manual trigger in Render dashboard)
# Deploy frontend
npx wrangler deploy

# Secrets
gh secret set GOOGLE_API_KEY --repo <org>/<repo> --body "..."
gh secret set GOOGLE_CSE_ID  --repo <org>/<repo> --body "..."

# Admin credential generation
cd backend && python scripts/hash_admin_password.py
cd backend && python scripts/generate_admin_totp_secret.py

# Verify production
curl https://<app>.onrender.com/health
curl https://<worker>.workers.dev/health
curl https://<domain>/health
```

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| 405 on `/api/v1/admin/opportunities/*` | Admin listings router not mounted | Already fixed in `main.py`; if it recurs, check the `include_router` call |
| `Table 'opportunities' is already defined` on import | Duplicate class defs in `models.py` or `schemas.py` | Already fixed; if it recurs, check for appended duplicate blocks |
| Render cold start (30-60s) | Free tier sleeps after 15 min idle | `keep-alive.yml` pings every 10 min; upgrade Render plan for always-on |
| `could not translate host name "dpg-..."` | Render deleted the free Postgres | Use Neon instead (already documented in DEPLOY-RENDER.md) |
| Admin login always fails | `ADMIN_PASSWORD_HASH` not set or not bcrypt | Regenerate with `hash_admin_password.py`, set in Render env |
| Emails not sending | No email provider key set | Set `RESEND_API_KEY` (or Brevo/SendGrid); otherwise emails log to console |
| ruff CI fails | New lint errors introduced | Run `python -m ruff check --fix .` locally before pushing |
| Frontend test `window.scrollTo` error | jsdom doesn't implement `scrollTo` | Not a failure — tests still pass; ignore the stderr warning |
