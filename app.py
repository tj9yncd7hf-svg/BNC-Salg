"""BNC Selger – besøksrute-app for selger på veien.

Selgeren planlegger ruter (radius fra start, eller langs en strekning A→B), ser
drop-in-kandidater, registrerer besøk (i et dialogvindu), kan ekskludere
irrelevante bedrifter, og lagre ruter til datoer (kalender). Rapport + «takk for
praten» + ukesoppsummering går via e-postkøen. Admin styrer brukere og e-postinnhold.
"""
import hashlib
import json
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
    omr = f"{p.get('postnr','')} {p.get('poststed','')}".strip()
    parts = [p.get("navn", ""), p.get("adresse", ""), omr]
    q = ", ".join(x for x in parts if x)
    return q or f"{p.get('lat')},{p.get('lon')}"


def gmaps_route_url(start, stops):
    if not stops:
        return None
    enc = urllib.parse.quote
    origin = start.get("label") or f"{start['lat']},{start['lon']}"
    dest = _place_q(stops[-1])
    mids = stops[:-1][:9]
    url = (f"https://www.google.com/maps/dir/?api=1&origin={enc(origin)}"
           f"&destination={enc(dest)}&travelmode=driving")
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
    sb_upsert("excluded", {"user_email": email, "orgnr": str(c.get("orgnr")),
                           "navn": c.get("navn", ""),
                           "created_at": datetime.now(timezone.utc).isoformat()},
              "user_email,orgnr")


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
USER_PAGES = ["Min dag", "Besøk", "Planlagt", "Historikk", "Innstillinger"]
ADMIN_PAGES = ["Brukere", "E-postinnhold"]
PAGE_ICONS = {"Min dag": "map", "Besøk": "clipboard-check", "Planlagt": "calendar-week",
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


# -------------------------------------------------- dialog: registrer besøk ---
@st.dialog("Registrer besøk")
def register_dialog(company):
    st.markdown(f"**{company.get('navn','')}**  \n"
                f"<span class='bnc-sub'>{company.get('adresse','')}, "
                f"{company.get('postnr','')} {company.get('poststed','')}</span>",
                unsafe_allow_html=True)
    outcome = st.selectbox("Besøksresultat", ["Interessert", "Vil tenke", "Ikke interessert",
                                              "Ikke til stede", "Solgt", "Annet"])
    report = st.text_area("Besøksrapport", placeholder="Hva skjedde på besøket …", height=130)
    contact = st.text_input("Kundens e-post", value=company.get("epost", ""))
    send_thanks = st.checkbox("Send «takk for praten» (går ut 2 virkedager etter)",
                              value=bool(company.get("epost")))
    if st.button("💾  Lagre besøk og send rapport", type="primary", use_container_width=True):
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


def route_map(start, route):
    try:
        import pydeck as pdk
        pts = [{"lat": start["lat"], "lon": start["lon"], "navn": "START",
                "label": "S", "color": [31, 157, 91]}]
        pts += [{"lat": s["lat"], "lon": s["lon"], "navn": s.get("navn", ""),
                 "label": str(s["stop"]), "color": [27, 42, 74]} for s in route]
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


# ============================================================== MIN DAG =======
if nav == "Min dag":
    page_header("Min dag", "Planlegg ruta, naviger og registrer besøk")
    _def_label = _settings.get("start_label") or ""
    _kat_present = [k for k in KATEGORIER if any(c.get("kategori") == k for c in companies)]

    # Last dagens planlagte rute automatisk (hvis ingen aktiv rute).
    if not st.session_state.get("route"):
        _plan = get_plan(email, date.today())
        if _plan and _plan.get("companies"):
            st.session_state["route"] = _plan["companies"]
            st.session_state["route_start"] = _plan.get("start")
            st.session_state["route_meta"] = _plan.get("meta") or {}
            st.session_state["_from_plan"] = True

    with st.container(border=True):
        with st.form("plan"):
            c1, c2, c3 = st.columns([2, 1, 1])
            start_txt = c1.text_input("Startposisjon", value=_def_label,
                                      placeholder="Adresse eller sted, f.eks. Molde lufthavn")
            hours = c2.number_input("Timer på veien", 1.0, 12.0,
                                    float(_settings.get("hours_per_day") or 7), 0.5)
            visit_min = c3.number_input("Snitt pr. besøk (min)", 10, 180,
                                        int(_settings.get("avg_visit_minutes") or 60), 5)
            dest_txt = st.text_input("Til (valgfritt – reiser du et sted?)",
                                     placeholder="F.eks. Sarpsborg – viser bedrifter langs ruta dit")
            velg_alle = st.checkbox("Velg alle bransjer")
            valgte_cat = st.multiselect(
                "Bransjer", _kat_present, default=[],
                help="La stå tom og huk av «Velg alle bransjer», eller velg kun de du vil besøke.")
            orgvalg = st.radio("Selskapsform", ["AS + ENK", "Kun AS", "Kun ENK"], horizontal=True)
            cc1, cc2 = st.columns(2)
            maxkm = cc1.slider("Maks avstand mellom besøk (km)", 10, 300, 120, 10)
            detour = cc2.slider("Maks omvei fra strekning (km)", 2, 50, 10,
                                help="Brukes når «Til» er fylt ut – hvor langt fra kjøreruta et stopp kan ligge.")
            planlegg = st.form_submit_button("🧭  Foreslå rute", type="primary",
                                             use_container_width=True)

    if planlegg:
        valgte = _kat_present if velg_alle else valgte_cat
        if not valgte:
            st.warning("Velg minst én bransje – eller huk av «Velg alle bransjer».")
        else:
            lat, lon, label = geocode(start_txt) if start_txt else (None, None, start_txt)
            if not lat:
                st.error("Fant ikke startposisjonen. Prøv «gate nr, sted» eller et stedsnavn.")
            else:
                dlat = dlon = None
                if dest_txt.strip():
                    dlat, dlon, _dlabel = geocode(dest_txt)
                    if not dlat:
                        st.warning("Fant ikke destinasjonen – planlegger radius fra start i stedet.")
                visited = visited_orgnr_this_month(email)
                excl = excluded_orgnr(email)
                _off = {"Kun AS": "AS", "Kun ENK": "ENK"}.get(orgvalg)
                cands = [c for c in companies
                         if c.get("kategori") in valgte
                         and str(c.get("orgnr")) not in visited
                         and str(c.get("orgnr")) not in excl
                         and (_off is None or c.get("orgform") == _off)]
                if dlat:
                    base = routing.haversine_km(lat, lon, dlat, dlon)
                    cands = [c for c in cands
                             if (routing.haversine_km(lat, lon, c["lat"], c["lon"])
                                 + routing.haversine_km(c["lat"], c["lon"], dlat, dlon)
                                 - base) <= detour]
                res = routing.plan_route(lat, lon, cands, hours,
                                         visit_minutes=visit_min, max_leg_km=maxkm)
                st.session_state["route"] = res["route"]
                st.session_state["route_start"] = {"lat": lat, "lon": lon, "label": label}
                st.session_state["route_meta"] = res
                st.session_state.pop("_from_plan", None)

    route = st.session_state.get("route") or []
    start = st.session_state.get("route_start")
    meta = st.session_state.get("route_meta") or {}
    if route and start:
        if st.session_state.get("_from_plan"):
            st.info("📅 Dette er din planlagte rute for i dag. Trykk «Foreslå rute» for å lage en ny.")
        st.write("")
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
        st.caption(f"Av {meta.get('n_candidates', 0)} aktuelle bedrifter. CO₂e med "
                   f"{int(co2_factor(_settings))} g/km (endres under Innstillinger).")

        with st.expander("📅 Lagre denne ruta til en dato"):
            pdato = st.date_input("Dato", value=date.today() + timedelta(days=3),
                                  min_value=date.today())
            if st.button("Lagre rute til valgt dato", type="primary"):
                try:
                    save_plan(email, pdato, start, route, meta)
                    st.success(f"✅ Rute lagret til {pdato.isoformat()} (se «Planlagt»).")
                except Exception as exc:
                    st.error(f"Kunne ikke lagre: {exc}")
        st.write("")

        show_map = st.checkbox("Vis kart", value=True)
        list_col, map_col = st.columns([5, 6]) if show_map else (st.container(), None)
        with list_col:
            g_url = gmaps_route_url(start, route)
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
                    f"<div class='bnc-stop{' done' if done else ''}'><b>{s['stop']}. {s.get('navn','')}</b><br>"
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
                            st.session_state["route"] = [x for x in route if str(x.get("orgnr")) != org]
                            st.session_state["_toast"] = f"{s.get('navn','')} skjult fra forslag."
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")
        if show_map and map_col is not None:
            with map_col:
                route_map(start, route)
    else:
        st.info("Sett startposisjon, timer og bransjer over, og trykk **Foreslå rute**. "
                "Fyll «Til» hvis du vil ha drop-in-besøk langs en strekning.")


# ================================================================ BESØK =======
if nav == "Besøk":
    page_header("Registrer besøk", "Søk opp en bedrift (drop-in) og registrer")
    q = st.text_input("Søk bedrift", placeholder="Skriv navn …")
    if q:
        hits = [c for c in companies if q.lower() in str(c.get("navn", "")).lower()][:25]
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


# =============================================================== PLANLAGT ======
if nav == "Planlagt":
    page_header("Planlagt", "Ruter du har lagt opp fremover")
    plans = list_plans(email)
    if not plans:
        st.info("Ingen planlagte ruter. Lag en rute på «Min dag» og trykk "
                "«Lagre denne ruta til en dato».")
    for p in plans:
        d = p["plan_date"]
        comps = p.get("companies") or []
        try:
            dt = datetime.fromisoformat(d).date()
            ukedag = ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"][dt.weekday()]
            tittel = f"📅 {ukedag.capitalize()} {dt.strftime('%d.%m.%Y')} — {len(comps)} besøk"
        except Exception:
            tittel = f"📅 {d} — {len(comps)} besøk"
        with st.expander(tittel):
            for s in comps:
                st.markdown(f"- **{s.get('stop','')}. {s.get('navn','')}** "
                            f"<span class='bnc-sub'>({s.get('poststed','')})</span>",
                            unsafe_allow_html=True)
            b1, b2 = st.columns(2)
            if b1.button("📍 Åpne i Min dag", key=f"open_{d}", use_container_width=True):
                st.session_state["route"] = comps
                st.session_state["route_start"] = p.get("start")
                st.session_state["route_meta"] = p.get("meta") or {}
                st.session_state["_from_plan"] = True
                st.session_state["_goto"] = "Min dag"
                st.rerun()
            if b2.button("🗑️ Slett", key=f"delp_{d}", use_container_width=True):
                try:
                    delete_plan(email, d)
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")


# ============================================================= HISTORIKK ======
if nav == "Historikk":
    page_header("Historikk", f"Dine besøk i {month_key()}")
    try:
        rows = sb_get("visits", "*",
                      {"user_email": f"eq.{email}", "month_key": f"eq.{month_key()}",
                       "order": "visited_on.desc"})
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
        st.info("Ingen besøk registrert denne måneden ennå.")


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
