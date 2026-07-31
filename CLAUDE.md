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

## Status

Product has not launched — the Stripe Payment Link in the dashboard's price
settings is still a **TEST** link. Going live needs that swapped for a real one.

## Testing

`./.venv/Scripts/python.exe -m pytest`. `tests/conftest.py` has the same
config-clobber safety net as the desktop repo — read it before writing a test
that touches config or licence state.
