# Licence server

Issues and activates licences for the **desktop** Soft Clipper app. It runs
next to the webapp on the same server but in its own container, with its own
SQLite database on its own volume.

## What it guarantees

- One key activates on **one machine**. A second machine is refused with a
  message the customer can act on.
- Moving PC is self-service: release on the old machine, activate on the new
  one. Support can release a machine that no longer exists.
- A refund or chargeback revokes the licence automatically.
- The app keeps working **offline** for `LICENCE_TOKEN_DAYS` (default 14) on a
  signed token, so an outage here never locks out a paying customer.
- Stripe retries cannot issue two licences for one payment.

## Setting it up

**1. Generate the signing keypair — once, ever.**

```bash
python -m licence.crypto keygen
```

Put `LICENCE_PRIVATE_KEY` in `deploy/.env`. Paste the printed public key into
the desktop app's `core/licence.py`. Losing the private key means every existing
activation token stops verifying, so keep a copy somewhere safe and offline.

**2. Fill in `deploy/.env`:**

```
LICENCE_PRIVATE_KEY=...          # from keygen
LICENCE_ADMIN_TOKEN=...          # openssl rand -hex 24
STRIPE_WEBHOOK_SECRET=whsec_...  # from the Stripe dashboard
SMTP_HOST=...                    # where the key email is sent from
SMTP_PORT=587
SMTP_USER=...
SMTP_PASS=...
MAIL_FROM=info@aisofttechsolution.com
DOWNLOAD_URL=https://.../Soft-Clipper.zip
```

**3. Point Stripe at the webhook:** `https://your-domain/webhooks/stripe`,
listening for `checkout.session.completed`, `charge.refunded` and
`charge.dispute.created`.

**4. Deploy:**

```bash
cd /opt/soft-clipper && git pull
cd deploy && docker compose up -d --build licence caddy
```

## Endpoints

| Method | Path | Who calls it |
|---|---|---|
| POST | `/api/licence/activate` | the app, first run |
| POST | `/api/licence/validate` | the app, every couple of weeks |
| POST | `/api/licence/release` | the app, when moving PC |
| POST | `/webhooks/stripe` | Stripe |
| POST | `/api/admin/licences` | you — issue a key by hand |
| GET | `/api/admin/licences` | you — recent licences |
| GET | `/api/admin/licences/{key}` | you — one licence and its history |
| POST | `/api/admin/licences/{key}/release` | you — free a dead machine |
| POST | `/api/admin/licences/{key}/revoke` | you — kill a shared key |

Admin calls need the `X-Admin-Token` header.

## Issuing a key by hand

```bash
curl -X POST https://your-domain/api/admin/licences \
  -H "X-Admin-Token: $LICENCE_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"buyer@example.com","note":"replacement after refund dispute"}'
```

## Tests

```bash
python -m pytest licence/test_licence.py -q
```

They run against the real HTTP stack and cover the rules that cost money when
they break: two-machine refusal, release and re-activation, revocation, Stripe
signature checks and retry idempotency, and token forgery.

## Things to know

- **The database is the business.** `deploy/data-licence/licences.db` holds every
  licence you have ever sold. Back it up.
- Fingerprints are hashed again server-side, so a database leak cannot be
  replayed as a machine identity.
- A refund is matched to a licence by email. If a customer pays with one address
  and asks for a refund from another, revoke it by hand.
