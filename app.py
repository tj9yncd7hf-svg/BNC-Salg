"""BNC Selger – besøksrute-app for selger på veien.

Selgeren planlegger ruter (radius fra start, eller langs en strekning A→B), ser
drop-in-kandidater, registrerer besøk (i et dialogvindu), kan ekskludere
irrelevante bedrifter, og lagre ruter til datoer (kalender). Rapport + «takk for
praten» + ukesoppsummering går via e-postkøen. Admin styrer brukere og e-postinnhold.
"""
import calendar as _cal
import hashlib
import json
import math
import os
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import streamlit as st

import routing

try:
    from streamlit_option_menu import option_menu
    HAS_OPTION_MENU = True
except Exception:
    HAS_OPTION_MENU = False

APP_DIR = Path(__file__).parent
DATA_FILE = APP_DIR / "data" / "companies.json"
LOGO_WHITE = APP_DIR / "assets" / "bnc_logo_white.webp"
LOGO_NAVY = APP_DIR / "assets" / "bnc_logo_navy.webp"


def _secret(name, default=""):
    v = os.getenv(name, "")
    if v:
        return v
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


BACKOFFICE_EMAIL = _secret("BACKOFFICE_EMAIL", "post@bncnordic.no")
SUPABASE_URL = _secret("SUPABASE_URL").rstrip("/")
SUPABASE_KEY = _secret("SUPABASE_SECRET_KEY")
ADMIN_EMAILS = {e.strip().lower() for e in _secret("ADMIN_EMAILS", "andre@bncnordic.no").split(",") if e.strip()}
APP_BASE_URL = _secret("APP_BASE_URL").rstrip("/")

KATEGORIER = [
    "Verksted", "Bilpleie", "Bilforhandler", "Delegrossist",
    "Bildeler & rekvisita (butikk)", "Bilgrossist (import/engros)",
    "Bensinstasjon / drivstoff", "MC-verksted", "MC-forhandler", "MC-utstyr & klær",
]

DEFAULT_CONTENT = {
    "thankyou": {
        "subject": "Takk for praten – BNC Nordic",
        "body": (
            "Hei,\n\n"
            "Takk for at du tok deg tid til en hyggelig prat da jeg var innom dere! 😊\n\n"
            "Vi i BNC Nordic leverer produkter innen bilpleie, felger, understell og bremser, "
            "og hjelper gjerne med å finne produkter som passer deres behov.\n\n"
            "Har du spørsmål etter besøket, ønsker mer informasjon eller vil ha et tilbud, er "
            "det bare å svare på denne e-posten eller ringe meg på {telefon}.\n\n"
            "Hyggelig å bli kjent – vi holder kontakten!\n\n"
            "{navn}\n"
            "Produktansvarlig / Product manager\n\n"
            "BNC Nordic Distribution AS\n"
            "{telefon}\n"
            "{epost}\n"
            "Strømsveien 323B, 1081 Oslo"
        ),
        "vars": "{navn}, {telefon}, {epost}",
        "label": "Takk for praten (til kunden, 2 virkedager etter besøk)",
    },
    "summary": {
        "subject": "Ukesoppsummering: {antall} besøk ({periode})",
        "body": (
            "Hei,\n\n"
            "Her er oppsummeringen av uka ({periode}):\n\n"
            "Totalt {antall} besøk.\n"
            "Utfall: {utfall}\n"
            "Kjørt denne uka: {total_km} km (~{total_co2} kg CO₂e)\n\n"
            "Besøk:\n{besok_liste}\n\n"
            "Stå på!\nBNC Nordic"
        ),
        "vars": "{navn}, {periode}, {antall}, {utfall}, {besok_liste}, {total_km}, {total_co2}",
        "label": "Ukesoppsummering (til selger, fredager)",
    },
    "report": {
        "subject": "Besøksrapport: {bedrift} ({utfall})",
        "body": (
            "Selger: {selger}\n"
            "Bedrift: {bedrift} (org {orgnr})\n"
            "Adresse: {adresse}\n"
            "Dato: {dato}\n"
            "Utfall: {utfall}\n"
            "Kjøring til besøket: {km} km (~{co2} kg CO₂e)\n\n"
            "Rapport:\n{rapport}\n"
        ),
        "vars": "{selger}, {bedrift}, {orgnr}, {adresse}, {dato}, {utfall}, {rapport}, {km}, {co2}",
        "label": "Besøksrapport (til backoffice, samme dag)",
    },
}


def co2_factor(settings):
    try:
        return float(settings.get("co2_g_per_km") or 13)
    except Exception:
        return 13.0


def co2_kg(km, settings):
    return round(float(km or 0) * co2_factor(settings) / 1000.0, 2)


def apply_vars(text, mapping):
    out = str(text or "")
    for k, v in mapping.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def fill_thanks(text, settings, login_email):
    return apply_vars(text, {
        "navn": str(settings.get("name") or "").strip() or "Kim Werner Strand",
        "telefon": str(settings.get("phone") or "").strip(),
        "epost": str(login_email or "").strip(),
    })


st.set_page_config(page_title="BNC Selger", page_icon="🚗", layout="wide")

st.markdown("""
<style>
  @import url('https://fonts.googleapis.com/css2?family=Archivo:wght@600;700&family=Inter:wght@400;500;600&display=swap');
  :root{ --navy:#1b2a4a; --navy-hi:#2a3e66; --green:#1f9d5b; }
  html, body, [class*="css"]{ font-family:'Inter',system-ui,sans-serif; }
  .stApp{ background:#eef1f5; }
  /* Skjul Streamlit-chrome (meny, deploy, badge) – men BEHOLD sidebar-knappen */
  #MainMenu{ display:none !important; }
  [data-testid="stToolbarActions"]{ display:none !important; }
  [data-testid="stDecoration"]{ display:none !important; }
  .stAppDeployButton, [data-testid="stAppDeployButton"]{ display:none !important; }
  [data-testid="stStatusWidget"]{ display:none !important; }
  footer{ visibility:hidden; height:0; }
  [class*="viewerBadge"]{ display:none !important; }
  header[data-testid="stHeader"]{ background:transparent; }
  [data-testid="stSidebarCollapsedControl"],
  [data-testid="collapsedControl"]{ display:block !important; visibility:visible !important; }
  .block-container{ max-width:1240px; padding-top:1.4rem; padding-bottom:3rem; }
  h1,h2,h3,h4{ font-family:'Archivo','Inter',sans-serif; color:#1c2534; }
  section[data-testid="stSidebar"]{ background:#1b2a4a; }
  section[data-testid="stSidebar"] .stImage{ padding:6px 4px 2px; }
  div.stButton>button[kind="primary"]{ background:#1b2a4a; border-color:#1b2a4a; color:#fff; border-radius:10px; font-weight:600; }
  div.stButton>button[kind="primary"]:hover{ background:#2a3e66; border-color:#2a3e66; }
  div.stButton>button[kind="secondary"]{ border-radius:10px; }
  a[data-testid="stBaseLinkButton-secondary"], a[data-testid="baseLinkButton-secondary"]{ border-radius:10px; }
  div[data-testid="stVerticalBlockBorderWrapper"]{ background:#fff; border-radius:12px; }
  .bnc-sub{ color:#6a7488; font-size:13.5px; margin-top:-6px; }
  .bnc-stop{ background:#fff;border:1px solid #e3e8f0;border-radius:12px;padding:12px 14px;margin-bottom:9px }
  .bnc-stop.done{ opacity:.55;background:#f4f6f9 }
  .bnc-stop b{ color:#1b2a4a }
  .bnc-badge{ display:inline-block;background:#eef2f8;color:#1b2a4a;border-radius:999px;
    padding:2px 10px;font-size:12px;font-weight:600;margin-right:6px }
  .b-ok{ background:#e7f5ee;color:#1f9d5b }
  .b-warn{ background:#f9efd9;color:#9a6a12 }
  .b-no{ background:#fae9e6;color:#bb3b2c }
  .bnc-stat{ background:#fff;border:1px solid #e3e8f0;border-radius:12px;padding:12px 15px }
  .bnc-stat .k{ color:#6a7488;font-size:12px;font-weight:500 }
  .bnc-stat .v{ font-family:'Archivo',sans-serif;font-size:23px;font-weight:700;color:#1c2534 }
  .bnc-stat .v small{ font-size:13px;color:#6a7488;font-weight:600 }
  /* --- Planlegging wizard --- */
  .wz{ display:flex;align-items:center;gap:4px;margin:2px 0 16px }
  .wz .s{ display:flex;align-items:center;gap:7px }
  .wz .num{ width:26px;height:26px;border-radius:50%;background:#dbe0ea;color:#6a7488;
    display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;flex:0 0 auto }
  .wz .s.on .num{ background:#1b2a4a;color:#fff }
  .wz .s.done .num{ background:#1f9d5b;color:#fff }
  .wz .lbl{ font-size:13px;color:#8892a4;white-space:nowrap }
  .wz .s.on .lbl{ color:#1c2534;font-weight:600 }
  .wz .line{ flex:1 1 auto;height:2px;background:#dbe0ea;min-width:8px;border-radius:2px }
  .dcards{ display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:6px 0 2px }
  .dcard{ border:1px solid #e3e8f0;background:#fff;border-radius:12px;padding:10px 12px }
  .dcard.on{ border:2px solid #1b2a4a;padding:9px 11px }
  .dcard .t{ font-size:12px;color:#8892a4;font-weight:600 }
  .dcard .v{ font-family:'Archivo',sans-serif;font-size:17px;font-weight:700;color:#1c2534;margin-top:2px }
  .dcard .v.empty{ color:#aeb6c4;font-weight:500 }
  .psum{ background:#f3f6fb;border:1px solid #e3e8f0;border-radius:12px;padding:10px 14px;
    display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px }
  .psum .pv{ font-weight:600;color:#1c2534 }
  /* kalender: 7 kolonner også på mobil, kompakte dag-knapper */
  .st-key-bnc_cal [data-testid="stHorizontalBlock"]{ flex-wrap:nowrap !important; gap:4px !important }
  .st-key-bnc_cal [data-testid="stColumn"], .st-key-bnc_cal [data-testid="column"]{
    min-width:0 !important; flex:1 1 0 !important; width:auto !important }
  .st-key-bnc_cal [data-testid="stVerticalBlock"]{ gap:4px !important }
  .st-key-bnc_cal button{ padding:6px 0 !important; min-height:40px; border-radius:9px !important;
    position:relative; font-size:14px !important }
  .st-key-bnc_cal .cal-wd{ text-align:center;font-size:11px;font-weight:600;color:#8892a4;padding:2px 0 }
</style>
""", unsafe_allow_html=True)


