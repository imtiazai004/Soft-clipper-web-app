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
