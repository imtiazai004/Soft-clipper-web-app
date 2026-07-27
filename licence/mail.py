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
FROM = os.environ.get("MAIL_FROM", "info@aisofttechsolution.com")
DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "")
SUPPORT_WHATSAPP = os.environ.get("SUPPORT_WHATSAPP", "+44 7462 086661")


def send_licence(to: str, key: str) -> bool:
	body = f"""Thanks for buying Soft Clipper.

Your licence key:

    {key}

Download:
    {DOWNLOAD_URL or "(download link not configured — reply to this email and we will send it)"}

Getting started
  1. Extract the ZIP somewhere you will keep it (Documents works well).
  2. Run "Soft Clipper.exe".
  3. Paste the licence key above when it asks.
  4. In Settings, add a free Google Gemini API key for the AI features.

Your licence covers one computer. Changing PC later is fine — release the
licence in Settings on the old machine, then activate on the new one.

Stuck? Reply to this email, or WhatsApp {SUPPORT_WHATSAPP}.

— Soft Clipper
"""
	msg = EmailMessage()
	msg["Subject"] = "Your Soft Clipper licence key"
	msg["From"] = FROM
	msg["To"] = to
	msg.set_content(body)

	if not SMTP_HOST:
		# Deliberately loud: the order succeeded and the customer is waiting.
		log.error("SMTP not configured — licence %s for %s was NOT emailed", key, to)
		return False

	try:
		with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
			s.starttls()
			if SMTP_USER:
				s.login(SMTP_USER, SMTP_PASS)
			s.send_message(msg)
		log.info("licence emailed to %s", to)
		return True
	except Exception as exc:  # noqa: BLE001 - never let mail failure lose the sale
		log.error("could not email licence %s to %s: %s", key, to, exc)
		return False