# ---------------------------------------------------------------- Supabase ----
def _sb_headers():
    return {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json"}


def sb_configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def sb_get(table, select="*", params=None):
    if not sb_configured():
        return []
    p = {"select": select}
    if params:
        p.update(params)
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=_sb_headers(), params=p, timeout=20)
    r.raise_for_status()
    return r.json()


def sb_insert(table, rows):
    if not sb_configured():
        raise RuntimeError("Supabase er ikke satt opp.")
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers={**_sb_headers(), "Prefer": "return=minimal"},
                      json=rows if isinstance(rows, list) else [rows], timeout=20)
    r.raise_for_status()


def sb_upsert(table, row, on_conflict):
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{table}",
                      headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates,return=minimal"},
                      params={"on_conflict": on_conflict}, json=[row], timeout=20)
    r.raise_for_status()


def sb_delete(table, params):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}",
                        headers={**_sb_headers(), "Prefer": "return=minimal"},
                        params=params, timeout=20)
    r.raise_for_status()


# ------------------------------------------------------------------- data -----
@st.cache_data(show_spinner=False)
def load_companies():
    if not DATA_FILE.exists():
        return []
    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return [c for c in data if c.get("lat") is not None and c.get("lon") is not None]


def geocode(text):
    """Startpunkt: 1) adresse, 2) stedsnavn, 3) POI (Nominatim). Dekker «gate nr,
    sted», «Molde lufthavn» og dagligtale som «molde flyplass»."""
    t = (text or "").strip()
    if not t:
        return None, None, text
    try:
        r = requests.get("https://ws.geonorge.no/adresser/v1/sok",
                         params={"sok": t, "treffPerSide": 1, "utkoordsys": 4258}, timeout=15)
        adr = (r.json().get("adresser") or [])
        if adr:
            rp = adr[0].get("representasjonspunkt") or {}
            if rp.get("lat"):
                return float(rp["lat"]), float(rp["lon"]), adr[0].get("adressetekst", t)
    except Exception:
        pass
    try:
        r = requests.get("https://api.kartverket.no/stedsnavn/v1/navn",
                         params={"sok": t, "utkoordsys": 4258, "treffPerSide": 1}, timeout=15)
        navn = (r.json().get("navn") or [])
        if navn:
            rp = navn[0].get("representasjonspunkt") or {}
            if rp.get("nord"):
                return float(rp["nord"]), float(rp["øst"]), navn[0].get("skrivemåte", t)
    except Exception:
        pass
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": t, "format": "json", "limit": 1, "countrycodes": "no"},
                         headers={"User-Agent": "BNC-Selger/1.0 (post@bncnordic.no)"}, timeout=15)
        js = r.json()
        if js:
            return float(js[0]["lat"]), float(js[0]["lon"]), t
    except Exception:
        pass
    return None, None, text


def add_business_days(d, n):
    cur, added = d, 0
    while added < n:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            added += 1
    return cur


def month_key(d=None):
    return (d or date.today()).strftime("%Y-%m")


def _place_q(p):
    # Naviger på ADRESSEN, ikke firmanavnet. Navn som destinasjon får Google til å
    # gjøre et bedriftssøk og lande på nærmeste treff ved brukerens posisjon.
    adr = str(p.get("adresse", "") or "")
    segs = [s.strip() for s in adr.split(",") if s.strip()
            and not s.strip().lower().startswith(("c/o", "co ", "postboks", "pb "))]
    street = segs[-1] if segs else ""
    if street:
        omr = f"{p.get('postnr','')} {p.get('poststed','')}".strip()
        return ", ".join(x for x in [street, omr] if x)
    lat, lon = p.get("lat"), p.get("lon")
    if lat is not None:
        return f"{lat},{lon}"
    return f"{p.get('postnr','')} {p.get('poststed','')}".strip()


def _dest_q(d):
    lbl = (d.get("label") or "").strip()
    if lbl and lbl != "(underveis)":
        return lbl
    return f"{d.get('lat')},{d.get('lon')}"


def gmaps_route_url(start, stops, dest=None):
    if not stops:
        return None
    enc = urllib.parse.quote
    lbl = (start.get("label") or "").strip()
    origin = lbl if lbl and lbl != "(underveis)" else f"{start['lat']},{start['lon']}"
    if dest and dest.get("lat") is not None:
        destination = _dest_q(dest)
        mids = stops[:10]
    else:
        destination = _place_q(stops[-1])
        mids = stops[:-1][:9]
    url = (f"https://www.google.com/maps/dir/?api=1&origin={enc(origin)}"
           f"&destination={enc(destination)}&travelmode=driving")
    if mids:
        url += "&waypoints=" + "%7C".join(enc(_place_q(p)) for p in mids)
    return url


def gmaps_dest_url(p):
    return (f"https://www.google.com/maps/dir/?api=1"
            f"&destination={urllib.parse.quote(_place_q(p))}&travelmode=driving")


def visited_orgnr_this_month(email):
    try:
        rows = sb_get("visits", "orgnr",
                      {"user_email": f"eq.{email}", "month_key": f"eq.{month_key()}"})
        return {str(r["orgnr"]) for r in rows}
    except Exception:
        return set()


def recently_visited(email, days=28):
    """Org.nr. besøkt de siste N dagene (standard 4 uker) – skjules fra nye forslag."""
    try:
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        rows = sb_get("visits", "orgnr",
                      {"user_email": f"eq.{email}", "visited_on": f"gte.{cutoff}"})
        return {str(r["orgnr"]) for r in rows}
    except Exception:
        return set()


def planned_orgnr(email, skip_dates=()):
    """Org.nr. som allerede ligger i andre kommende planlagte ruter – så samme bedrift
    ikke foreslås på to datoer. skip_dates utelates (datoene som nå planlegges på nytt)."""
    try:
        rows = sb_get("planned_routes", "plan_date,companies",
                      {"user_email": f"eq.{email}",
                       "plan_date": f"gte.{date.today().isoformat()}"})
        skip = {d.isoformat() for d in skip_dates}
        out = set()
        for r in rows:
            if r.get("plan_date") in skip:
                continue
            for c in (r.get("companies") or []):
                out.add(str(c.get("orgnr")))
        return out
    except Exception:
        return set()


def delete_visit(email, orgnr):
    sb_delete("visits", {"user_email": f"eq.{email}", "orgnr": f"eq.{orgnr}",
                         "month_key": f"eq.{month_key()}"})


def excluded_orgnr(email):
    try:
        rows = sb_get("excluded", "orgnr", {"user_email": f"eq.{email}"})
        return {str(r["orgnr"]) for r in rows}
    except Exception:
        return set()


def exclude_company(email, c):
    # Unngår on_conflict-upsert: tabellen kan mangle sammensatt PK (gir 400).
    org = str(c.get("orgnr"))
    try:
        if sb_get("excluded", "orgnr", {"user_email": f"eq.{email}", "orgnr": f"eq.{org}"}):
            return
    except Exception:
        pass
    sb_insert("excluded", {"user_email": email, "orgnr": org, "navn": c.get("navn", "")})


def unexclude(email, orgnr):
    sb_delete("excluded", {"user_email": f"eq.{email}", "orgnr": f"eq.{orgnr}"})


def save_plan(email, d, start, companies, meta):
    sb_upsert("planned_routes", {
        "user_email": email, "plan_date": d.isoformat(),
        "start": start, "companies": companies, "meta": meta,
        "created_at": datetime.now(timezone.utc).isoformat()}, "user_email,plan_date")


def get_plan(email, d):
    try:
        rows = sb_get("planned_routes", "*",
                      {"user_email": f"eq.{email}", "plan_date": f"eq.{d.isoformat()}"})
        return rows[0] if rows else None
    except Exception:
        return None


def list_plans(email):
    try:
        return sb_get("planned_routes", "plan_date,companies,start,meta",
                      {"user_email": f"eq.{email}", "plan_date": f"gte.{date.today().isoformat()}",
                       "order": "plan_date.asc"})
    except Exception:
        return []


def delete_plan(email, d):
    sb_delete("planned_routes", {"user_email": f"eq.{email}", "plan_date": f"eq.{d}"})


def load_settings(email):
    try:
        rows = sb_get("salesperson_settings", "*", {"email": f"eq.{email}"})
        return rows[0] if rows else {}
    except Exception:
        return {}


def get_content(kind):
    d = DEFAULT_CONTENT[kind]
    try:
        rows = sb_get("email_content", "kind,subject,body", {"kind": f"eq.{kind}"})
        if rows:
            return {"subject": rows[0].get("subject") or d["subject"],
                    "body": rows[0].get("body") or d["body"]}
    except Exception:
        pass
    return {"subject": d["subject"], "body": d["body"]}


# ------------------------------------------------------------------- auth -----
APP_PASSWORD = _secret("APP_PASSWORD")
LOGIN_TOKEN = (hashlib.sha256(("bncselger::" + APP_PASSWORD).encode()).hexdigest()[:20]
               if APP_PASSWORD else "")
_qp = st.query_params


def _valid_personal_link():
    u = (_qp.get("u") or "").strip().lower()
    k = _qp.get("k") or ""
    if not (u and k):
        return None
    try:
        rows = sb_get("salesperson_settings", "email,login_token", {"email": f"eq.{u}"})
        if rows and rows[0].get("login_token") and rows[0]["login_token"] == k:
            return u
    except Exception:
        return None
    return None


if not st.session_state.get("_authed"):
    _pu = _valid_personal_link()
    if _pu:
        st.session_state["_authed"] = True
        st.session_state["user_email"] = _pu
if APP_PASSWORD and not st.session_state.get("_authed") and _qp.get("t") == LOGIN_TOKEN:
    st.session_state["_authed"] = True
