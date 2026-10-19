"""E-postjobb for BNC Selger – sender det som ligger klart i email_queue.

Kjøres jevnlig av en GitHub Action. Henter rader med status=pending og
send_after <= i dag (svensk/norsk tid), bygger e-posten (takk-malen fylles ut
her), sender via SMTP og markerer sendt/feilet.

Env (GitHub Secrets):
  SUPABASE_URL, SUPABASE_SECRET_KEY
  SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_USE_TLS
  MAIL_FROM_EMAIL, MAIL_FROM_NAME

  Svensk utsending (lang='sv' i køen) – egne, valgfrie innstillinger. Settes de
  ikke, brukes de norske. Typisk en egen Brevo-konto + avsender info@bncnordic.se:
  SV_SMTP_HOST, SV_SMTP_PORT, SV_SMTP_USERNAME, SV_SMTP_PASSWORD, SV_SMTP_USE_TLS
  SV_MAIL_FROM_EMAIL (standard info@bncnordic.se), SV_MAIL_FROM_NAME
"""
import os
import smtplib
import json
from datetime import datetime, timezone
from email.message import EmailMessage

import requests

try:
    from zoneinfo import ZoneInfo
    TODAY = datetime.now(ZoneInfo("Europe/Oslo")).date().isoformat()
except Exception:
    TODAY = datetime.utcnow().date().isoformat()

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]
SMTP_HOST = os.environ["SMTP_HOST"]
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USERNAME", "")
SMTP_PASS = os.getenv("SMTP_PASSWORD", "")
SMTP_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in ("1", "true", "yes", "on")
FROM_EMAIL = os.getenv("MAIL_FROM_EMAIL", SMTP_USER)
FROM_NAME = os.getenv("MAIL_FROM_NAME", "BNC Nordic")

_TRUE = ("1", "true", "yes", "on")


def _profile(lang):
    """SMTP + avsender for gitt språk. Svensk bruker egne SV_*-verdier om satt,
    ellers faller alt tilbake til den norske konfigurasjonen."""
    if str(lang).lower() == "sv":
        return {
            "host": os.getenv("SV_SMTP_HOST", SMTP_HOST),
            "port": int(os.getenv("SV_SMTP_PORT", str(SMTP_PORT))),
            "user": os.getenv("SV_SMTP_USERNAME", SMTP_USER),
            "pw": os.getenv("SV_SMTP_PASSWORD", SMTP_PASS),
            "tls": os.getenv("SV_SMTP_USE_TLS", "true").lower() in _TRUE,
            "from_email": os.getenv("SV_MAIL_FROM_EMAIL", "info@bncnordic.se"),
            "from_name": os.getenv("SV_MAIL_FROM_NAME", FROM_NAME),
        }
    return {"host": SMTP_HOST, "port": SMTP_PORT, "user": SMTP_USER, "pw": SMTP_PASS,
            "tls": SMTP_TLS, "from_email": FROM_EMAIL, "from_name": FROM_NAME}


def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def due_emails():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/email_queue",
                     headers=sb_headers(),
                     params={"select": "*", "status": "eq.pending",
                             "send_after": f"lte.{TODAY}", "order": "send_after.asc"},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def mark(row_id, status, error=""):
    requests.patch(f"{SUPABASE_URL}/rest/v1/email_queue",
                   headers={**sb_headers(), "Prefer": "return=minimal"},
                   params={"id": f"eq.{row_id}"},
                   json={"status": status, "error": error,
                         "sent_at": datetime.now(timezone.utc).isoformat()},
                   timeout=30).raise_for_status()


def thankyou_body(lang="no"):
    """Nødtekst hvis køraden mangler body (appen fyller normalt inn body selv)."""
    if str(lang).lower() == "sv":
        return (
            "Hej,\n\n"
            "Tack för att du tog dig tid till en trevlig pratstund när jag var förbi hos er! 😊\n\n"
            "Vi på BNC Nordic levererar produkter inom bilvård, fälgar, underrede och "
            "bromsar, och hjälper gärna till att hitta produkter som passar era behov.\n\n"
            "Har du frågor efter besöket, vill ha mer information eller en offert, "
            "är det bara att svara på detta mejl.\n\n"
            "Trevligt att lära känna er – vi håller kontakten!\n\n"
            "BNC Nordic Distribution AS\n"
            "info@bncnordic.se"
        )
    return (
        "Hei,\n\n"
        "Takk for at du tok deg tid til en hyggelig prat da jeg var innom dere! 😊\n\n"
        "Vi i BNC Nordic leverer produkter innen bilpleie, felger, understell og "
        "bremser, og hjelper gjerne med å finne produkter som passer deres behov.\n\n"
        "Har du spørsmål etter besøket, ønsker mer informasjon eller vil ha et tilbud, "
        "er det bare å svare på denne e-posten eller ringe meg på +47 63 81 99 00.\n\n"
        "Hyggelig å bli kjent – vi holder kontakten!\n\n"
        "Kim Werner Strand\n"
        "Produktansvarlig / Product manager\n\n"
        "BNC Nordic Distribution AS\n"
        "+47 63 81 99 00\n"
        "kim@bncnordic.no\n"
        "Strømsveien 323B, 1081 Oslo"
    )


def send(to_email, subject, body, prof):
    msg = EmailMessage()
    msg["From"] = f"{prof['from_name']} <{prof['from_email']}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    with smtplib.SMTP(prof["host"], prof["port"], timeout=30) as s:
        if prof["tls"]:
            s.starttls()
        if prof["user"]:
            s.login(prof["user"], prof["pw"])
        s.send_message(msg)


def main():
    rows = due_emails()
    print(f"Dato {TODAY}: {len(rows)} e-post(er) klare.")
    sent = failed = 0
    for r in rows:
        lang = r.get("lang") or "no"
        prof = _profile(lang)
        body = r.get("body") or ""
        if r.get("kind") == "thankyou" and not body.strip():
            body = thankyou_body(lang)
        try:
            send(r["to_email"], r.get("subject") or "(uten emne)", body, prof)
            mark(r["id"], "sent")
            sent += 1
            print(f"  sendt [{r.get('kind')}/{lang}] fra {prof['from_email']} -> {r['to_email']}")
        except Exception as exc:
            mark(r["id"], "failed", str(exc)[:400])
            failed += 1
            print(f"  FEIL [{r.get('kind')}] -> {r['to_email']}: {exc}")
    print(f"Ferdig: {sent} sendt, {failed} feilet.")


if __name__ == "__main__":
    main()
