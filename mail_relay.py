"""Credentialed TLS relay shared by workspace account mail."""

from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
import os
import smtplib
import ssl


def configured(origin):
    host = os.environ.get("CUEPORCH_SMTP_HOST", "").strip()
    sender = os.environ.get("CUEPORCH_SMTP_FROM", "").strip()
    username = os.environ.get("CUEPORCH_SMTP_USER", "")
    secret = os.environ.get("CUEPORCH_SMTP_PASSWORD", "")
    if not origin.startswith("https://") or not host or not sender or bool(username) != bool(secret):
        return None
    try:
        port = int(os.environ.get("CUEPORCH_SMTP_PORT", "465"))
        sender = Address(addr_spec=sender).addr_spec
    except (ValueError, HeaderParseError):
        return None
    return (host, port, sender, username, secret) if 0 < port < 65536 and "@" in sender else None


def deliver(config, address, subject, text):
    host, port, sender, username, secret = config
    message = EmailMessage()
    message["Subject"] = subject
    accepted = False
    try:
        message["From"] = sender
        message["To"] = address
        message.set_content(text)
        with smtplib.SMTP_SSL(host, port, timeout=6, context=ssl.create_default_context()) as relay:
            if username:
                relay.login(username, secret)
            relay.send_message(message, from_addr=sender, to_addrs=[address])
            accepted = True
    except (OSError, ValueError):
        pass
    return accepted
