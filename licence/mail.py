"""Sending the licence key to the customer.

Plain SMTP, because the mailbox already exists and one message per sale does
not justify a transactional email provider. If SMTP is not configured the key
is logged instead of lost — a missing env var must never swallow a paid order.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("licence.mail")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
# The domain the customer just bought from. Sending a licence key from anywhere
# else reads as phishing to the buyer and scores badly with spam filters, which
# both end the same way: the key never gets used.
FROM = os.environ.get("MAIL_FROM", "info@softclipper.pro")
DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "")
SUPPORT_WHATSAPP = os.environ.get("SUPPORT_WHATSAPP", "+44 7462 086661")
MAC_GUIDE_URL = os.environ.get("MAC_GUIDE_URL", "https://softclipper.pro/help/install-mac/")
# Where a new affiliate application is announced. The same inbox by default —
# there is one person reading it — but separable without a code change.
OWNER_EMAIL = os.environ.get("AFFILIATE_NOTIFY", FROM)


def _send(to: str, subject: str, body: str, what: str = "email") -> bool:
	"""One SMTP path for every message this service sends.

	Failure is logged and swallowed, never raised. The caller is always in the
	middle of something that has already happened — a sale, a sign-up, an
	approval — and unwinding that because a mail server was briefly unreachable
	would turn a missing email into a lost record of the thing itself.
	"""
	msg = EmailMessage()
	msg["Subject"] = subject
	msg["From"] = FROM
	msg["To"] = to
	msg.set_content(body)

	if not SMTP_HOST:
		# Deliberately loud, and it prints the body: somebody is waiting for this,
		# and the log is the only remaining copy of what they were supposed to get.
		log.error("SMTP not configured — %s to %s was NOT sent:\n%s", what, to, body)
		return False

	try:
		with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
			s.starttls()
			if SMTP_USER:
				s.login(SMTP_USER, SMTP_PASS)
			s.send_message(msg)
		log.info("%s sent to %s", what, to)
		return True
	except Exception as exc:  # noqa: BLE001 - a mail failure must not undo the event
		log.error("could not send %s to %s: %s", what, to, exc)
		return False


def send_licence(to: str, key: str) -> bool:
	# Both sets of steps go in every email. We do not know which computer the
	# buyer is on, and a Mac customer who follows Windows instructions hits an
	# unexplained security warning and asks for a refund instead of asking us.
	body = f"""Thanks for buying Soft Clipper.

Your licence key:

    {key}

Download:
    {DOWNLOAD_URL or "(download link not configured — reply to this email and we will send it)"}

Getting started — Windows
  1. Run the installer you downloaded. If Windows shows a blue SmartScreen
     panel, choose "More info" and then "Run anyway" — that appears for any
     new release and goes away as more people download it.
  2. It installs for you only, so Windows never asks for an administrator
     password, and Soft Clipper opens by itself when it finishes.
  3. Paste the licence key above when it asks.

Getting started — Mac
  1. Open the .dmg you downloaded and drag Soft Clipper onto the Applications
     shortcut in the same window. Then eject the disk image.
  2. Open it from Applications. macOS will say it "cannot verify" the app —
     this is normal for software not sold through the App Store, and it
     appears once.
  3. Open System Settings > Privacy & Security, scroll to Security, and click
     "Open Anyway" next to the message about Soft Clipper.
  4. Open the app again and paste your licence key.
     Full guide: {MAC_GUIDE_URL}

Then, on either platform: open Settings and add a free Google Gemini API key
for the AI features — or switch transcription and analysis to run on your own
computer if you would rather not use one.

Your licence covers one computer. Changing machine later is fine — release the
licence in Settings on the old one, then activate on the new one.

Stuck? Reply to this email, or WhatsApp {SUPPORT_WHATSAPP}.

— Soft Clipper
"""
	return _send(to, "Your Soft Clipper licence key", body, what=f"licence {key}")


# ── Bank transfer / wallet payments ─────────────────────────────────────────


def send_bank_payment_submitted(to: str, reference: str, amount_pkr: int) -> bool:
	return _send(
		to,
		f"Payment submitted — {reference}",
		f"""Thanks. We received the payment details for your Soft Clipper order.

Order reference: {reference}
Amount to verify: PKR {amount_pkr:,}

This is not a payment confirmation yet. We will compare the transaction with
the receiving account. Once it is verified, your licence key and download link
will be emailed to this address.

If anything was entered incorrectly, reply to this email or WhatsApp
{SUPPORT_WHATSAPP} and quote {reference}.

— Soft Clipper
""",
		what=f"bank-payment receipt {reference}",
	)


def notify_bank_payment(reference: str, email: str, amount_pkr: int, method: str, transaction_id: str) -> bool:
	return _send(
		OWNER_EMAIL,
		f"Bank payment waiting — {reference}",
		f"""A bank transfer/wallet payment is waiting for verification.

Order: {reference}
Customer: {email}
Expected: PKR {amount_pkr:,}
Method: {method}
Transaction ID: {transaction_id}

