"""Ukesoppsummering for BNC-selgeren.

Kjøres ukentlig (f.eks. fredag ettermiddag). For hver selger med besøk denne
ISO-uka lages en kort oppsummering som legges i e-postkøen (kind='summary',
send_after=i dag), slik at e-postjobben sender den.

Env: SUPABASE_URL, SUPABASE_SECRET_KEY
"""
import os
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

try:
    from zoneinfo import ZoneInfo
    TODAY = datetime.now(ZoneInfo("Europe/Oslo")).date()
except Exception:
    TODAY = datetime.utcnow().date()

MONDAY = TODAY - timedelta(days=TODAY.weekday())  # mandag denne uka

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SECRET_KEY"]


def sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def week_visits():
    r = requests.get(f"{SUPABASE_URL}/rest/v1/visits",
                     headers=sb_headers(),
                     params={"select": "user_email,navn,kommune,outcome,visited_on",
                             "visited_on": f"gte.{MONDAY.isoformat()}",
                             "order": "visited_on.asc"},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def queue(to_email, subject, body):
    requests.post(f"{SUPABASE_URL}/rest/v1/email_queue",
                  headers={**sb_headers(), "Prefer": "return=minimal"},
                  json=[{"kind": "summary", "to_email": to_email, "subject": subject,
                         "body": body, "send_after": TODAY.isoformat()}],
                  timeout=30).raise_for_status()


def summary_recipients():
    """{user_email: (summary_email, name)} fra profilene."""
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/salesperson_settings",
                         headers=sb_headers(), params={"select": "email,summary_email,name"}, timeout=30)
        r.raise_for_status()
        return {row["email"]: (row.get("summary_email") or row["email"], row.get("name") or "")
                for row in r.json()}
    except Exception:
        return {}


# Standardtekst – admin kan overstyre den i tabellen email_content (kind=summary).
DEFAULT_SUMMARY_SUBJECT = "Ukesoppsummering: {antall} besøk ({periode})"
DEFAULT_SUMMARY_BODY = (
    "Hei,\n\nHer er oppsummeringen av uka ({periode}):\n\n"
    "Totalt {antall} besøk.\nUtfall: {utfall}\n\n"
    "Besøk:\n{besok_liste}\n\nStå på!\nBNC Nordic"
)


def summary_template():
    try:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/email_content",
                         headers=sb_headers(),
                         params={"select": "subject,body", "kind": "eq.summary"}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if rows:
            return (rows[0].get("subject") or DEFAULT_SUMMARY_SUBJECT,
                    rows[0].get("body") or DEFAULT_SUMMARY_BODY)
    except Exception:
        pass
    return DEFAULT_SUMMARY_SUBJECT, DEFAULT_SUMMARY_BODY


def apply_vars(text, mapping):
    out = str(text or "")
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def main():
    rows = week_visits()
    recips = summary_recipients()
    subj_tpl, body_tpl = summary_template()
    periode = f"{MONDAY.strftime('%d.%m')} – {TODAY.strftime('%d.%m')}"
    by_user = {}
    for v in rows:
        by_user.setdefault(v["user_email"], []).append(v)
    print(f"Uke fra {MONDAY}: {len(rows)} besøk, {len(by_user)} selger(e).")
    for user, vs in by_user.items():
        to_email, name = recips.get(user, (user, ""))
        outc = Counter(v.get("outcome", "") for v in vs)
        besok_liste = "\n".join(
            f"- {v['visited_on']}  {v.get('navn','')} ({v.get('kommune','')}) – {v.get('outcome','')}"
            for v in vs)
        m = {"navn": name or user, "periode": periode, "antall": len(vs),
             "utfall": ", ".join(f"{k}: {n}" for k, n in outc.most_common()),
             "besok_liste": besok_liste}
        try:
            queue(to_email, apply_vars(subj_tpl, m), apply_vars(body_tpl, m))
            print(f"  kø: {user} -> {to_email} ({len(vs)} besøk)")
        except Exception as exc:
            print(f"  FEIL {user}: {exc}")


if __name__ == "__main__":
    main()
