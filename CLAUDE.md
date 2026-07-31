# Soft Clipper Web

The team webapp version of **Soft Clipper** — same `core/` as the desktop repo
(`d:\VIBE CODING\Video cliper (Ali raza)`, see its CLAUDE.md for the full
feature history and architecture notes; this file only covers what's different
here).

Runs on a Hetzner VPS at **`app.softclipper.pro`** — not Render, no auto-deploy.
Deploying means:

```
ssh root@5.75.178.2 "cd /opt/soft-clipper && git pull && cd deploy && docker compose up -d --build"
```

`licence/` is a second service in the same `docker-compose.yml` — the licence
API, the admin dashboard (`licence/admin.html`, at `/admin`), Stripe webhooks,
and `/api/site-config` (what the marketing site reads at build time).

## What's different from the desktop repo

- **Per-user, not one app-data folder.** `backend/main.py user_root(user)` is
  the split point — `core/` functions that touch disk (projects, brand kit,
  config) take a `user_root`/`user` argument here that the desktop equivalents
  don't need. When porting a desktop feature, this is the thing to check first.
- **`auth.MULTI_USER`** gates single-vs-multi-user behaviour. With it off this
  is deliberately a single-user desktop-shaped app and everyone shares one
  config — that's correct there, not a bug. Tests that assert per-user
  isolation must set `MULTI_USER = True` explicitly (see `test_brand_kit.py`'s
  isolation test for why this tripped a test once).
- **Fonts matter more here.** The container ships `fonts-liberation` and
  `fonts-noto-cjk` and nothing else — no Windows Nirmala UI fallback. Every
  bundled font in `assets/fonts/` is load-bearing on this server in a way it
  mostly wasn't on Windows.
- **Licence dashboard** (`licence/admin.html`) is where the owner sets price,
  discount, download links, the affiliate programme, the notice bar, and (as of
  the update-check rework) the app version the desktop apps get told about —
  see the desktop repo's CLAUDE.md § Release process for the full flow.

## The affiliate programme

Runs entirely out of `licence/` plus the marketing site — nothing in `core/`,
so nothing to port to the desktop repo.

- **Anyone can sign themselves up**, from any country, at
  `softclipper.pro/affiliates/`. The form posts cross-origin to
  `POST /api/partner/join`; `SITE_ORIGIN` is the CORS allow-list and is the
  single thing whose absence breaks the form while every test still passes.
- **Never put "affiliate", "click", "track" or "referral" in a path a browser
  requests.** Ad and tracker blockers match request URLs against keyword lists
  and cancel the request; JavaScript is told only `Failed to fetch` — no status,
  no body, nothing pointing at a cause, and the server looks perfectly healthy
  from every angle. That is what `/api/affiliates/apply` did on a real machine.
  Everything the browser calls lives under `/api/partner/*` and `/partner*` now;
  the old paths are kept as aliases so links already emailed keep working.
- **Nothing earns until an email is confirmed.** Statuses are
  `pending → review → active`, plus `disabled`/`rejected`. `_credit_affiliate`
  only ever pays `active`, so every unfinished state is safe by default.
  `autoApprove` (dashboard) decides whether `review` is skipped.
- **No passwords anywhere.** Sign-in is a signed, scoped, expiring token
  (`crypto.make_scoped`/`read_scoped`) emailed as a link, exchanged for an
  HttpOnly session cookie. The **scope** is what stops a sign-in link being
  replayed as a confirmation link or a month-long session — never drop it.
- **The affiliate's own dashboard** is `licence/affiliate.html`, served at
  `/affiliate`, same pattern as `/admin`. It never shows a buyer's email.
- **Clicks** are counted per code per day (`clicks` table), pinged by
  `ReferralTag.astro` with `sendBeacon` *only* when the URL carried a fresh
  tag. In `affiliate_summary()` clicks must stay a scalar subquery — a second
  LEFT JOIN multiplies the referral rows and silently inflates every money
  column.
- **Payouts are unchanged**: the affiliate picks Stripe Connect (self-serve
  onboarding, ~50 countries) or manual (Wise/PayPal, everywhere else, which is
  the path Pakistan/Bangladesh/Nigeria need). Money still only moves when the
  owner presses a button on the admin page.
- **Deploy needs the Caddyfile**: `/api/partner/*` and `/partner*` (plus the
  kept `/api/affiliate*` aliases) must be in the `@licence` matcher, or every
  emailed sign-in link lands on the video app's 404. Caddy does **not** pick up
  a Caddyfile change on `docker compose up -d --build` — its container is left
  running; it needs `--force-recreate caddy`.

## Status

Product has not launched — the Stripe Payment Link in the dashboard's price
settings is still a **TEST** link. Going live needs that swapped for a real one.

## Testing

`./.venv/Scripts/python.exe -m pytest`. `tests/conftest.py` has the same
config-clobber safety net as the desktop repo — read it before writing a test
that touches config or licence state.
