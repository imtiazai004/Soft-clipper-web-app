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
- **Payouts: four methods, one dispatcher.** The affiliate picks Stripe Connect
  (~50 countries), PayPal (an email address; no Pakistan), Wise (their *bank
  account*, ~160 countries — this is the rail that reaches PK/BD/NG), or manual.
  Manual must always stay: no set of providers covers everybody.
  - Every rail lives in its own module shaped like `stripe_api.py`, and
    `payouts.send()` is the only thing that knows which to use. The admin Pay
    button, "what is owed", and marking rows paid are one implementation — a
    second endpoint per rail is how two rails start disagreeing about what has
    been paid.
  - **Money still only moves when the owner presses a button.** The affiliate can
    ask (`payout_requested_at`), which emails the owner and flags the row; that
    flag clears itself inside `mark_referrals_paid` when nothing payable is left.
  - `payouts.reference()` is derived from the code, the row ids and the amount —
    never a timestamp. Each rail turns it into its own idempotency key (Stripe
    header, PayPal `sender_batch_id` hashed to 30 chars, Wise transaction UUID).
    A retry after a timeout must be the same payment, not a second one.
  - Rows are marked paid **only after** the rail returns. A row wrongly left open
    is a retry; a row wrongly closed is an affiliate who is never paid.
  - Wise's account fields are fetched from Wise (`account-requirements`) and
    rendered as-is — never hard-code a per-currency field list, it is wrong for
    every country nobody here has tested. Funding a Wise transfer needs an
    RSA-signed challenge (`WISE_PRIVATE_KEY`); without it transfers are created
    and left unfunded, and that is reported rather than recorded as sent.
- **Two hostnames, and the difference matters.** `app.softclipper.pro` is this
  server's bare IP and carries the video app, including gigabyte uploads.
  `api.softclipper.pro` is the *same server behind Cloudflare* and carries the
  licence and affiliate API only. A bare IP is blocked wholesale in a number of
  countries — the symptom is the marketing site loading perfectly (it is on
  Cloudflare) and then every request the page makes dying with a browser error
  that names no cause. Cloudflare's free plan caps a request body at 100 MB,
  which is why the video app cannot simply move there too.
  Needs `API_DOMAIN=api.softclipper.pro` in `deploy/.env`, a **proxied** (orange
  cloud) A record, and the zone's SSL mode on **Full (strict)** — Flexible makes
  Cloudflare talk HTTP to Caddy, which redirects to HTTPS, which loops forever.
- **HTTP/3 is off in the Caddyfile, deliberately.** Caddy advertises h3 via
  `Alt-Svc` and browsers then use QUIC over UDP 443 for everything after the
  first request. On a network that drops UDP 443 — carriers, corporate
  firewalls, several countries' ISPs — the page loads over TCP and then every
  `fetch` it makes dies, reported to JS as a bare `Failed to fetch` while the
  server answers `curl` from anywhere else. Do not turn it back on.
- **Reload Caddy, do not recreate it.** `docker compose exec caddy caddy reload
  --config /etc/caddy/Caddyfile` validates first and keeps the old config if
  the new one is bad. `--force-recreate` with a broken Caddyfile takes the
  whole host down, app and licence together.
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