if _qp.get("u") and not st.session_state.get("user_email"):
    st.session_state["user_email"] = _qp.get("u")


def _logo(path, width):
    if path.exists():
        st.image(str(path), width=width)


if APP_PASSWORD and not st.session_state.get("_authed"):
    cols = st.columns([1, 1, 1])
    with cols[1]:
        _logo(LOGO_NAVY, 170)
        st.subheader("Selger på veien")
        _pw = st.text_input("Passord", type="password")
        if st.button("Logg inn", type="primary", use_container_width=True):
            if _pw == APP_PASSWORD:
                st.session_state["_authed"] = True
                st.query_params["t"] = LOGIN_TOKEN
                st.rerun()
            else:
                st.error("Feil passord.")
    st.stop()

if not sb_configured():
    st.error("Supabase er ikke konfigurert (SUPABASE_URL / SUPABASE_SECRET_KEY mangler).")

email = (st.session_state.get("user_email") or "").strip().lower()
if not email:
    cols = st.columns([1, 1, 1])
    with cols[1]:
        _logo(LOGO_NAVY, 170)
        st.subheader("Selger på veien")
        e = st.text_input("Din e-post", placeholder="navn@bncnordic.no")
        if st.button("Fortsett", type="primary", use_container_width=True):
            if e.strip():
                st.session_state["user_email"] = e.strip().lower()
                st.rerun()
        st.caption("Selgere kommer vanligvis inn via sin personlige lenke og slipper dette.")
    st.stop()

if _qp.get("u") != email:
    st.query_params["u"] = email
if LOGIN_TOKEN and _qp.get("t") != LOGIN_TOKEN:
    st.query_params["t"] = LOGIN_TOKEN

_settings = load_settings(email)
is_admin = email in ADMIN_EMAILS
companies = load_companies()

if st.session_state.get("_toast"):
    st.toast(st.session_state.pop("_toast"))


# ------------------------------------------------------------- sidemeny -------
USER_PAGES = ["Min dag", "Drop-in", "Planlegging", "Historikk", "Innstillinger"]
ADMIN_PAGES = ["Brukere", "E-postinnhold"]
PAGE_ICONS = {"Min dag": "map", "Drop-in": "clipboard-check", "Planlegging": "calendar-week",
              "Historikk": "clock-history", "Innstillinger": "gear",
              "Brukere": "people", "E-postinnhold": "envelope"}
pages = USER_PAGES + (ADMIN_PAGES if is_admin else [])

_goto = st.session_state.pop("_goto", None)
with st.sidebar:
    _logo(LOGO_WHITE, 150)
    if HAS_OPTION_MENU:
        _mi = pages.index(_goto) if _goto in pages else None
        nav = option_menu(
            None, pages, icons=[PAGE_ICONS[p] for p in pages],
            default_index=0, key="nav_menu", manual_select=_mi,
            styles={
                "container": {"background-color": "#1b2a4a", "padding": "8px 4px"},
                "nav-link": {"color": "#c3ccdd", "font-size": "14.5px", "font-weight": "500",
                             "margin": "2px 0", "--hover-color": "#2a3e66"},
                "nav-link-selected": {"background-color": "#ffffff", "color": "#1b2a4a",
                                      "font-weight": "600"},
                "icon": {"color": "#9fb0cb", "font-size": "15px"},
            })
    else:
        if _goto in pages:
            st.session_state["nav_menu"] = _goto
        nav = st.radio("Meny", pages, label_visibility="collapsed", key="nav_menu")
    st.markdown("<div style='flex:1'></div>", unsafe_allow_html=True)
    st.markdown(
        f"<div style='color:#c3ccdd;font-size:12.5px;border-top:1px solid #33456b;"
        f"padding-top:10px;margin-top:8px'><b style='color:#fff'>"
        f"{_settings.get('name') or email}</b><br>{email}"
        f"{' · admin' if is_admin else ''}</div>", unsafe_allow_html=True)
    if st.button("Logg ut", use_container_width=True):
        st.query_params.clear()
        for k in list(st.session_state.keys()):
            st.session_state.pop(k, None)
        st.rerun()
if _goto in pages and nav != _goto:
    nav = _goto

if not companies:
    st.warning("Bedriftslista (data/companies.json) mangler eller er tom.")
    st.stop()


def page_header(title, sub):
    st.markdown(f"## {title}")
    st.markdown(f"<div class='bnc-sub'>{sub}</div>", unsafe_allow_html=True)
    st.write("")


UKEDAGER = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"]
_kat_present = [k for k in KATEGORIER if any(c.get("kategori") == k for c in companies)]


def _poly_cum(points):
    seg = [routing.haversine_km(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
           for i in range(len(points) - 1)]
    cum = [0.0]
    for s in seg:
        cum.append(cum[-1] + s)
    return seg, cum, cum[-1]


def _point_at(points, seg, cum, x):
    if x <= 0 or len(points) == 1:
        return points[0]
    for i in range(len(seg)):
        if x <= cum[i + 1] or i == len(seg) - 1:
            f = 0.0 if seg[i] == 0 else max(0.0, min(1.0, (x - cum[i]) / seg[i]))
            a, b = points[i], points[i + 1]
            return (a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1]))
    return points[-1]


def _subpoly(points, seg, cum, lo, hi):
    pts = [_point_at(points, seg, cum, lo)]
    for i, c in enumerate(cum):
        if lo < c < hi:
            pts.append(points[i])
    pts.append(_point_at(points, seg, cum, hi))
    out = [pts[0]]
    for p in pts[1:]:
        if routing.haversine_km(out[-1][0], out[-1][1], p[0], p[1]) > 0.05:
            out.append(p)
    return out if len(out) >= 2 else [pts[0], pts[-1]]


def plan_poly(points, cands, hours, visit_min, speed, detour, maxkm, target_n):
    """Sprer ønsket antall besøk jevnt langs en polylinje (Fra → evt. mellomstopp →
    Til) og reserverer tid til å rekke frem til siste punkt."""
    seg, cum, total = _poly_cum(points)
    res = {"route": [], "total_min": 0, "total_km": round(total, 1), "n_candidates": len(cands)}
    if total < 0.1:
        return res
    lat0, lon0 = points[0]
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    pxy = [((lon - lon0) * kx, (lat - lat0) * ky) for lat, lon in points]
    scored = []
    for c in cands:
        cx = (c["lon"] - lon0) * kx
        cy = (c["lat"] - lat0) * ky
        best_perp, best_cum = 1e9, 0.0
        for i in range(len(pxy) - 1):
            ax, ay = pxy[i]
            bx, by = pxy[i + 1]
            dx, dy = bx - ax, by - ay
            seg2 = dx * dx + dy * dy
            tf = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / seg2))
            perp = math.hypot(cx - (ax + tf * dx), cy - (ay + tf * dy))
            if perp < best_perp:
                best_perp, best_cum = perp, cum[i] + tf * seg[i]
        if best_perp <= detour:
            scored.append((best_cum, best_perp, c))
    if not scored:
        return res
    n = max(1, min(int(target_n or 6), len(scored), 30))
    binw = total / n
    picks, usedc = [], set()
    for j in range(n):
        lo = j * binw
        hi = (j + 1) * binw if j < n - 1 else total + 1
        inb = [(t, p, c) for (t, p, c) in scored
               if lo <= t < hi and str(c.get("orgnr")) not in usedc]
        if inb:
            inb.sort(key=lambda x: x[1])
            picks.append(inb[0])
            usedc.add(str(inb[0][2].get("orgnr")))
    picks.sort(key=lambda x: x[0])
    end = points[-1]
    route, used_min, total_km = [], 0.0, 0.0
    cur = points[0]
    for _t, _p, c in picks:
        leg = routing.haversine_km(cur[0], cur[1], c["lat"], c["lon"])
        if maxkm and leg > maxkm:
            continue
        leg_min = routing.travel_minutes(leg, speed, 1.3)
        reach = routing.travel_minutes(routing.haversine_km(c["lat"], c["lon"], end[0], end[1]), speed, 1.3)
        if used_min + leg_min + visit_min + reach > hours * 60:
            break
        used_min += leg_min + visit_min
        total_km += leg
        route.append({**c, "leg_km": round(leg, 1), "leg_min": round(leg_min),
                      "visit_min": round(visit_min), "cum_min": round(used_min),
                      "stop": len(route) + 1})
        cur = (c["lat"], c["lon"])
    final = routing.haversine_km(cur[0], cur[1], end[0], end[1])
    total_km += final
    used_min += routing.travel_minutes(final, speed, 1.3)
    res.update({"route": route, "total_min": round(used_min), "total_km": round(total_km, 1)})
    return res


