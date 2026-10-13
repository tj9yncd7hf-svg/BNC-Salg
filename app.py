"""BNC Selger – besøksrute-app for selger på veien.

Selgeren setter startposisjon + timer + snitt-tid pr. besøk, og appen foreslår
en kjørerute med bedrifter (AS, utvalgte bilbransjer, ikke konkurs) fra
Brønnøysund. Besøk hukes av (utelates resten av måneden), rapport sendes til
backoffice, og «takk for praten» + ukesoppsummering går via e-postkøen.

Admin (André) oppretter brukere og styrer innholdet i alle e-postene. Selgeren
(Kim) ser kun sine egne sider: Min dag, Besøk, Historikk, Innstillinger.
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

# Vennlige bransjenavn (kategori-feltet i companies.json). Rekkefølge = visning.
KATEGORIER = [
    "Verksted", "Bilpleie", "Bilforhandler", "Delegrossist",
    "Bildeler & rekvisita (butikk)", "Bilgrossist (import/engros)",
    "Bensinstasjon / drivstoff", "MC-verksted", "MC-forhandler", "MC-utstyr & klær",
]

# ---------------------------------------------------------- e-postinnhold -----
# Standardtekster. Admin kan overstyre dem (lagres i tabellen email_content).
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
    """g CO₂e pr. km fra profilen (standard elbil-anslag)."""
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
  /* Sidemeny marineblå */
  section[data-testid="stSidebar"]{ background:#1b2a4a; }
  section[data-testid="stSidebar"] .stImage{ padding:6px 4px 2px; }
  /* Primærknapper */
  div.stButton>button[kind="primary"]{ background:#1b2a4a; border-color:#1b2a4a; color:#fff; border-radius:10px; font-weight:600; }
  div.stButton>button[kind="primary"]:hover{ background:#2a3e66; border-color:#2a3e66; }
  div.stButton>button[kind="secondary"]{ border-radius:10px; }
  a[data-testid="stBaseLinkButton-secondary"], a[data-testid="baseLinkButton-secondary"]{ border-radius:10px; }
  /* Kort (st.container(border=True)) */
  div[data-testid="stVerticalBlockBorderWrapper"]{ background:#fff; border-radius:12px; }
  /* Toppnivå-radio skjules (vi bruker sidemeny) */
  .bnc-sub{ color:#6a7488; font-size:13.5px; margin-top:-6px; }
  .bnc-stop{ background:#fff;border:1px solid #e3e8f0;border-radius:12px;padding:12px 14px;margin-bottom:9px }
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


# ------------------------------------------------------------------- data -----
@st.cache_data(show_spinner=False)
def load_companies():
    if not DATA_FILE.exists():
        return []
    with open(DATA_FILE, encoding="utf-8") as f:
        data = json.load(f)
    return [c for c in data if c.get("lat") is not None and c.get("lon") is not None]


def geocode(text):
    try:
        r = requests.get("https://ws.geonorge.no/adresser/v1/sok",
                         params={"sok": text, "treffPerSide": 1, "utkoordsys": 4258}, timeout=20)
        adr = (r.json().get("adresser") or [])
        if adr:
            rp = adr[0].get("representasjonspunkt") or {}
            if rp.get("lat"):
                return float(rp["lat"]), float(rp["lon"]), adr[0].get("adressetekst", text)
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


def load_settings(email):
    try:
        rows = sb_get("salesperson_settings", "*", {"email": f"eq.{email}"})
        return rows[0] if rows else {}
    except Exception:
        return {}


def get_content(kind):
    """E-postinnhold: admin-overstyring fra DB, ellers standardtekst."""
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


# Passord-gate (kun for admin/manuell pålogging – brukere kommer inn via lenke).
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

# E-post / identitet.
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


# ------------------------------------------------------------- sidemeny -------
USER_PAGES = ["Min dag", "Besøk", "Historikk", "Innstillinger"]
ADMIN_PAGES = ["Brukere", "E-postinnhold"]
PAGE_ICONS = {"Min dag": "map", "Besøk": "clipboard-check", "Historikk": "clock-history",
              "Innstillinger": "gear", "Brukere": "people", "E-postinnhold": "envelope"}
pages = USER_PAGES + (ADMIN_PAGES if is_admin else [])

with st.sidebar:
    _logo(LOGO_WHITE, 150)
    if HAS_OPTION_MENU:
        nav = option_menu(
            None, pages, icons=[PAGE_ICONS[p] for p in pages],
            default_index=0, key="nav_menu",
            styles={
                "container": {"background-color": "#1b2a4a", "padding": "8px 4px"},
                "nav-link": {"color": "#c3ccdd", "font-size": "14.5px", "font-weight": "500",
                             "margin": "2px 0", "--hover-color": "#2a3e66"},
                "nav-link-selected": {"background-color": "#ffffff", "color": "#1b2a4a",
                                      "font-weight": "600"},
                "icon": {"color": "#9fb0cb", "font-size": "15px"},
            })
    else:
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

# «Registrer besøk»-knappen på Min dag ber om å bytte til Besøk-siden.
if st.session_state.pop("force_besok", False):
    nav = "Besøk"

if not companies:
    st.warning("Bedriftslista (data/companies.json) mangler eller er tom.")
    st.stop()


def page_header(title, sub):
    st.markdown(f"## {title}")
    st.markdown(f"<div class='bnc-sub'>{sub}</div>", unsafe_allow_html=True)
    st.write("")


# ============================================================== MIN DAG =======
if nav == "Min dag":
    page_header("Min dag", "Planlegg ruta, naviger og registrer besøk")
    _def_lat = _settings.get("start_lat")
    _def_lon = _settings.get("start_lon")
    _def_label = _settings.get("start_label") or ""
    _kat_present = [k for k in KATEGORIER if any(c.get("kategori") == k for c in companies)]

    with st.container(border=True):
        with st.form("plan"):
            c1, c2, c3 = st.columns([2, 1, 1])
            start_txt = c1.text_input("Startposisjon", value=_def_label,
                                      placeholder="F.eks. Storgata 1, Oslo")
            hours = c2.number_input("Timer på veien", 1.0, 12.0,
                                    float(_settings.get("hours_per_day") or 7), 0.5)
            visit_min = c3.number_input("Snitt pr. besøk (min)", 10, 180,
                                        int(_settings.get("avg_visit_minutes") or 60), 5)
            velg_alle = st.checkbox("Velg alle bransjer")
            valgte_cat = st.multiselect(
                "Bransjer", _kat_present, default=[],
                help="La stå tom og huk av «Velg alle bransjer», eller velg kun de du vil besøke.")
            orgvalg = st.radio(
                "Selskapsform", ["AS + ENK", "Kun AS", "Kun ENK"], horizontal=True,
                help="ENK = enkeltpersonforetak, ofte små bilpleie-/verksted-aktører.")
            maxkm = st.slider("Maks avstand mellom besøk (km)", 10, 300, 120, 10)
            planlegg = st.form_submit_button("🧭  Foreslå rute", type="primary",
                                             use_container_width=True)

    if planlegg:
        valgte = _kat_present if velg_alle else valgte_cat
        if not valgte:
            st.warning("Velg minst én bransje – eller huk av «Velg alle bransjer».")
        else:
            lat, lon, label = (_def_lat, _def_lon, _def_label)
            if start_txt and start_txt != _def_label:
                lat, lon, label = geocode(start_txt)
            if not lat:
                st.error("Fant ikke startposisjonen. Prøv «gate nr, sted».")
            else:
                visited = visited_orgnr_this_month(email)
                _off = {"Kun AS": "AS", "Kun ENK": "ENK"}.get(orgvalg)
                cands = [c for c in companies
                         if c.get("kategori") in valgte and str(c.get("orgnr")) not in visited
                         and (_off is None or c.get("orgform") == _off)]
                res = routing.plan_route(lat, lon, cands, hours,
                                         visit_minutes=visit_min, max_leg_km=maxkm)
                st.session_state["route"] = res["route"]
                st.session_state["route_start"] = {"lat": lat, "lon": lon, "label": label}
                st.session_state["route_meta"] = res

    route = st.session_state.get("route") or []
    start = st.session_state.get("route_start")
    meta = st.session_state.get("route_meta") or {}
    if route and start:
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
        st.caption(f"Av {meta.get('n_candidates', 0)} aktuelle bedrifter innenfor valgene dine. "
                   f"CO₂e beregnet med {int(co2_factor(_settings))} g/km (endres under Innstillinger).")
        st.write("")

        show_map = st.checkbox("Vis kart", value=True)
        list_col, map_col = st.columns([5, 6]) if show_map else (st.container(), None)

        with list_col:
            g_url = gmaps_route_url(start, route)
            if g_url:
                st.link_button("🧭  Åpne hele ruta i Google Maps", g_url, use_container_width=True)
            st.markdown("#### Dagens besøk")
            for s in route:
                _of = s.get("orgform", "")
                _ofh = f" <small style='color:#6a7488'>{_of}</small> " if _of else ""
                st.markdown(
                    f"<div class='bnc-stop'><b>{s['stop']}. {s.get('navn','')}</b><br>"
                    f"<span class='bnc-badge'>{s.get('kategori', s.get('bransje_kode',''))}</span>"
                    f"{_ofh}"
                    f"{s.get('adresse','')}, {s.get('postnr','')} {s.get('poststed','')}<br>"
                    f"<small>+{s.get('leg_km')} km / {s.get('leg_min')} min · kum. {s.get('cum_min')} min</small></div>",
                    unsafe_allow_html=True)
                b1, b2 = st.columns(2)
                if b1.button("📝  Registrer besøk", key=f"reg_{s.get('orgnr')}", use_container_width=True):
                    st.session_state["selected_company"] = s
                    st.session_state["force_besok"] = True
                    st.rerun()
                b2.link_button("🧭  Naviger", gmaps_dest_url(s), use_container_width=True)

        if show_map and map_col is not None:
            with map_col:
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
                        tooltip={"text": "{label}. {navn}"},
                    ))
                except Exception:
                    st.map([{"lat": p["lat"], "lon": p["lon"]} for p in route])
    else:
        st.info("Sett startposisjon, timer og bransjer over, og trykk **Foreslå rute**.")


# ================================================================ BESØK =======
if nav == "Besøk":
    page_header("Registrer besøk", "Resultat, rapport og oppfølging")
    sel = st.session_state.get("selected_company")
    if not sel:
        q = st.text_input("Søk bedrift", placeholder="Skriv navn …")
        hits = [c for c in companies if q and q.lower() in str(c.get("navn", "")).lower()][:20]
        if hits:
            lbl = st.selectbox("Treff", [f"{c['navn']} · {c.get('poststed','')}" for c in hits])
            sel = hits[[f"{c['navn']} · {c.get('poststed','')}" for c in hits].index(lbl)]
    if sel:
        with st.container(border=True):
            st.markdown(f"**{sel.get('navn','')}**  \n"
                        f"<span class='bnc-sub'>{sel.get('adresse','')}, "
                        f"{sel.get('postnr','')} {sel.get('poststed','')}</span>", unsafe_allow_html=True)
        with st.form("besok"):
            st.markdown("**Besøksresultat**")
            outcome = st.selectbox("Utfall", ["Interessert", "Vil tenke", "Ikke interessert",
                                              "Ikke til stede", "Solgt", "Annet"],
                                   label_visibility="collapsed")
            st.markdown("**Besøksrapport**")
            report = st.text_area("Rapport", placeholder="Hva skjedde på besøket …",
                                  height=150, label_visibility="collapsed")
            st.markdown("**Oppfølging på e-post**")
            contact = st.text_input("Kundens e-post", value=sel.get("epost", ""))
            send_thanks = st.checkbox("Send «takk for praten» (går ut 2 virkedager etter besøket)",
                                      value=bool(sel.get("epost")))
            lagre = st.form_submit_button("💾  Lagre besøk og send rapport", type="primary",
                                          use_container_width=True)
        if lagre:
            try:
                today = date.today()
                leg_km = round(float(sel.get("leg_km") or 0), 1)
                visit_co2_g = round(leg_km * co2_factor(_settings), 1)
                sb_insert("visits", {
                    "user_email": email, "orgnr": str(sel.get("orgnr")),
                    "navn": sel.get("navn", ""), "kommune": sel.get("kommune", ""),
                    "visited_on": today.isoformat(), "month_key": month_key(today),
                    "report": report, "outcome": outcome, "contact_email": contact,
                    "leg_km": leg_km, "co2_g": visit_co2_g,
                })
                rc = get_content("report")
                rmap = {"selger": email, "bedrift": sel.get("navn", ""),
                        "orgnr": sel.get("orgnr", ""),
                        "adresse": f"{sel.get('adresse','')}, {sel.get('postnr','')} {sel.get('poststed','')}",
                        "dato": today.isoformat(), "utfall": outcome, "rapport": report,
                        "km": leg_km, "co2": co2_kg(leg_km, _settings)}
                sb_insert("email_queue", {
                    "kind": "report", "to_email": BACKOFFICE_EMAIL,
                    "subject": apply_vars(rc["subject"], rmap),
                    "body": apply_vars(rc["body"], rmap),
                    "send_after": today.isoformat(),
                    "payload": {"orgnr": str(sel.get("orgnr")), "user": email},
                })
                if send_thanks and contact:
                    tc = get_content("thankyou")
                    sb_insert("email_queue", {
                        "kind": "thankyou", "to_email": contact,
                        "subject": fill_thanks(tc["subject"], _settings, email),
                        "body": fill_thanks(tc["body"], _settings, email),
                        "send_after": add_business_days(today, 2).isoformat(),
                        "payload": {"navn": sel.get("navn", ""), "selger": email},
                    })
                    if not str(_settings.get("phone") or "").strip():
                        st.info("Tips: legg inn telefonnummeret ditt under Innstillinger – "
                                "det settes automatisk inn i takk-e-posten.")
                st.success("✅ Besøk registrert. Rapport går til backoffice, og takk-e-post er "
                           "lagt i kø. Bedriften vises ikke i forslag igjen denne måneden.")
                st.session_state.pop("selected_company", None)
            except Exception as exc:
                st.error(f"Kunne ikke lagre: {exc}")
    else:
        st.info("Søk opp en bedrift for å registrere et besøk – eller start fra «Min dag».")


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
        cls = _cls.get(r.get("outcome", ""), "bnc-badge")
        st.markdown(
            f"<div class='bnc-stop'><b>{r.get('navn','')}</b> "
            f"<span class='bnc-badge {cls}'>{r.get('outcome','')}</span><br>"
            f"<small>{r.get('visited_on','')} · {r.get('kommune','')}</small><br>"
            f"{r.get('report','') or '<i>ingen rapporttekst</i>'}</div>",
            unsafe_allow_html=True)
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
                                            "ladbar hybrid ~60, bensin ~165, diesel ~170. Sett faktoren "
                                            "konsernet rapporterer med.")
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
        q = urllib.parse.urlencode({"u": u["email"], "k": tok})
        return f"{APP_BASE_URL}/?{q}" if APP_BASE_URL else f"?{q}"

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
