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
                     params={"select": "user_email,navn,kommune,outcome,visited_on,leg_km,co2_g",
                             "visited_on": f"gte.{MONDAY.isoformat()}",
                             "order": "visited_on.asc"},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def queue(to_email, subject, body, lang="no"):
    row = {"kind": "summary", "to_email": to_email, "subject": subject,
           "body": body, "send_after": TODAY.isoformat(), "lang": lang}
    r = requests.post(f"{SUPABASE_URL}/rest/v1/email_queue",
                      headers={**sb_headers(), "Prefer": "return=minimal"},
                      json=[row], timeout=30)
    if r.status_code >= 400 and "lang" in row:
        # «lang»-kolonnen finnes kanskje ikke ennå – prøv uten
        row.pop("lang", None)
        r = requests.post(f"{SUPABASE_URL}/rest/v1/email_queue",
                          headers={**sb_headers(), "Prefer": "return=minimal"},
                          json=[row], timeout=30)
    r.raise_for_status()


def summary_recipients():
    """{user_email: (summary_email, name, lang)} fra profilene."""
    def _fetch(select):
        r = requests.get(f"{SUPABASE_URL}/rest/v1/salesperson_settings",
                         headers=sb_headers(), params={"select": select}, timeout=30)
        r.raise_for_status()
        return r.json()
    try:
        rows = _fetch("email,summary_email,name,lang")
    except Exception:
        try:
            rows = _fetch("email,summary_email,name")   # før «lang»-kolonnen finnes
        except Exception:
            return {}
    return {row["email"]: (row.get("summary_email") or row["email"], row.get("name") or "",
                           (row.get("lang") or "no")) for row in rows}


# Standardtekst – admin kan overstyre den i tabellen email_content (kind=summary).
DEFAULT_SUMMARY_SUBJECT = "Ukesoppsummering: {antall} besøk ({periode})"
DEFAULT_SUMMARY_BODY = (
    "Hei,\n\nHer er oppsummeringen av uka ({periode}):\n\n"
    "Totalt {antall} besøk.\nUtfall: {utfall}\n"
    "Kjørt denne uka: {total_km} km (~{total_co2} kg CO₂e)\n\n"
    "Besøk:\n{besok_liste}\n\nStå på!\nBNC Nordic"
)


DEFAULT_SUMMARY_SUBJECT_SV = "Veckosammanfattning: {antall} besök ({periode})"
DEFAULT_SUMMARY_BODY_SV = (
    "Hej,\n\nHär är sammanfattningen av veckan ({periode}):\n\n"
    "Totalt {antall} besök.\nUtfall: {utfall}\n"
    "Kört denna vecka: {total_km} km (~{total_co2} kg CO₂e)\n\n"
    "Besök:\n{besok_liste}\n\nKör hårt!\nBNC Nordic"
)


def _defaults(lang):
    if str(lang).lower() == "sv":
        return DEFAULT_SUMMARY_SUBJECT_SV, DEFAULT_SUMMARY_BODY_SV
    return DEFAULT_SUMMARY_SUBJECT, DEFAULT_SUMMARY_BODY


def summary_template(lang="no"):
    d_subj, d_body = _defaults(lang)
    # Prøv (kind, lang); faller tilbake til kun kind (før «lang»-kolonnen finnes) for norsk.
    for params in ({"select": "subject,body", "kind": "eq.summary", "lang": f"eq.{lang}"},
                   {"select": "subject,body", "kind": "eq.summary"} if lang == "no" else None):
        if params is None:
            continue
        try:
            r = requests.get(f"{SUPABASE_URL}/rest/v1/email_content",
                             headers=sb_headers(), params=params, timeout=30)
            r.raise_for_status()
            rows = r.json()
            if rows:
                return (rows[0].get("subject") or d_subj, rows[0].get("body") or d_body)
        except Exception:
            pass
    return d_subj, d_body


def apply_vars(text, mapping):
    out = str(text or "")
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def main():
    rows = week_visits()
    recips = summary_recipients()
    periode = f"{MONDAY.strftime('%d.%m')} – {TODAY.strftime('%d.%m')}"
    tpl_cache = {}
    by_user = {}
    for v in rows:
        by_user.setdefault(v["user_email"], []).append(v)
    print(f"Uke fra {MONDAY}: {len(rows)} besøk, {len(by_user)} selger(e).")
    for user, vs in by_user.items():
        to_email, name, lang = recips.get(user, (user, "", "no"))
        if lang not in tpl_cache:
            tpl_cache[lang] = summary_template(lang)
        subj_tpl, body_tpl = tpl_cache[lang]
        outc = Counter(v.get("outcome", "") for v in vs)
        besok_liste = "\n".join(
            f"- {v['visited_on']}  {v.get('navn','')} ({v.get('kommune','')}) – {v.get('outcome','')}"
            for v in vs)
        tot_km = round(sum(float(v.get("leg_km") or 0) for v in vs), 1)
        tot_co2 = round(sum(float(v.get("co2_g") or 0) for v in vs) / 1000.0, 1)
        m = {"navn": name or user, "periode": periode, "antall": len(vs),
             "utfall": ", ".join(f"{k}: {n}" for k, n in outc.most_common()),
             "besok_liste": besok_liste, "total_km": tot_km, "total_co2": tot_co2}
        try:
            queue(to_email, apply_vars(subj_tpl, m), apply_vars(body_tpl, m), lang)
            print(f"  kø: {user} -> {to_email} ({len(vs)} besøk, {lang})")
        except Exception as exc:
            print(f"  FEIL {user}: {exc}")


if __name__ == "__main__":
    main()
