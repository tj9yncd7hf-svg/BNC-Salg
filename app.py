"""BNC Selger – besøksrute-app for selger på veien.

Kim setter startposisjon + timer på veien + snitt-tid pr. besøk, og appen foreslår
en kjørerute med bedrifter (AS, utvalgte bransjer, ikke konkurs) fra Brønnøysund.
Besøk hukes av (utelates resten av måneden), rapport skrives og sendes til
backoffice, og «takk for besøket» + ukesoppsummering går via e-postkøen.
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

APP_DIR = Path(__file__).parent
DATA_FILE = APP_DIR / "data" / "companies.json"
LOGO = APP_DIR / "assets" / "bnc_logo_navy.webp"
BACKOFFICE_EMAIL = os.getenv("BACKOFFICE_EMAIL", "post@bncnordic.no")

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SECRET_KEY", "")
if not SUPABASE_URL:
    try:
        SUPABASE_URL = str(st.secrets.get("SUPABASE_URL", "")).rstrip("/")
        SUPABASE_KEY = str(st.secrets.get("SUPABASE_SECRET_KEY", ""))
    except Exception:
        pass

# Vennlige bransjenavn (kategori-feltet i companies.json). Rekkefølge = visning.
KATEGORIER = [
    "Verksted", "Bilpleie", "Bilforhandler", "Delegrossist",
    "Bildeler & rekvisita (butikk)", "Bilgrossist (import/engros)",
    "Bensinstasjon / drivstoff", "Bilutleie", "MC-verksted", "MC-forhandler",
]

# Innebygd standardmal for «takk for praten» (Kim kan lage/endre egne maler i appen).
DEFAULT_THANKS_SUBJECT = "Takk for praten – BNC Nordic"
# Plassholdere {navn}, {telefon}, {epost} fylles med selgerens profil ved utsending.
DEFAULT_THANKS_BODY = (
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
)


def fill_template(text, settings, login_email):
    """Bytt plassholdere {navn}/{telefon}/{epost} med selgerens profil."""
    return (str(text or "")
            .replace("{navn}", str(settings.get("name") or "").strip() or "Kim Werner Strand")
            .replace("{telefon}", str(settings.get("phone") or "").strip())
            .replace("{epost}", str(login_email or "").strip()))

st.set_page_config(page_title="BNC Selger", page_icon="🚗", layout="centered")

st.markdown("""
<style>
  :root{ --navy:#1b2a4a; --accent:#1b2a4a; }
  .stApp{ background:#f5f6f8; }
  h1,h2,h3{ color:#1b2a4a; }
  div.stButton>button[kind="primary"]{ background:#1b2a4a; border-color:#1b2a4a; }
  .bnc-stop{ background:#fff;border:1px solid #e3e6ec;border-radius:12px;padding:12px 14px;margin-bottom:8px }
  .bnc-stop b{ color:#1b2a4a }
  .bnc-badge{ display:inline-block;background:#eef1f6;color:#1b2a4a;border-radius:999px;
    padding:2px 9px;font-size:12px;font-weight:600;margin-right:6px }
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
                      params={"on_conflict": on_conflict},
                      json=[row], timeout=20)
    r.raise_for_status()


def sb_update(table, row_id, row):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}",
                       headers={**_sb_headers(), "Prefer": "return=minimal"},
                       params={"id": f"eq.{row_id}"}, json=row, timeout=20)
    r.raise_for_status()


def sb_delete(table, row_id):
    r = requests.delete(f"{SUPABASE_URL}/rest/v1/{table}",
                        headers=_sb_headers(), params={"id": f"eq.{row_id}"}, timeout=20)
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
    """Geokode et fritekst-startpunkt via Kartverket. Returnerer (lat, lon, label)."""
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


def gmaps_route_url(start, stops):
    """Google Maps-rute: start → alle stopp i rekkefølge (åpnes i app på mobil)."""
    if not stops:
        return None
    origin = f"{start['lat']},{start['lon']}"
    dest = f"{stops[-1]['lat']},{stops[-1]['lon']}"
    mids = stops[:-1][:9]  # Google tillater opptil 9 mellomstopp i denne URL-en
    url = (f"https://www.google.com/maps/dir/?api=1&origin={origin}"
           f"&destination={dest}&travelmode=driving")
    if mids:
        url += "&waypoints=" + "%7C".join(f"{p['lat']},{p['lon']}" for p in mids)
    return url


def gmaps_dest_url(p):
    """Google Maps-navigasjon til ett stopp."""
    return (f"https://www.google.com/maps/dir/?api=1"
            f"&destination={p['lat']},{p['lon']}&travelmode=driving")


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


def load_templates(email):
    try:
        return sb_get("email_templates", "*", {"user_email": f"eq.{email}", "order": "name.asc"})
    except Exception:
        return []


def template_options(email):
    """Valgbare maler for takk-e-posten. Faller tilbake til innebygd standard."""
    tmpls = load_templates(email)
    std = {"id": None, "name": "Standard (takk for praten)",
           "subject": DEFAULT_THANKS_SUBJECT, "body": DEFAULT_THANKS_BODY,
           "is_default": not any(t.get("is_default") for t in tmpls)}
    opts = list(tmpls) if tmpls else [std]
    return opts


# ------------------------------------------------------------------- UI -------
if LOGO.exists():
    st.image(str(LOGO), width=160)
st.title("Selger på veien")

# Enkel delt passord-gate (APP_PASSWORD i Streamlit Secrets). Appen HUSKER
# innloggingen i nettadressen (?t=…&u=…), slik at Kim kan legge lenken til på
# Hjem-skjerm og slippe å taste passord + e-post hver gang. Er APP_PASSWORD tom,
# er appen åpen.
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
try:
    if not APP_PASSWORD:
        APP_PASSWORD = str(st.secrets.get("APP_PASSWORD", ""))
except Exception:
    pass
LOGIN_TOKEN = (hashlib.sha256(("bncselger::" + APP_PASSWORD).encode()).hexdigest()[:20]
               if APP_PASSWORD else "")

_qp = st.query_params
# Auto-innlogging fra lenken.
if APP_PASSWORD and not st.session_state.get("_authed") and _qp.get("t") == LOGIN_TOKEN:
    st.session_state["_authed"] = True
# Forhåndsutfyll e-post fra lenken.
if _qp.get("u") and not st.session_state.get("user_email"):
    st.session_state["user_email"] = _qp.get("u")

if APP_PASSWORD and not st.session_state.get("_authed"):
    _pw = st.text_input("Passord", type="password")
    if st.button("Logg inn", type="primary"):
        if _pw == APP_PASSWORD:
            st.session_state["_authed"] = True
            st.query_params["t"] = LOGIN_TOKEN
            st.rerun()
        else:
            st.error("Feil passord.")
    st.stop()

if not sb_configured():
    st.error("Supabase er ikke konfigurert (SUPABASE_URL / SUPABASE_SECRET_KEY mangler).")

email = st.text_input("Din e-post (selger)", key="user_email",
                      placeholder="kim@bncnordic.no").strip().lower()
if not email:
    st.info("Skriv inn e-posten din for å komme i gang.")
    st.stop()

# Lagre innlogging i lenken (slik at Hjem-skjerm-snarveien husker Kim).
if _qp.get("u") != email:
    st.query_params["u"] = email
if LOGIN_TOKEN and _qp.get("t") != LOGIN_TOKEN:
    st.query_params["t"] = LOGIN_TOKEN

_settings = load_settings(email)
nav = st.radio("", ["📍 Min dag", "📝 Besøk", "✉️ Maler", "📚 Historikk", "⚙️ Innstillinger"],
               horizontal=True, label_visibility="collapsed", key="nav")

companies = load_companies()
if not companies:
    st.warning("Bedriftslista (data/companies.json) mangler eller er tom.")
    st.stop()


# ============================================================== MIN DAG =======
if nav == "📍 Min dag":
    st.subheader("Planlegg dagen")
    _def_lat = _settings.get("start_lat")
    _def_lon = _settings.get("start_lon")
    _def_label = _settings.get("start_label") or ""
    _kat_present = [k for k in KATEGORIER if any(c.get("kategori") == k for c in companies)]

    with st.form("plan"):
        start_txt = st.text_input("Startposisjon (adresse/sted)", value=_def_label,
                                  placeholder="F.eks. Storgata 1, Oslo")
        c1, c2 = st.columns(2)
        hours = c1.number_input("Timer på veien", 1.0, 12.0,
                                float(_settings.get("hours_per_day") or 7), 0.5)
        visit_min = c2.number_input("Snitt pr. besøk (min)", 10, 180,
                                    int(_settings.get("avg_visit_minutes") or 60), 5)
        velg_alle = st.checkbox("Velg alle bransjer")
        valgte_cat = st.multiselect(
            "Bransjer", _kat_present, default=[],
            help="La stå tom og huk av «Velg alle bransjer», eller velg kun de du vil besøke.")
        maxkm = st.slider("Maks avstand mellom besøk (km)", 10, 300, 120, 10)
        planlegg = st.form_submit_button("🧭 Foreslå rute", type="primary", use_container_width=True)

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
                cands = [c for c in companies
                         if c.get("kategori") in valgte and str(c.get("orgnr")) not in visited]
                res = routing.plan_route(lat, lon, cands, hours,
                                         visit_minutes=visit_min, max_leg_km=maxkm)
                st.session_state["route"] = res["route"]
                st.session_state["route_start"] = {"lat": lat, "lon": lon, "label": label}
                st.success(f"Foreslått rute: **{len(res['route'])} besøk** · "
                           f"{res['total_km']} km · ca. {res['total_min']} min "
                           f"(av {res['n_candidates']} aktuelle bedrifter).")

    route = st.session_state.get("route") or []
    start = st.session_state.get("route_start")
    if route and start:
        # Hele ruta i Google Maps (åpnes i Maps-appen på mobil → rett inn på GPS).
        g_url = gmaps_route_url(start, route)
        if g_url:
            st.link_button("🧭 Åpne hele ruta i Google Maps", g_url, use_container_width=True)

        try:
            import pydeck as pdk
            pts = [{"lat": start["lat"], "lon": start["lon"], "navn": "START",
                    "label": "S", "color": [0, 150, 80]}]
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

        st.markdown("#### Dagens besøk")
        for s in route:
            st.markdown(
                f"<div class='bnc-stop'><b>{s['stop']}. {s.get('navn','')}</b><br>"
                f"<span class='bnc-badge'>{s.get('kategori', s.get('bransje_kode',''))}</span>"
                f"{s.get('adresse','')}, {s.get('postnr','')} {s.get('poststed','')}<br>"
                f"<small>+{s.get('leg_km')} km / {s.get('leg_min')} min · kum. {s.get('cum_min')} min</small></div>",
                unsafe_allow_html=True)
            b1, b2 = st.columns(2)
            if b1.button(f"📝 Registrer besøk", key=f"reg_{s.get('orgnr')}"):
                st.session_state["selected_company"] = s
                st.session_state["nav"] = "📝 Besøk"
                st.rerun()
            b2.link_button("🧭 Google Maps", gmaps_dest_url(s))


# ================================================================ BESØK =======
if nav == "📝 Besøk":
    st.subheader("Registrer besøk")
    sel = st.session_state.get("selected_company")
    if not sel:
        # Velg bedrift manuelt (søk)
        q = st.text_input("Søk bedrift (navn)", placeholder="Skriv navn …")
        hits = [c for c in companies if q and q.lower() in str(c.get("navn", "")).lower()][:20]
        if hits:
            lbl = st.selectbox("Treff", [f"{c['navn']} · {c.get('poststed','')}" for c in hits])
            sel = hits[[f"{c['navn']} · {c.get('poststed','')}" for c in hits].index(lbl)]
    if sel:
        st.markdown(f"**{sel.get('navn','')}** · {sel.get('adresse','')}, "
                    f"{sel.get('postnr','')} {sel.get('poststed','')}")
        _opts = template_options(email)
        _onames = [t["name"] for t in _opts]
        _def_idx = next((i for i, t in enumerate(_opts) if t.get("is_default")), 0)
        with st.form("besok"):
            outcome = st.selectbox("Utfall", ["Interessert", "Vil tenke", "Ikke interessert",
                                              "Ikke til stede", "Solgt", "Annet"])
            report = st.text_area("Besøksrapport", placeholder="Hva skjedde på besøket …", height=140)
            contact = st.text_input("Send «takk for praten» til (kundens e-post)",
                                    value=sel.get("epost", ""))
            send_thanks = st.checkbox("📧 Send «takk for praten» (går ut 2 virkedager etter)",
                                      value=bool(sel.get("epost")))
            tmpl_name = st.selectbox("Mal for takk-e-posten", _onames, index=_def_idx)
            lagre = st.form_submit_button("💾 Lagre besøk + send rapport", type="primary",
                                          use_container_width=True)
        if lagre:
            try:
                today = date.today()
                sb_insert("visits", {
                    "user_email": email, "orgnr": str(sel.get("orgnr")),
                    "navn": sel.get("navn", ""), "kommune": sel.get("kommune", ""),
                    "visited_on": today.isoformat(), "month_key": month_key(today),
                    "report": report, "outcome": outcome, "contact_email": contact,
                })
                # Rapport til backoffice (sendes av e-postjobben, send_after=i dag).
                sb_insert("email_queue", {
                    "kind": "report", "to_email": BACKOFFICE_EMAIL,
                    "subject": f"Besøksrapport: {sel.get('navn','')} ({outcome})",
                    "body": (f"Selger: {email}\nBedrift: {sel.get('navn','')} (org {sel.get('orgnr')})\n"
                             f"Adresse: {sel.get('adresse','')}, {sel.get('postnr','')} {sel.get('poststed','')}\n"
                             f"Dato: {today.isoformat()}\nUtfall: {outcome}\n\nRapport:\n{report}\n"),
                    "send_after": today.isoformat(),
                    "payload": {"orgnr": str(sel.get("orgnr")), "user": email},
                })
                # Takk-e-post til kunde, 2 virkedager etter – med valgt mal.
                if send_thanks and contact:
                    _ch = next((t for t in _opts if t["name"] == tmpl_name), _opts[0])
                    sb_insert("email_queue", {
                        "kind": "thankyou", "to_email": contact,
                        "subject": fill_template(_ch.get("subject") or DEFAULT_THANKS_SUBJECT, _settings, email),
                        "body": fill_template(_ch.get("body") or DEFAULT_THANKS_BODY, _settings, email),
                        "send_after": add_business_days(today, 2).isoformat(),
                        "payload": {"navn": sel.get("navn", ""), "selger": email, "mal": tmpl_name},
                    })
                    if not str(_settings.get("phone") or "").strip():
                        st.info("Tips: legg inn telefonnummeret ditt under ⚙️ Innstillinger – "
                                "det settes automatisk inn i takk-e-posten.")
                st.success("✅ Besøk registrert. Rapport går til backoffice, og takk-e-post "
                           "er lagt i kø. Bedriften vises ikke i forslag igjen denne måneden.")
                st.session_state.pop("selected_company", None)
            except Exception as exc:
                st.error(f"Kunne ikke lagre: {exc}")


# ================================================================ MALER ========
if nav == "✉️ Maler":
    st.subheader("E-postmaler")
    st.caption("Maler for «takk for praten»-e-posten. Sett én som standard – den "
               "foreslås når du registrerer et besøk. Du kan lage flere og endre fritt. "
               "Bruk **{navn}**, **{telefon}** og **{epost}** i teksten – de fylles "
               "automatisk fra profilen din (⚙️ Innstillinger) ved utsending.")
    tmpls = load_templates(email)
    if not tmpls:
        st.info("Ingen egne maler ennå – da brukes den innebygde standardmalen. "
                "Legg den inn her for å redigere den, eller lag en ny under.")
        if st.button("➕ Legg inn standardmalen (så du kan redigere den)"):
            try:
                sb_insert("email_templates", {"user_email": email, "name": "Takk for praten",
                                              "subject": DEFAULT_THANKS_SUBJECT,
                                              "body": DEFAULT_THANKS_BODY, "is_default": True})
                st.rerun()
            except Exception as exc:
                st.error(f"Kunne ikke lagre: {exc}")
        with st.expander("Se den innebygde standardmalen"):
            st.caption(f"Emne: {DEFAULT_THANKS_SUBJECT}")
            st.text(DEFAULT_THANKS_BODY)

    for t in tmpls:
        with st.expander(("⭐ " if t.get("is_default") else "✉️ ") + t["name"]):
            with st.form(f"tmpl_{t['id']}"):
                nm = st.text_input("Navn", value=t.get("name", ""))
                subj = st.text_input("Emne", value=t.get("subject", ""))
                body = st.text_area("Tekst", value=t.get("body", ""), height=280)
                b1, b2, b3 = st.columns(3)
                do_save = b1.form_submit_button("💾 Lagre")
                do_def = b2.form_submit_button("⭐ Sett standard")
                do_del = b3.form_submit_button("🗑️ Slett")
            if do_save:
                try:
                    sb_update("email_templates", t["id"], {
                        "name": nm, "subject": subj, "body": body,
                        "updated_at": datetime.now(timezone.utc).isoformat()})
                    st.success("Lagret."); st.rerun()
                except Exception as exc:
                    st.error(f"Kunne ikke lagre: {exc}")
            if do_def:
                try:
                    for o in tmpls:
                        if o["id"] != t["id"] and o.get("is_default"):
                            sb_update("email_templates", o["id"], {"is_default": False})
                    sb_update("email_templates", t["id"], {"is_default": True})
                    st.rerun()
                except Exception as exc:
                    st.error(f"Kunne ikke sette standard: {exc}")
            if do_del:
                try:
                    sb_delete("email_templates", t["id"]); st.rerun()
                except Exception as exc:
                    st.error(f"Kunne ikke slette: {exc}")

    st.markdown("**Ny mal**")
    with st.form("ny_mal"):
        nn = st.text_input("Navn", placeholder="F.eks. «Takk – kort»")
        ns = st.text_input("Emne", value=DEFAULT_THANKS_SUBJECT)
        nb = st.text_area("Tekst", value=DEFAULT_THANKS_BODY, height=220)
        if st.form_submit_button("➕ Opprett mal", type="primary"):
            if nn.strip():
                try:
                    sb_insert("email_templates", {"user_email": email, "name": nn.strip(),
                                                  "subject": ns, "body": nb,
                                                  "is_default": not tmpls})
                    st.success("Mal opprettet."); st.rerun()
                except Exception as exc:
                    st.error(f"Kunne ikke opprette: {exc}")
            else:
                st.error("Gi malen et navn.")


# ============================================================= HISTORIKK ======
if nav == "📚 Historikk":
    st.subheader("Mine besøk denne måneden")
    try:
        rows = sb_get("visits", "*",
                      {"user_email": f"eq.{email}", "month_key": f"eq.{month_key()}",
                       "order": "visited_on.desc"})
    except Exception as exc:
        rows = []
        st.error(f"Kunne ikke hente: {exc}")
    st.caption(f"{len(rows)} besøk i {month_key()}.")
    for r in rows:
        st.markdown(
            f"<div class='bnc-stop'><b>{r.get('navn','')}</b> "
            f"<span class='bnc-badge'>{r.get('outcome','')}</span><br>"
            f"<small>{r.get('visited_on','')} · {r.get('kommune','')}</small><br>"
            f"{r.get('report','') or '<i>ingen rapporttekst</i>'}</div>",
            unsafe_allow_html=True)


# =========================================================== INNSTILLINGER ====
if nav == "⚙️ Innstillinger":
    st.subheader("Innstillinger")
    with st.form("settings"):
        name = st.text_input("Navn", value=_settings.get("name", ""))
        phone = st.text_input("Telefon", value=_settings.get("phone", ""),
                              placeholder="+47 …",
                              help="Settes automatisk inn i «takk for praten»-e-posten ({telefon}).")
        summary_email = st.text_input("E-post for ukesoppsummering",
                                      value=_settings.get("summary_email", "") or email,
                                      help="Hit sendes fredagens ukesoppsummering (fra post@bncnordic.no). "
                                           "Kan være en annen adresse enn innloggings-e-posten.")
        start_label = st.text_input("Fast startposisjon (adresse)", value=_settings.get("start_label", ""),
                                    placeholder="Valgfritt – kan overstyres på «Min dag» hver dag")
        c1, c2 = st.columns(2)
        hours = c1.number_input("Timer pr. dag", 1.0, 12.0, float(_settings.get("hours_per_day") or 7), 0.5)
        visit_min = c2.number_input("Snitt pr. besøk (min)", 10, 180, int(_settings.get("avg_visit_minutes") or 60), 5)
        speed = st.number_input("Snittfart bil (km/t)", 30, 110, int(_settings.get("speed_kmh") or 70), 5)
        lagre = st.form_submit_button("💾 Lagre innstillinger", type="primary", use_container_width=True)
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
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }, "email")
            st.success("✅ Lagret." + (f" Startposisjon: {label}." if lat else
                                       " (Fant ikke adressen – startposisjon ikke satt.)"))
        except Exception as exc:
            st.error(f"Kunne ikke lagre: {exc}")

    # ---- Testutsending: kontroller e-postene før lansering --------------------
    st.divider()
    with st.expander("🧪 Send testmail (kontroll før lansering)"):
        st.caption("Legg en test-e-post i kø og kjør **E-postjobb** i GitHub Actions "
                   "(eller vent inntil 1 time) for å motta den. Forhåndsvisningen under "
                   "viser nøyaktig innholdet.")
        tst_to = st.text_input("Send test til", value=email, key="test_to")
        tst_type = st.radio("Type", ["Takk for praten", "Ukesoppsummering", "Besøksrapport"],
                            key="test_type")

        if tst_type == "Takk for praten":
            _opts = template_options(email)
            _tn = st.selectbox("Mal", [t["name"] for t in _opts], key="test_tmpl")
            _ch = next((t for t in _opts if t["name"] == _tn), _opts[0])
            tsubj = fill_template(_ch.get("subject") or DEFAULT_THANKS_SUBJECT, _settings, email)
            tbody = fill_template(_ch.get("body") or DEFAULT_THANKS_BODY, _settings, email)
            tkind = "thankyou"
        elif tst_type == "Ukesoppsummering":
            tsubj = "Ukesoppsummering (TEST): 2 besøk"
            tbody = ("Hei,\n\nHer er oppsummeringen av uka (eksempel):\n\n"
                     "Totalt 2 besøk.\nUtfall: Interessert: 1, Vil tenke: 1\n\n"
                     "Besøk:\n- 2026-10-12  Eksempel Bil AS (OSLO) – Interessert\n"
                     "- 2026-10-13  Demo Verksted AS (BÆRUM) – Vil tenke\n\n"
                     "Stå på!\nBNC Nordic")
            tkind = "summary"
        else:
            tsubj = "Besøksrapport (TEST): Eksempel Bil AS (Interessert)"
            tbody = (f"Selger: {email}\nBedrift: Eksempel Bil AS (org 999999999)\n"
                     "Adresse: Eksempelveien 1, 0001 OSLO\n"
                     f"Dato: {date.today().isoformat()}\nUtfall: Interessert\n\n"
                     "Rapport:\nDette er en testrapport for å kontrollere formatet.\n")
            tkind = "report"

        st.caption("Forhåndsvisning – emne:")
        st.code(tsubj, language=None)
        st.caption("Forhåndsvisning – tekst:")
        st.text(tbody)

        if st.button("📨 Legg testmail i kø"):
            try:
                sb_insert("email_queue", {
                    "kind": tkind, "to_email": tst_to.strip(),
                    "subject": "[TEST] " + tsubj, "body": tbody,
                    "send_after": date.today().isoformat(),
                    "payload": {"test": True, "selger": email},
                })
                st.success(f"✅ Testmail lagt i kø til {tst_to.strip()}. Gå til GitHub → "
                           "Actions → «E-postjobb» → Run workflow for å sende den nå.")
            except Exception as exc:
                st.error(f"Kunne ikke legge i kø: {exc}")

    # ---- Pålogging / brukere --------------------------------------------------
    st.divider()
    st.markdown("#### Pålogging")
    st.caption("Appen husker innloggingen i nettadressen. Når du er logget inn: "
               "**legg appen til på Hjem-skjerm på mobilen** (del-knappen → «Legg til "
               "på Hjem-skjerm») – da åpner snarveien rett inn, uten passord og e-post. "
               "Hver selger logger inn med sin egen e-post og får sin egen profil/historikk.")
    if st.button("🚪 Logg ut (fjern minnet i lenken)"):
        st.query_params.clear()
        for k in ("_authed", "user_email", "route", "route_start", "selected_company"):
            st.session_state.pop(k, None)
        st.rerun()