def plan_trip(start_txt, til_txt, vias, from_date, to_date, hours, visit_min, valgte, orgvalg,
              maxkm, detour, n_visits=None, round_trip=False):
    """Planlegg etappe(r). Fra → evt. mellomstopp → Til (→ Fra ved tur-retur), med besøk
    spredt langs veien. Periode på flere dager deler strekningen i like etapper (én pr. hverdag)."""
    slat, slon, slabel = geocode(start_txt) if start_txt else (None, None, start_txt)
    if not slat:
        return ("feil", "Fant ikke startadressen. Prøv «gate nr, sted» eller et stedsnavn.")
    if to_date < from_date:
        return ("feil", "Til-datoen må være på eller etter fra-datoen.")
    weekdays = []
    d = from_date
    while d <= to_date and len(weekdays) < 60:
        if d.weekday() < 5:
            weekdays.append(d)
        d += timedelta(days=1)
    if not weekdays:
        return ("feil", "Ingen hverdager i perioden. Velg et datospenn med hverdager.")
    D = len(weekdays)

    visited = recently_visited(email)
    excl = excluded_orgnr(email)
    planned = planned_orgnr(email, skip_dates=weekdays)
    skip = visited | excl | planned
    _off = {"Kun AS": "AS", "Kun ENK": "ENK"}.get(orgvalg)
    pool = [c for c in companies
            if c.get("kategori") in valgte and str(c.get("orgnr")) not in skip
            and (_off is None or c.get("orgform") == _off)]
    speed = float(_settings.get("speed_kmh") or 70)

    if not (til_txt and til_txt.strip()):
        # Uten destinasjon: område-plan rundt start (dedup pr. dag).
        used, made, total_visits = set(), 0, 0
        for day in weekdays:
            cands = [c for c in pool if str(c.get("orgnr")) not in used]
            res = routing.plan_route(slat, slon, cands, hours,
                                     visit_minutes=visit_min, max_leg_km=maxkm)
            route = res["route"]
            if n_visits:
                route = route[:int(n_visits)]
            for s in route:
                used.add(str(s.get("orgnr")))
            if route:
                save_plan(email, day, {"lat": slat, "lon": slon, "label": slabel}, route, {
                    "total_min": res["total_min"], "total_km": res["total_km"],
                    "n_candidates": res["n_candidates"]})
                made += 1
                total_visits += len(route)
        if made == 0:
            return ("tom", "Fant ingen bedrifter med disse valgene. Prøv flere bransjer.")
        return ("ok", f"{made} dag(er) planlagt med til sammen {total_visits} besøk.")

    tlat, tlon, tlabel = geocode(til_txt)
    if not tlat:
        return ("feil", "Fant ikke destinasjonen (Til). Prøv «gate nr, sted» eller et stedsnavn.")
    poly = [(slat, slon)]
    for v in (vias or []):
        va, vo, _vl = geocode(v)
        if va:
            poly.append((va, vo))
    poly.append((tlat, tlon))
    if round_trip:
        poly.append((slat, slon))   # tur-retur: hjem igjen til start
    seg, cum, total = _poly_cum(poly)
    if total < 1:
        return ("feil", "Fra og Til er for nær hverandre.")

    used, made, total_visits = set(), 0, 0
    for k, day in enumerate(weekdays):
        sub = _subpoly(poly, seg, cum, k / D * total, (k + 1) / D * total)
        flabel = slabel if k == 0 else "(underveis)"
        # Etappens endepunkt (for kart/Google Maps): siste dag = Til (eller Fra ved tur-retur).
        if k == D - 1:
            dlabel = slabel if round_trip else tlabel
        else:
            dlabel = "(underveis)"
        dest = {"lat": sub[-1][0], "lon": sub[-1][1], "label": dlabel}
        cands = [c for c in pool if str(c.get("orgnr")) not in used]
        # maxkm («maks avstand mellom besøk») gjelder ikke langs en korridor – der kan
        # nabobesøk ligge langt fra hverandre, og det skal ikke stoppe ruta fram til målet.
        res = plan_poly(sub, cands, hours, visit_min, speed, detour, None, n_visits)
        route = res["route"]
        for s in route:
            used.add(str(s.get("orgnr")))
        if route:
            save_plan(email, day, {"lat": sub[0][0], "lon": sub[0][1], "label": flabel}, route, {
                "total_min": res["total_min"], "total_km": res["total_km"],
                "n_candidates": res["n_candidates"], "dest": dest})
            made += 1
            total_visits += len(route)
    if made == 0:
        return ("tom", "Fant ingen bedrifter langs ruta med disse valgene. "
                       "Prøv flere bransjer eller større omvei.")
    return ("ok", f"{made} dag(er) planlagt med til sammen {total_visits} besøk langs ruta.")


# -------------------------------------------------- dialog: registrer besøk ---
@st.dialog("Registrer besøk")
def register_dialog(company):
    st.markdown(f"**{company.get('navn','')}**  \n"
                f"<span class='bnc-sub'>{company.get('adresse','')}, "
                f"{company.get('postnr','')} {company.get('poststed','')}</span>",
                unsafe_allow_html=True)
    with st.form(f"reg_{company.get('orgnr')}"):
        outcome = st.selectbox("Besøksresultat", ["Interessert", "Vil tenke", "Ikke interessert",
                                                  "Ikke til stede", "Solgt", "Annet"])
        report = st.text_area("Besøksrapport", placeholder="Hva skjedde på besøket …", height=130)
        contact = st.text_input("Kundens e-post", value=company.get("epost", ""))
        send_thanks = st.checkbox("Send «takk for praten» (går ut 2 virkedager etter)",
                                  value=bool(company.get("epost")))
        lagre_besok = st.form_submit_button("💾  Lagre besøk og send rapport", type="primary",
                                            use_container_width=True)
    if lagre_besok:
        try:
            today = date.today()
            leg_km = round(float(company.get("leg_km") or 0), 1)
            co2g = round(leg_km * co2_factor(_settings), 1)
            sb_insert("visits", {
                "user_email": email, "orgnr": str(company.get("orgnr")),
                "navn": company.get("navn", ""), "kommune": company.get("kommune", ""),
                "visited_on": today.isoformat(), "month_key": month_key(today),
                "report": report, "outcome": outcome, "contact_email": contact,
                "leg_km": leg_km, "co2_g": co2g,
            })
            rc = get_content("report")
            rmap = {"selger": email, "bedrift": company.get("navn", ""),
                    "orgnr": company.get("orgnr", ""),
                    "adresse": f"{company.get('adresse','')}, {company.get('postnr','')} {company.get('poststed','')}",
                    "dato": today.isoformat(), "utfall": outcome, "rapport": report,
                    "km": leg_km, "co2": co2_kg(leg_km, _settings)}
            sb_insert("email_queue", {
                "kind": "report", "to_email": BACKOFFICE_EMAIL,
                "subject": apply_vars(rc["subject"], rmap),
                "body": apply_vars(rc["body"], rmap),
                "send_after": today.isoformat(),
                "payload": {"orgnr": str(company.get("orgnr")), "user": email}})
            if send_thanks and contact:
                tc = get_content("thankyou")
                sb_insert("email_queue", {
                    "kind": "thankyou", "to_email": contact,
                    "subject": fill_thanks(tc["subject"], _settings, email),
                    "body": fill_thanks(tc["body"], _settings, email),
                    "send_after": add_business_days(today, 2).isoformat(),
                    "payload": {"navn": company.get("navn", ""), "selger": email}})
            st.session_state["_toast"] = "✅ Besøk registrert – rapport sendt, bedriften er merket besøkt."
            st.rerun()
        except Exception as exc:
            st.error(f"Kunne ikke lagre: {exc}")


def route_map(start, route, dest=None):
    try:
        import pydeck as pdk
        pts = [{"lat": start["lat"], "lon": start["lon"], "navn": "START",
                "label": "S", "color": [31, 157, 91]}]
        pts += [{"lat": s["lat"], "lon": s["lon"], "navn": s.get("navn", ""),
                 "label": str(s["stop"]), "color": [27, 42, 74]} for s in route]
        if dest and dest.get("lat") is not None:
            pts.append({"lat": dest["lat"], "lon": dest["lon"],
                        "navn": dest.get("label") or "Mål", "label": "M", "color": [192, 57, 43]})
        path = [[p["lon"], p["lat"]] for p in pts]
        lats = [p["lat"] for p in pts]
        lons = [p["lon"] for p in pts]
        span = max(max(lats) - min(lats), max(lons) - min(lons))
        zoom = (12 if span < 0.05 else 11 if span < 0.15 else 10 if span < 0.3 else
                9 if span < 0.6 else 8 if span < 1.2 else 7 if span < 2.5 else 6)
        st.pydeck_chart(pdk.Deck(
            map_style=None,
            initial_view_state=pdk.ViewState(latitude=sum(lats) / len(lats),
                                             longitude=sum(lons) / len(lons), zoom=zoom),
            layers=[
                pdk.Layer("PathLayer", data=[{"path": path}], get_path="path",
                          get_width=4, width_min_pixels=3, get_color=[27, 42, 74]),
                pdk.Layer("ScatterplotLayer", data=pts, get_position=["lon", "lat"],
                          get_fill_color="color", get_radius=300,
                          radius_min_pixels=9, radius_max_pixels=20, pickable=True),
                pdk.Layer("TextLayer", data=pts, get_position=["lon", "lat"],
                          get_text="label", get_size=13, get_color=[255, 255, 255],
                          get_alignment_baseline="'center'"),
            ],
            tooltip={"text": "{label}. {navn}"}))
    except Exception:
        st.map([{"lat": p["lat"], "lon": p["lon"]} for p in route])


def leaflet_route_html(start, route, height=360, line_color="#1b2a4a", dest=None):
    """Robust kart (Leaflet i iframe) – virker også inne i expandere, der pydeck feiler.
    dest = sluttpunkt (Til / hjem) som tegnes som rødt mål-punkt."""
    pts = []
    if start and start.get("lat") is not None:
        pts.append({"lat": start["lat"], "lon": start["lon"], "label": "S",
                    "navn": start.get("label") or "Start", "kind": "start"})
    for s in route:
        if s.get("lat") is None:
            continue
        pts.append({"lat": s["lat"], "lon": s["lon"], "label": str(s.get("stop", "")),
                    "navn": s.get("navn", ""), "kind": "stop"})
    if dest and dest.get("lat") is not None:
        pts.append({"lat": dest["lat"], "lon": dest["lon"], "label": "M",
                    "navn": dest.get("label") or "Mål", "kind": "dest"})
    if not pts:
        st.caption("Ingen punkter å vise på kart.")
        return
    tmpl = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{margin:0}#map{height:__H__px;width:100%}</style></head>