Open the licence admin dashboard, compare it with the actual incoming bank or
JazzCash transaction, then approve or reject it there. A screenshot is supporting
evidence only; do not approve without seeing the credit in the receiving account.
""",
		what=f"bank-payment notification {reference}",
	)


def send_bank_payment_rejected(to: str, reference: str, reason: str = "") -> bool:
	detail = reason or "The submitted transaction could not be matched to the receiving account."
	return _send(
		to,
		f"We could not verify payment — {reference}",
		f"""We could not verify the payment submitted for Soft Clipper order {reference}.

{detail}

No licence has been issued. Reply to this email or WhatsApp {SUPPORT_WHATSAPP}
with the order reference and your transaction details so we can check it with you.

— Soft Clipper
""",
		what=f"bank-payment rejection {reference}",
	)


# ── affiliates ───────────────────────────────────────────────────────────────
#
# Four messages, and between them they are the whole of an affiliate's contact
# with us until they are owed money. They are plain text and short on purpose:
# these go to people who have just filled in a form on a site they do not know
# yet, and a long HTML email from a stranger asking them to click a link is the
# shape of the thing they have been told to delete.


def send_affiliate_verify(to: str, name: str, url: str) -> bool:
	"""Confirm the address. Nothing is live until this is clicked.

	This is the whole anti-abuse gate on an open sign-up form: anyone can type a
	name and a code, but only somebody holding that mailbox can turn it into a
	link that earns.
	"""
	return _send(
		to,
		"Confirm your Soft Clipper affiliate sign-up",
		f"""Hi {name},

Thanks for signing up to promote Soft Clipper. One click and you are in:

    {url}

That link is good for 24 hours. Ask for another from the affiliate page if it
expires — nothing is lost either way.

If you did not sign up, ignore this. Nothing happens until the link is used, and
we will not email you again.

— Soft Clipper
""",
		what="affiliate verification",
	)


def send_affiliate_welcome(to: str, name: str, code: str, link: str, portal: str, rate_pct: int) -> bool:
	"""They are live. This is the email that has to contain their link, because it
	is the one they will search their inbox for six weeks from now."""
	return _send(
		to,
		"You are in — here is your Soft Clipper affiliate link",
		f"""Hi {name},

You are approved. Your link:

    {link}

Anyone who follows it and buys within 60 days earns you {rate_pct}% of what they
pay. It costs them nothing extra, and there is no cap.

Your dashboard — clicks, sales, what you are owed, and where to send it:

    {portal}

Sign in there with this email address. There is no password: you ask for a link
and it arrives here.

Two things worth knowing before you start:

  · Commission is held for a while after each sale so a refund can still cancel
    it. What the dashboard says you have earned is money you get to keep.
  · Do not bid on our brand name in ads, and do not buy through your own link.
    Those are the two things that end an account.

The full terms are on the affiliate page. Reply to this email with anything at
all — it reaches a person.

— Soft Clipper
""",
		what="affiliate welcome",
	)


def send_affiliate_login(to: str, url: str) -> bool:
	return _send(
		to,
		"Your Soft Clipper affiliate sign-in link",
		f"""Here is your sign-in link:

    {url}

It works once and expires in 30 minutes. If you did not ask for it, nothing has
happened to your account — somebody typed your address into the sign-in box, and
without this email they got nowhere.

— Soft Clipper
""",
		what="affiliate sign-in link",
	)


def send_affiliate_decision(to: str, name: str, approved: bool, reason: str, link: str, portal: str) -> bool:
	"""The answer when the owner reviews an application by hand.

	A rejection is sent rather than swallowed. Someone who signed up and heard
	nothing assumes the form is broken and fills it in again, from a second
	address — which is how one rejected applicant becomes three accounts.
	"""
	if approved:
		body = f"""Hi {name},

Your affiliate application is approved. Your link:

    {link}

Your dashboard: {portal}

— Soft Clipper
"""
	else:
		body = f"""Hi {name},

We are not able to take your affiliate application on this occasion.

{reason or "No specific reason was given."}

Nothing else changes, and you are welcome to reply if you think this is a
mistake — it often is.

— Soft Clipper
"""
	return _send(
		to,
		"Your Soft Clipper affiliate application",
		body,
		what="affiliate decision",
	)


def notify_owner_new_affiliate(affiliate: dict, needs_review: bool, admin_url: str) -> bool:
	"""Tell the owner somebody signed up.

	Sent on confirmation rather than on the form submission, so the inbox only
	sees people who own the address they typed. Everything the owner needs to
	judge the application is in the message itself — the point is to be able to
	decide from a phone without opening the dashboard.
	"""
	state = "waiting for your approval" if needs_review else "already live and earning"
	return _send(
		OWNER_EMAIL,
		f"New Soft Clipper affiliate: {affiliate.get('code', '')} ({state})",
		f"""{affiliate.get("name", "")} <{affiliate.get("email", "")}> signed up as an affiliate.

  Code       {affiliate.get("code", "")}
  Country    {affiliate.get("country") or "not given"}
  Rate       {affiliate.get("rate_pct", 0)}%
  Promoting  {affiliate.get("promo") or "not given"}

They are {state}.

Dashboard: {admin_url}

— Soft Clipper
""",
		what="affiliate signup notice",
	)