<body><div id="map"></div><script>
var pts=__DATA__, line="__LINE__";
var map=L.map('map',{scrollWheelZoom:false});
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
 {maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
var ll=pts.map(function(p){return [p.lat,p.lon];});
L.polyline(ll,{color:line,weight:4}).addTo(map);
function ic(l,c){return L.divIcon({className:'',iconSize:[24,24],iconAnchor:[12,12],
 html:'<div style="background:'+c+';color:#fff;width:24px;height:24px;border-radius:50%;'
 +'display:flex;align-items:center;justify-content:center;font:700 12px sans-serif;'
 +'border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.4)">'+l+'</div>'});}
pts.forEach(function(p){var c=p.kind=='start'?'#1f9d5b':(p.kind=='dest'?'#c0392b':line);
 L.marker([p.lat,p.lon],{icon:ic(p.label,c)}).addTo(map).bindTooltip(p.label+'. '+p.navn);});
var b=L.latLngBounds(ll);
function fit(){if(ll.length>1){map.fitBounds(b,{padding:[30,30],maxZoom:13});}
 else{map.setView(ll[0],12);}}
fit();
setTimeout(function(){map.invalidateSize();fit();},300);
setTimeout(function(){map.invalidateSize();fit();},800);
</script></body></html>"""
    html = (tmpl.replace("__DATA__", json.dumps(pts))
                .replace("__LINE__", line_color)
                .replace("__H__", str(height)))
    import streamlit.components.v1 as components
    components.html(html, height=height + 4)


DAY_COLORS = [[27, 42, 74], [192, 57, 43], [31, 157, 91], [230, 126, 34],
              [142, 68, 173], [41, 128, 185], [22, 160, 133], [212, 172, 13]]


def multi_route_map(plans):
    """Samlet kart for en flerdagstur: én farge per dag, nummererte stopp."""
    try:
        import pydeck as pdk
        paths, pts = [], []
        all_lat, all_lon = [], []
        for di, p in enumerate(plans):
            comps = p.get("companies") or []
            start = p.get("start") or {}
            col = DAY_COLORS[di % len(DAY_COLORS)]
            dt = datetime.fromisoformat(p["plan_date"]).date()
            dag = f"{UKEDAGER[dt.weekday()].capitalize()} {dt.strftime('%d.%m')}"
            line = []
            if start.get("lat") is not None:
                line.append([start["lon"], start["lat"]])
                all_lat.append(start["lat"])
                all_lon.append(start["lon"])
            for s in comps:
                if s.get("lat") is None:
                    continue
                line.append([s["lon"], s["lat"]])
                all_lat.append(s["lat"])
                all_lon.append(s["lon"])
                pts.append({"lat": s["lat"], "lon": s["lon"], "navn": s.get("navn", ""),
                            "label": str(s.get("stop", "")), "dag": dag, "color": col})
            if len(line) >= 2:
                paths.append({"path": line, "color": col})
        if not pts:
            st.caption("Ingen punkter å vise på kart.")
            return
        span = max(max(all_lat) - min(all_lat), max(all_lon) - min(all_lon))
        zoom = (11 if span < 0.15 else 10 if span < 0.3 else 9 if span < 0.6 else
                8 if span < 1.2 else 7 if span < 2.5 else 6 if span < 5 else 5)
        st.pydeck_chart(pdk.Deck(
            map_style=None,
            initial_view_state=pdk.ViewState(latitude=sum(all_lat) / len(all_lat),
                                             longitude=sum(all_lon) / len(all_lon), zoom=zoom),
            layers=[
                pdk.Layer("PathLayer", data=paths, get_path="path", get_color="color",
                          get_width=4, width_min_pixels=3),
                pdk.Layer("ScatterplotLayer", data=pts, get_position=["lon", "lat"],
                          get_fill_color="color", get_radius=300,
                          radius_min_pixels=8, radius_max_pixels=18, pickable=True),
                pdk.Layer("TextLayer", data=pts, get_position=["lon", "lat"],
                          get_text="label", get_size=12, get_color=[255, 255, 255],
                          get_alignment_baseline="'center'"),
            ],
            tooltip={"text": "{dag} · {label}. {navn}"}))
        # fargeforklaring per dag
        chips = []
        for di, p in enumerate(plans):
            if not (p.get("companies") or []):
                continue
            c = DAY_COLORS[di % len(DAY_COLORS)]
            dt = datetime.fromisoformat(p["plan_date"]).date()
            chips.append(f"<span style='display:inline-block;margin:2px 10px 2px 0'>"
                         f"<span style='display:inline-block;width:11px;height:11px;border-radius:3px;"
                         f"background:rgb({c[0]},{c[1]},{c[2]});margin-right:5px'></span>"
                         f"{UKEDAGER[dt.weekday()].capitalize()} {dt.strftime('%d.%m')}</span>")
        st.markdown("<div style='font-size:13px'>" + "".join(chips) + "</div>",
                    unsafe_allow_html=True)
    except Exception:
        st.map([{"lat": pp["lat"], "lon": pp["lon"]}
                for p in plans for pp in (p.get("companies") or []) if pp.get("lat") is not None])


# -------------------------------------------------- Planlegging-wizard (UI) ----
MND_FULL = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august",
            "september", "oktober", "november", "desember"]
MND_ABBR = ["jan.", "feb.", "mars", "apr.", "mai", "juni", "juli", "aug.",
            "sep.", "okt.", "nov.", "des."]


def fmt_date_no(d):
    return f"{d.day}. {MND_ABBR[d.month - 1]} {d.year}" if d else "—"


def count_weekdays(a, b):
    n, d = 0, a
    while d <= b:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def wizard_indicator(step):
    labels = ["Periode", "Område", "Besøksvalg"]
    parts = ["<div class='wz'>"]
    for i, lab in enumerate(labels, 1):
        cls = "on" if i == step else ("done" if i < step else "")
        mark = "✓" if i < step else str(i)
        parts.append(f"<div class='s {cls}'><div class='num'>{mark}</div>"
                     f"<div class='lbl'>{lab}</div></div>")
        if i < 3:
            parts.append("<div class='line'></div>")
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def date_cards(w_from, w_to, active):
    def cell(title, d, on):
        vcls = "v" if d else "v empty"
        val = fmt_date_no(d) if d else "velg dato"
        return (f"<div class='dcard{' on' if on else ''}'><div class='t'>{title}</div>"
                f"<div class='{vcls}'>📅 {val}</div></div>")
    html = ("<div class='dcards'>"
            + cell("Fra", w_from, active == "from")
            + cell("Til", w_to, active == "to")
            + "</div>")
    st.markdown(html, unsafe_allow_html=True)


def calendar_daystyle(cy, cm, w_from, w_to, planned, today):
    """Genererer <style> som farger hver dag-knapp via st-key-klassen."""
    import calendar as _cal
    css = ["<style>"]
    for week in _cal.Calendar(firstweekday=0).monthdatescalendar(cy, cm):
        for i, d in enumerate(week):
            if d.month != cm:
                continue
            iso = d.isoformat()
            sel = f".st-key-cal_{iso} button"
            rules = []
            if i >= 5:
                rules.append("color:#c0392b !important")
            if d == today:
                rules.append("box-shadow:inset 0 0 0 2px #1b2a4a !important")
                rules.append("font-weight:800 !important")
            inrange = w_from and w_to and w_from <= d <= w_to and d not in (w_from, w_to)
            if inrange:
                rules.append("background:#dbe4f3 !important")
                rules.append("border-color:#dbe4f3 !important")
            endpoint = d == w_from or d == w_to
            if endpoint:
                rules.append("background:#1b2a4a !important")
                rules.append("border-color:#1b2a4a !important")
                rules.append("color:#fff !important")
            if rules:
                css.append(sel + "{" + ";".join(rules) + "}")
            if iso in planned:
                dotc = "#fff" if endpoint else "#1f9d5b"
                css.append(f".st-key-cal_{iso} button::after{{content:'';position:absolute;"
                           f"bottom:4px;left:calc(50% - 2px);width:4px;height:4px;"
                           f"border-radius:50%;background:{dotc}}}")
    css.append("</style>")
    st.markdown("".join(css), unsafe_allow_html=True)


# ============================================================== MIN DAG =======
if nav == "Min dag":
    today = date.today()
    _plans = list_plans(email)            # kommende planlagte dager (>= i dag)
    byd = {p["plan_date"]: p for p in _plans}
    opts = sorted(set(byd.keys()) | {today.isoformat()})
    default_iso = today.isoformat() if today.isoformat() in opts else opts[0]
    sel_iso = default_iso
    if len(opts) > 1:
        def _fmt_day(iso):
            dt = datetime.fromisoformat(iso).date()
            n = len(byd.get(iso, {}).get("companies") or [])
            tag = f" — {n} besøk" if n else " — ingen rute"
            pre = "I dag · " if dt == today else ""
            return f"{pre}{UKEDAGER[dt.weekday()].capitalize()} {dt.strftime('%d.%m.%Y')}{tag}"
        sel_iso = st.selectbox("Velg dag", opts, index=opts.index(default_iso),
                               format_func=_fmt_day)
    seldate = datetime.fromisoformat(sel_iso).date()
    wd = UKEDAGER[seldate.weekday()].capitalize()
    _subtitle = ("dagens planlagte rute" if seldate == today else "planlagt rute")
    page_header("Min dag", f"{wd} {seldate.strftime('%d.%m.%Y')} – {_subtitle}")
    plan = byd.get(sel_iso) or get_plan(email, seldate)
    route = (plan or {}).get("companies") or []
    start = (plan or {}).get("start")
    meta = (plan or {}).get("meta") or {}

    if not route:
        _når = "i dag" if seldate == today else "denne dagen"
        st.info(f"📅 Ingen planlagt rute {_når}. Gå til **Planlegging** for å sette opp ruter "
                "– eller bruk **Drop-in** for et besøk på sparket.")
    else:
        h = int(meta.get("total_min", 0)) // 60
        m = int(meta.get("total_min", 0)) % 60
        day_co2 = co2_kg(meta.get("total_km", 0), _settings)
        s1, s2, s3, s4 = st.columns(4)
        s1.markdown(f"<div class='bnc-stat'><div class='k'>Besøk i ruta</div>"
                    f"<div class='v'>{len(route)}</div></div>", unsafe_allow_html=True)
        s2.markdown(f"<div class='bnc-stat'><div class='k'>Samlet kjøring</div>"
                    f"<div class='v'>{meta.get('total_km',0)} <small>km</small></div></div>", unsafe_allow_html=True)
        s3.markdown(f"<div class='bnc-stat'><div class='k'>Beregnet tid</div>"
                    f"<div class='v'>{h} t {m} <small>min</small></div></div>", unsafe_allow_html=True)
        s4.markdown(f"<div class='bnc-stat'><div class='k'>Klimaavtrykk</div>"
                    f"<div class='v'>{day_co2} <small>kg CO₂e</small></div></div>", unsafe_allow_html=True)
        st.write("")

        show_map = st.checkbox("Vis kart", value=True)
        list_col, map_col = st.columns([5, 6]) if show_map else (st.container(), None)
        with list_col:
            g_url = gmaps_route_url(start, route, dest=meta.get("dest")) if start else None
            if g_url:
                st.link_button("🧭  Åpne hele ruta i Google Maps", g_url, use_container_width=True)
            st.markdown("#### Dagens besøk")
            visited = visited_orgnr_this_month(email)
            for s in route:
                org = str(s.get("orgnr"))
                done = org in visited
                _of = s.get("orgform", "")
                _ofh = f" <small style='color:#6a7488'>{_of}</small>" if _of else ""
                badge = ("<span class='bnc-badge b-ok'>✓ Besøkt</span>" if done
                         else f"<span class='bnc-badge'>{s.get('kategori', s.get('bransje_kode',''))}</span>")
                st.markdown(
                    f"<div class='bnc-stop{' done' if done else ''}'><b>{s.get('stop','')}. {s.get('navn','')}</b><br>"
                    f"{badge}{_ofh} {s.get('adresse','')}, {s.get('postnr','')} {s.get('poststed','')}<br>"
                    f"<small>+{s.get('leg_km')} km / {s.get('leg_min')} min · kum. {s.get('cum_min')} min</small></div>",
                    unsafe_allow_html=True)
                if done:
                    if st.button("↩︎ Angre besøk", key=f"undo_{org}"):
                        try:
                            delete_visit(email, org)
                            st.session_state["_toast"] = "Besøk nullstilt."
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")
                else:
                    b1, b2, b3 = st.columns([2, 2, 1])
                    if b1.button("📝  Registrer", key=f"reg_{org}", use_container_width=True):
                        register_dialog(s)
                    b2.link_button("🧭  Naviger", gmaps_dest_url(s), use_container_width=True)
                    if b3.button("✕", key=f"exc_{org}", help="Ikke aktuell – skjul fra forslag",
                                 use_container_width=True):
                        try:
                            exclude_company(email, s)
                            rest = [x for x in route if str(x.get("orgnr")) != org]
                            if rest:
                                save_plan(email, seldate, start, rest, meta)
                            else:
                                delete_plan(email, seldate.isoformat())
                            st.session_state["_toast"] = f"{s.get('navn','')} skjult fra forslag."
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")
        if show_map and map_col is not None and start:
            with map_col:
                route_map(start, route, dest=meta.get("dest"))


# =============================================================== DROP-IN ======
if nav == "Drop-in":
    page_header("Drop-in", "Søk opp en bedrift og registrer et besøk på sparket")
    with st.form("dropin_sok"):
        sc1, sc2 = st.columns([5, 1])
        q = sc1.text_input("Søk bedrift (navn eller org.nr)",
                           placeholder="Skriv navn eller org.nr …", label_visibility="collapsed")
        sc2.form_submit_button("🔍 Søk", use_container_width=True)
    if q:
        ql = q.strip().lower()
        qd = "".join(ch for ch in q if ch.isdigit())
        hits = [c for c in companies
                if ql in str(c.get("navn", "")).lower()
                or (qd and len(qd) >= 3 and qd in str(c.get("orgnr", "")))][:25]
        if not hits:
            st.caption("Ingen treff.")
        for c in hits:
            cc = st.columns([5, 2])
            cc[0].markdown(f"**{c.get('navn','')}**  \n"
                           f"<span class='bnc-sub'>{c.get('kategori','')} · "
                           f"{c.get('poststed','')}</span>", unsafe_allow_html=True)
            if cc[1].button("📝 Registrer", key=f"d_{c.get('orgnr')}", use_container_width=True):
                register_dialog(c)
    else:
        st.info("Søk opp en bedrift for å registrere et drop-in-besøk.")


# ============================================================ PLANLEGGING ======
if nav == "Planlegging":
    page_header("Planlegg besøk", "Finn kundebesøk langs ruta di.")
    today = date.today()
    ss = st.session_state
    ss.setdefault("pl_step", 1)
    ss.setdefault("w_from", None)
    ss.setdefault("w_to", None)
    ss.setdefault("w_calym", (today.year, today.month))
    ss.setdefault("w_start", _settings.get("start_label", ""))
    ss.setdefault("w_til", "")
    ss.setdefault("w_vias", [])
    ss.setdefault("w_retur", False)
    ss.setdefault("w_antall", 6)
    ss.setdefault("w_org", "AS + ENK")
    ss.setdefault("w_all", True)
    ss.setdefault("w_cat", [])
    ss.setdefault("w_hours", float(_settings.get("hours_per_day") or 7))
    ss.setdefault("w_visit", int(_settings.get("avg_visit_minutes") or 60))
    ss.setdefault("w_max", 120)
    ss.setdefault("w_det", 15)
    if ss["pl_step"] > 1 and ss["w_from"] is None:
        ss["pl_step"] = 1

    try:
        allplans = sb_get("planned_routes", "plan_date,companies,start,meta",
                          {"user_email": f"eq.{email}", "order": "plan_date.asc"})
    except Exception:
        allplans = []
    planned_set = {p["plan_date"] for p in allplans if (p.get("companies") or [])}

    wizard_indicator(ss["pl_step"])

    # ------------------------------------------------- STEG 1 · PERIODE --------
    if ss["pl_step"] == 1:
        st.markdown("#### Når skal du reise?")
        st.caption(f"Velg én dag eller en periode. I dag er {UKEDAGER[today.weekday()]} "
                   f"{today.strftime('%d.%m.%Y')}.")
        active = "from" if (ss["w_from"] is None or ss["w_to"] is not None) else "to"
        date_cards(ss["w_from"], ss["w_to"], active)

        sc1, sc2, sc3 = st.columns(3)
        if sc1.button("Én dag", use_container_width=True):
            ss["w_from"] = ss["w_to"] = today
            ss["w_calym"] = (today.year, today.month)
            st.rerun()
        if sc2.button("Denne uka", use_container_width=True):
            mon = today - timedelta(days=today.weekday())
            fri = mon + timedelta(days=4)
            if today.weekday() >= 5:          # helg → neste uke
                mon += timedelta(days=7)
                fri += timedelta(days=7)
                ss["w_from"] = mon
            else:
                ss["w_from"] = today
            ss["w_to"] = fri
            ss["w_calym"] = (ss["w_from"].year, ss["w_from"].month)
            st.rerun()
        if sc3.button("Neste uke", use_container_width=True):
            mon = today - timedelta(days=today.weekday()) + timedelta(days=7)
            ss["w_from"], ss["w_to"] = mon, mon + timedelta(days=4)
            ss["w_calym"] = (mon.year, mon.month)
            st.rerun()

        cy, cm = ss["w_calym"]
        calendar_daystyle(cy, cm, ss["w_from"], ss["w_to"], planned_set, today)
        with st.container(key="bnc_cal"):
            nv1, nv2, nv3 = st.columns([1, 2, 1])
            if nv1.button("‹", use_container_width=True, key="cal_prev"):
                ss["w_calym"] = (cy - 1, 12) if cm == 1 else (cy, cm - 1)
                st.rerun()
            nv2.markdown(f"<div style='text-align:center;font-weight:700;"
                         f"font-family:Archivo,sans-serif;padding-top:7px'>"
                         f"{MND_FULL[cm-1].capitalize()} {cy}</div>", unsafe_allow_html=True)
            if nv3.button("›", use_container_width=True, key="cal_next"):
                ss["w_calym"] = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
                st.rerun()
            hdr = st.columns(7)
            for i, wd in enumerate(["Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn"]):
                hdr[i].markdown(f"<div class='cal-wd'>{wd}</div>", unsafe_allow_html=True)
            for week in _cal.Calendar(firstweekday=0).monthdatescalendar(cy, cm):
                cols = st.columns(7)
                for i, d in enumerate(week):
                    if d.month != cm:
                        cols[i].markdown("<div style='height:40px'></div>",
                                         unsafe_allow_html=True)
                        continue
                    if cols[i].button(str(d.day), key=f"cal_{d.isoformat()}",
                                      use_container_width=True):
                        f, t = ss["w_from"], ss["w_to"]
                        if f is None or t is not None:
                            ss["w_from"], ss["w_to"] = d, None
                        elif d >= f:
                            ss["w_to"] = d
                        else:
                            ss["w_from"], ss["w_to"] = d, None
                        st.rerun()

        if ss["w_from"]:
            nd = count_weekdays(ss["w_from"], ss["w_to"] or ss["w_from"])
            st.caption(f"✅ {nd} hverdag{'er' if nd != 1 else ''} valgt.")
        else:
            st.caption("Velg en startdato i kalenderen.")

        if st.button("Neste: område  →", type="primary", use_container_width=True,
                     disabled=ss["w_from"] is None):
            ss["pl_step"] = 2
            st.rerun()

    # ------------------------------------------------- STEG 2 · OMRÅDE ---------
    elif ss["pl_step"] == 2:
        fr = ss["w_from"]
        to = ss["w_to"] or ss["w_from"]
        if fr == to:
            rtxt = f"{UKEDAGER[fr.weekday()].capitalize()} {fr.strftime('%d.%m.%Y')}"
        else:
            rtxt = (f"{fr.strftime('%d.%m')} – {to.strftime('%d.%m.%Y')} "
                    f"({count_weekdays(fr, to)} hverdager)")
        ps1, ps2 = st.columns([4, 1])
        ps1.markdown(f"<div class='psum'><span>📅 <span class='pv'>{rtxt}</span></span></div>",
                     unsafe_allow_html=True)
        if ps2.button("Endre", use_container_width=True):
            ss["pl_step"] = 1
            st.rerun()

        st.markdown("#### Hvor skal du?")
        ss["w_start"] = st.text_input("Fra · startadresse", value=ss["w_start"],
                                      placeholder="F.eks. Lillehammer", key="_w_start")
        ss["w_til"] = st.text_input("Til · valgfritt", value=ss["w_til"],
                                    placeholder="F.eks. Oslo", key="_w_til",
                                    help="Fyll ut for besøk langs veien. Tomt = området rundt Fra.")
        st.caption("Mellomstopp (valgfritt):")
        for i in range(len(ss["w_vias"])):
            mc1, mc2 = st.columns([6, 1])
            ss["w_vias"][i] = mc1.text_input(f"Mellomstopp {i + 1}", value=ss["w_vias"][i],
                                             key=f"_w_via_{i}", label_visibility="collapsed",
                                             placeholder="F.eks. Gjøvik")
            if mc2.button("✕", key=f"w_via_del_{i}", use_container_width=True):
                ss["w_vias"].pop(i)
                st.rerun()
        if st.button("➕ Legg til mellomstopp", use_container_width=True):
            ss["w_vias"].append("")
            st.rerun()

        ss["w_retur"] = st.checkbox("Tur-retur – hjem igjen til start samme dag",
                                    value=ss["w_retur"], key="_w_retur")
        st.caption("Med destinasjon fordeles ruta på hverdagene i perioden. "
                   "Uten destinasjon planlegges besøk rundt startadressen hver dag.")

        bk, nx = st.columns(2)
        if bk.button("←  Tilbake", use_container_width=True):
            ss["pl_step"] = 1
            st.rerun()
        if nx.button("Neste: besøksvalg  →", type="primary", use_container_width=True,
                     disabled=not (ss["w_start"] or "").strip()):
            ss["pl_step"] = 3
            st.rerun()

    # ------------------------------------------------- STEG 3 · BESØKSVALG -----
    else:
        fr = ss["w_from"]
        to = ss["w_to"] or ss["w_from"]
        area = (ss["w_start"] or "start") + (f" → {ss['w_til']}" if (ss["w_til"] or "").strip()
                                             else " (området rundt)")
        st.markdown(f"<div class='psum'><span>📍 <span class='pv'>{area}</span></span></div>",
                    unsafe_allow_html=True)

        st.markdown("#### Besøksvalg")
        v1, v2 = st.columns(2)
        ss["w_antall"] = v1.number_input("Besøk per dag", 1, 20, int(ss["w_antall"]), 1,
                                         key="_w_antall")
        ss["w_hours"] = v2.number_input("Timer per dag", 1.0, 12.0, float(ss["w_hours"]), 0.5,
                                        key="_w_hours")
        ss["w_visit"] = st.number_input("Minutter per besøk", 10, 180, int(ss["w_visit"]), 5,
                                        key="_w_visit")

        if ss["w_all"]:
            bsum = "Alle bransjer"
        elif ss["w_cat"]:
            bsum = ", ".join(ss["w_cat"][:3]) + (" …" if len(ss["w_cat"]) > 3 else "")
        else:
            bsum = "ingen valgt"
        with st.expander(f"Bransjer: {bsum}"):
            ss["w_all"] = st.checkbox("Alle aktuelle bransjer", value=ss["w_all"], key="_w_all")
            ss["w_cat"] = st.multiselect("…eller velg selv", _kat_present,
                                         default=[c for c in ss["w_cat"] if c in _kat_present],
                                         key="_w_cat", disabled=ss["w_all"])
        with st.expander("Flere valg"):
            _orgs = ["AS + ENK", "Kun AS", "Kun ENK"]
            ss["w_org"] = st.selectbox("Selskapsform", _orgs,
                                       index=_orgs.index(ss["w_org"]) if ss["w_org"] in _orgs else 0,
                                       key="_w_org")
            ss["w_max"] = st.slider("Maks avstand mellom besøk (km)", 10, 300,
                                    int(ss["w_max"]), 10, key="_w_max")
            if (ss["w_til"] or "").strip():
                ss["w_det"] = st.slider("Maks omvei fra ruta (km)", 2, 60,
                                        int(ss["w_det"]), key="_w_det")

        bk2, go2 = st.columns([1, 2])
        if bk2.button("←  Tilbake", use_container_width=True):
            ss["pl_step"] = 2
            st.rerun()
        if go2.button("🧭  Lag ruteforslag", type="primary", use_container_width=True):
            valgte = _kat_present if ss["w_all"] else ss["w_cat"]
            if not valgte:
                st.warning("Velg minst én bransje – eller huk av «Alle aktuelle bransjer».")
            else:
                vias = [v.strip() for v in ss["w_vias"] if v and v.strip()]
                with st.spinner("Lager ruteforslag … henter bedrifter og geokoder adresser."):
                    status, msg = plan_trip(ss["w_start"], ss["w_til"], vias, fr, to,
                                            ss["w_hours"], ss["w_visit"], valgte, ss["w_org"],
                                            ss["w_max"], ss["w_det"], n_visits=int(ss["w_antall"]),
                                            round_trip=ss["w_retur"])
                if status == "ok":
                    ss["pl_step"] = 1
                    st.session_state["_toast"] = f"✅ {msg}"
                    st.rerun()
                elif status == "tom":
                    st.warning(msg)
                else:
                    st.error(msg)

    upcoming = ([p for p in allplans if p["plan_date"] >= today.isoformat()]
                if ss["pl_step"] == 1 else [])
    if ss["pl_step"] == 1:
        st.divider()
        st.markdown("#### Kommende ruter")
        if not upcoming:
            st.caption("Ingen kommende ruter ennå – planlegg en rute over.")
        if len(upcoming) > 1:
            tot_km_all = round(sum(float((p.get("meta") or {}).get("total_km") or 0)
                                   for p in upcoming), 1)
            n_all = sum(len(p.get("companies") or []) for p in upcoming)
            st.markdown(f"**🗺️ Hele turen** — {len(upcoming)} dager · {n_all} besøk · "
                        f"≈ {tot_km_all} km")
            multi_route_map(upcoming)
            st.write("")
    for p in upcoming:
        d = p["plan_date"]
        comps = p.get("companies") or []
        try:
            dt = datetime.fromisoformat(d).date()
            tittel = f"📅 {UKEDAGER[dt.weekday()].capitalize()} {dt.strftime('%d.%m.%Y')} — {len(comps)} besøk"
        except Exception:
            dt, tittel = None, f"📅 {d} — {len(comps)} besøk"
        pstart = p.get("start")
        pmeta = p.get("meta") or {}
        with st.expander(tittel):
            pm = int(pmeta.get("total_min", 0))
            if pmeta:
                st.caption(f"≈ {pmeta.get('total_km', 0)} km · {pm // 60}t {pm % 60}min kjøring")
            pdest = pmeta.get("dest")
            if pstart and pstart.get("lat") is not None and comps:
                leaflet_route_html(pstart, comps, dest=pdest)
            g_url = gmaps_route_url(pstart, comps, dest=pdest) if pstart else None
            if g_url:
                st.link_button("🧭  Åpne ruta i Google Maps", g_url, use_container_width=True)
            for s in comps:
                org = str(s.get("orgnr"))
                rc = st.columns([7, 1])
                rc[0].markdown(f"**{s.get('stop','')}. {s.get('navn','')}** "
                               f"<span class='bnc-sub'>({s.get('poststed','')})</span>",
                               unsafe_allow_html=True)
                if rc[1].button("✕", key=f"plx_{d}_{org}",
                                help="Ikke aktuell – skjul fra forslag og fjern fra ruta",
                                use_container_width=True):
                    try:
                        exclude_company(email, s)
                        rest = [x for x in comps if str(x.get("orgnr")) != org]
                        for i, x in enumerate(rest):
                            x["stop"] = i + 1
                        if rest and dt is not None:
                            save_plan(email, dt, pstart, rest, pmeta)
                        else:
                            delete_plan(email, d)
                        st.session_state["_toast"] = f"{s.get('navn','')} skjult fra forslag."
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{exc}")
            if st.button("🗑️ Slett ruten", key=f"delp_{d}"):
                try:
                    delete_plan(email, d)
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")


# ============================================================= HISTORIKK ======
if nav == "Historikk":
    page_header("Historikk", "Dine besøk og klimaavtrykk")
    periode = st.radio("Periode", ["Denne måneden", "Hittil i år", "Alt"], horizontal=True)
    params = {"user_email": f"eq.{email}", "order": "visited_on.desc"}
    if periode == "Denne måneden":
        params["month_key"] = f"eq.{month_key()}"
    elif periode == "Hittil i år":
        params["visited_on"] = f"gte.{date.today().year}-01-01"
    try:
        rows = sb_get("visits", "*", params)
    except Exception as exc:
        rows = []
        st.error(f"Kunne ikke hente: {exc}")
    tot_km = round(sum(float(r.get("leg_km") or 0) for r in rows), 1)
    tot_co2 = round(sum(float(r.get("co2_g") or 0) for r in rows) / 1000.0, 1)
    m1, m2, m3 = st.columns(3)
    m1.markdown(f"<div class='bnc-stat'><div class='k'>Besøk</div>"
                f"<div class='v'>{len(rows)}</div></div>", unsafe_allow_html=True)
    m2.markdown(f"<div class='bnc-stat'><div class='k'>Kjørt (km)</div>"
                f"<div class='v'>{tot_km}</div></div>", unsafe_allow_html=True)
    m3.markdown(f"<div class='bnc-stat'><div class='k'>Klimaavtrykk</div>"
                f"<div class='v'>{tot_co2} <small>kg CO₂e</small></div></div>", unsafe_allow_html=True)
    st.caption("Kjøring summeres fra avstanden til hvert registrerte besøk (anslag for "
               "konsernrapportering). Besøk søkt opp manuelt teller 0 km.")
    st.write("")
    _cls = {"Interessert": "b-ok", "Solgt": "b-ok", "Vil tenke": "b-warn",
            "Ikke til stede": "b-no", "Ikke interessert": "b-no"}
    for r in rows:
        cls = _cls.get(r.get("outcome", ""), "")
        with st.expander(f"{r.get('navn','')}  ·  {r.get('outcome','')}  ·  {r.get('visited_on','')}"):
            st.markdown(f"<span class='bnc-badge {cls}'>{r.get('outcome','')}</span> "
                        f"<span class='bnc-sub'>{r.get('visited_on','')} · {r.get('kommune','')}</span>",
                        unsafe_allow_html=True)
            st.write(r.get("report") or "_Ingen rapporttekst._")
            if r.get("contact_email"):
                st.caption(f"Takk-e-post til: {r.get('contact_email')}")
            if st.button("↩︎ Angre / slett besøk", key=f"hdel_{r.get('id')}"):
                try:
                    sb_delete("visits", {"id": f"eq.{r.get('id')}"})
                    st.session_state["_toast"] = "Besøk slettet."
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")
    if not rows:
        st.info(f"Ingen besøk i perioden «{periode}».")


# =========================================================== INNSTILLINGER ====
if nav == "Innstillinger":
    page_header("Innstillinger", "Profil og standardverdier for planlegging")
    with st.form("settings"):
        c1, c2 = st.columns(2)
        with c1:
            with st.container(border=True):
                st.markdown("**Selgerprofil**")
                name = st.text_input("Navn", value=_settings.get("name", ""))
                phone = st.text_input("Telefon", value=_settings.get("phone", ""), placeholder="+47 …",
                                      help="Settes automatisk inn i takk-e-posten ({telefon}).")
                summary_email = st.text_input("E-post for ukesoppsummering",
                                              value=_settings.get("summary_email", "") or email,
                                              help="Hit sendes fredagens ukesoppsummering.")
        with c2:
            with st.container(border=True):
                st.markdown("**Standard for planlegging**")
                start_label = st.text_input("Fast startposisjon (adresse)",
                                            value=_settings.get("start_label", ""),
                                            placeholder="Valgfritt – kan overstyres på «Min dag»")
                cc1, cc2 = st.columns(2)
                hours = cc1.number_input("Timer pr. dag", 1.0, 12.0, float(_settings.get("hours_per_day") or 7), 0.5)
                visit_min = cc2.number_input("Snitt pr. besøk (min)", 10, 180, int(_settings.get("avg_visit_minutes") or 60), 5)
                cc3, cc4 = st.columns(2)
                speed = cc3.number_input("Snittfart bil (km/t)", 30, 110, int(_settings.get("speed_kmh") or 70), 5)
                co2 = cc4.number_input("Utslipp (g CO₂e/km)", 0, 400, int(_settings.get("co2_g_per_km") or 13), 1,
                                       help="For konsernrapportering. Typiske anslag: elbil ~13, "
                                            "ladbar hybrid ~60, bensin ~165, diesel ~170.")
        lagre = st.form_submit_button("💾  Lagre innstillinger", type="primary")
    if lagre:
        lat, lon, label = (_settings.get("start_lat"), _settings.get("start_lon"), start_label)
        if start_label:
            _la, _lo, _lb = geocode(start_label)
            if _la:
                lat, lon, label = _la, _lo, _lb
        try:
            sb_upsert("salesperson_settings", {
                "email": email, "name": name, "phone": phone.strip(),
                "summary_email": summary_email.strip().lower(),
                "start_label": label, "start_lat": lat, "start_lon": lon,
                "hours_per_day": hours, "avg_visit_minutes": visit_min, "speed_kmh": speed,
                "co2_g_per_km": co2,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, "email")
            st.success("✅ Lagret." + (f" Startposisjon: {label}." if lat else
                                       " (Fant ikke adressen – startposisjon ikke satt.)"))
        except Exception as exc:
            st.error(f"Kunne ikke lagre: {exc}")

    st.divider()
    with st.expander("🚫 Ekskluderte bedrifter (skjult fra forslag)"):
        try:
            ex = sb_get("excluded", "orgnr,navn", {"user_email": f"eq.{email}", "order": "navn.asc"})
        except Exception:
            ex = []
        if not ex:
            st.caption("Ingen bedrifter er ekskludert.")
        for e in ex:
            xc = st.columns([5, 2])
            xc[0].write(f"{e.get('navn','')}  ({e.get('orgnr','')})")
            if xc[1].button("↩︎ Angre", key=f"unx_{e.get('orgnr')}", use_container_width=True):
                try:
                    unexclude(email, e.get("orgnr"))
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")

    st.divider()
    st.caption("Du kommer inn via din personlige lenke. Legg appen til på Hjem-skjerm på "
               "mobilen (del-knappen → «Legg til på Hjem-skjerm») – da åpner snarveien rett inn.")


# =============================================================== BRUKERE =======
if nav == "Brukere" and is_admin:
    import secrets as _secrets
    page_header("Brukere", "Opprett selgere og gi dem personlig innloggingslenke")

    def personal_link(u):
        tok = u.get("login_token")
        if not tok:
            return None
        qr = urllib.parse.urlencode({"u": u["email"], "k": tok})
        return f"{APP_BASE_URL}/?{qr}" if APP_BASE_URL else f"?{qr}"

    c1, c2 = st.columns([1, 2])
    with c1:
        with st.container(border=True):
            st.markdown("**Ny bruker**")
            with st.form("ny_bruker"):
                bn = st.text_input("Navn", placeholder="Kim Werner Strand")
                be = st.text_input("E-post", placeholder="kim@bncnordic.no").strip().lower()
                if st.form_submit_button("➕  Opprett / ny lenke", type="primary", use_container_width=True):
                    if be:
                        try:
                            sb_upsert("salesperson_settings", {
                                "email": be, "name": bn, "login_token": _secrets.token_urlsafe(12),
                                "updated_at": datetime.now(timezone.utc).isoformat()}, "email")
                            st.success("✅ Bruker klar – lenken vises til høyre.")
                        except Exception as exc:
                            st.error(f"Kunne ikke lagre: {exc}")
                    else:
                        st.error("Skriv inn e-post.")
    with c2:
        if not APP_BASE_URL:
            st.warning("Legg **APP_BASE_URL** i Streamlit Secrets (appens adresse) for komplette lenker.")
        try:
            users = sb_get("salesperson_settings", "email,name,login_token", {"order": "email.asc"})
        except Exception as exc:
            users = []
            st.error(f"Kunne ikke hente brukere: {exc}")
        for u in users:
            with st.container(border=True):
                tag = " · 👑 admin" if u["email"] in ADMIN_EMAILS else ""
                st.markdown(f"**{u.get('name') or '(uten navn)'}** — {u['email']}{tag}")
                link = personal_link(u)
                if link:
                    st.code(link, language=None)
                else:
                    st.caption("Ingen lenke ennå – trykk «Ny lenke».")
                bc = st.columns(2)
                if bc[0].button("🔄 Ny lenke", key=f"nl_{u['email']}", use_container_width=True):
                    try:
                        sb_upsert("salesperson_settings", {
                            "email": u["email"], "login_token": _secrets.token_urlsafe(12),
                            "updated_at": datetime.now(timezone.utc).isoformat()}, "email")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{exc}")
                if u["email"] not in ADMIN_EMAILS:
                    if bc[1].button("🚫 Steng lenke", key=f"rv_{u['email']}", use_container_width=True):
                        try:
                            sb_upsert("salesperson_settings", {
                                "email": u["email"], "login_token": None,
                                "updated_at": datetime.now(timezone.utc).isoformat()}, "email")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")


# ========================================================= E-POSTINNHOLD =======
if nav == "E-postinnhold" and is_admin:
    page_header("E-postinnhold", "Rediger de automatiske e-postene. Selgerne ser ikke denne siden.")
    for kind in ("thankyou", "summary", "report"):
        d = DEFAULT_CONTENT[kind]
        cur = get_content(kind)
        with st.expander(d["label"], expanded=(kind == "thankyou")):
            with st.form(f"ec_{kind}"):
                subj = st.text_input("Emne", value=cur["subject"])
                body = st.text_area("Tekst", value=cur["body"], height=260)
                st.caption(f"Variabler som fylles automatisk: {d['vars']}")
                if st.form_submit_button("💾  Lagre", type="primary"):
                    try:
                        sb_upsert("email_content", {
                            "kind": kind, "subject": subj, "body": body,
                            "updated_at": datetime.now(timezone.utc).isoformat()}, "kind")
                        st.success("Lagret.")
                    except Exception as exc:
                        st.error(f"Kunne ikke lagre: {exc}")

    st.divider()
    with st.expander("🧪 Send testmail (kontroll før lansering)"):
        st.caption("Legger en test i kø og sendes av E-postjobben (kjør den i GitHub Actions "
                   "eller vent inntil 1 time).")
        tt_to = st.text_input("Send test til", value=email, key="tt_to")
        tt_kind = st.selectbox("Type", ["thankyou", "summary", "report"],
                               format_func=lambda k: DEFAULT_CONTENT[k]["label"], key="tt_kind")
        cur = get_content(tt_kind)
        if tt_kind == "thankyou":
            tsub = fill_thanks(cur["subject"], _settings, email)
            tbody = fill_thanks(cur["body"], _settings, email)
        elif tt_kind == "summary":
            demo = {"navn": _settings.get("name") or "Kim", "periode": "13.10 – 17.10",
                    "antall": "2", "utfall": "Interessert: 1, Vil tenke: 1",
                    "total_km": "84,2", "total_co2": "1,1",
                    "besok_liste": "- 13.10  Eksempel Bil AS (OSLO) – Interessert\n"
                                   "- 14.10  Demo Verksted AS (BÆRUM) – Vil tenke"}
            tsub, tbody = apply_vars(cur["subject"], demo), apply_vars(cur["body"], demo)
        else:
            demo = {"selger": email, "bedrift": "Eksempel Bil AS", "orgnr": "999999999",
                    "adresse": "Eksempelveien 1, 0001 OSLO", "dato": date.today().isoformat(),
                    "utfall": "Interessert", "km": "12,4", "co2": "0,16",
                    "rapport": "Testrapport for å kontrollere formatet."}
            tsub, tbody = apply_vars(cur["subject"], demo), apply_vars(cur["body"], demo)
        st.caption("Forhåndsvisning:")
        st.code(tsub, language=None)
        st.text(tbody)
        if st.button("📨  Legg testmail i kø"):
            try:
                sb_insert("email_queue", {
                    "kind": tt_kind, "to_email": tt_to.strip(),
                    "subject": "[TEST] " + tsub, "body": tbody,
                    "send_after": date.today().isoformat(), "payload": {"test": True}})
                st.success(f"✅ Testmail lagt i kø til {tt_to.strip()}. Kjør «E-postjobb» i "
                           "GitHub Actions for å sende nå.")
            except Exception as exc:
                st.error(f"Kunne ikke legge i kø: {exc}")
