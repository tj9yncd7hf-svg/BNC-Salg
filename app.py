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
DATA_FILES = {"NO": APP_DIR / "data" / "companies.json",
              "SE": APP_DIR / "data" / "companies_se.json"}
DATA_FILE = DATA_FILES["NO"]  # bakoverkompatibilitet
LAND_NAVN = {"NO": "Norge", "SE": "Sverige"}
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


# ----------------------------------------------------------- språk (NO/SV) ----
# Appen er skrevet på norsk. t() oversetter synlige strenger til svensk når selger
# har valgt det i Innstillinger. Ukjente strenger faller trygt tilbake til norsk.
def current_lang():
    v = st.session_state.get("lang")
    if v in ("no", "sv"):
        return v
    try:
        q = st.query_params.get("lang")
    except Exception:
        q = None
    return q if q in ("no", "sv") else "no"


def t(no):
    if current_lang() == "sv":
        return _SV.get(no, no)
    return no


class _Loc:
    """Språkavhengig sekvens (ukedager/måneder): samme indeksering som før, men gir
    svensk tekst når språket er svensk."""
    def __init__(self, no, sv):
        self._no, self._sv = no, sv

    def _cur(self):
        return self._sv if current_lang() == "sv" else self._no

    def __getitem__(self, i):
        return self._cur()[i]

    def __len__(self):
        return len(self._no)

    def __iter__(self):
        return iter(self._cur())


_SV = {
    # innlogging / landing
    "Selger på veien": "Säljare på vägen", "Passord": "Lösenord", "Logg inn": "Logga in",
    "Feil passord.": "Fel lösenord.",
    "Supabase er ikke konfigurert (SUPABASE_URL / SUPABASE_SECRET_KEY mangler).":
        "Supabase är inte konfigurerat (SUPABASE_URL / SUPABASE_SECRET_KEY saknas).",
    "Din e-post": "Din e-post", "Fortsett": "Fortsätt",
    "Selgere kommer vanligvis inn via sin personlige lenke og slipper dette.":
        "Säljare kommer vanligtvis in via sin personliga länk och slipper detta.",
    # meny / navigasjon / sidebar
    "Meny": "Meny", "Logg ut": "Logga ut",
    "Min dag": "Min dag", "Drop-in": "Drop-in", "Planlegging": "Planering",
    "Historikk": "Historik", "Innstillinger": "Inställningar", "Brukere": "Användare",
    "E-postinnhold": "E-postinnehåll", "Land": "Land",
    "Klimaavtrykk:": "Klimatavtryck:", "denne mnd": "denna mån", "kg i år": "kg i år",
    "Data for": "Data för",
    "er ikke lagt inn ennå – viser Norge.": "är inte inlagt ännu – visar Norge.",
    "Bedriftslista for": "Företagslistan för",
    "mangler eller er tom. Bytt søkeområde under Innstillinger.":
        "saknas eller är tom. Byt sökområde under Inställningar.",
    # besøksregistrering + utfall
    "Besøksresultat": "Besöksresultat", "Interessert": "Intresserad", "Vil tenke": "Vill tänka",
    "Ikke interessert": "Inte intresserad", "Ikke til stede": "Inte på plats",
    "Solgt": "Sålt", "Annet": "Annat",
    "Besøksrapport": "Besöksrapport", "Hva skjedde på besøket …": "Vad hände på besöket …",
    "Kundens e-post": "Kundens e-post",
    "Send «takk for praten» (går ut 2 virkedager etter)":
        "Skicka «tack för pratstunden» (skickas 2 arbetsdagar efter)",
    "Ikke besøk denne kunden igjen": "Besök inte denna kund igen",
    "Skjuler bedriften permanent fra framtidige forslag. Kan angres under Innstillinger → Ekskluderte bedrifter.":
        "Döljer företaget permanent från framtida förslag. Kan ångras under Inställningar → Exkluderade företag.",
    "Lagre besøk og send rapport": "Spara besök och skicka rapport",
    "Lagre endringer": "Spara ändringar",
    # kart
    "Ingen punkter å vise på kart.": "Inga punkter att visa på kartan.",
    # Min dag
    "Velg dag": "Välj dag", "besøk": "besök", " — ingen rute": " — ingen rutt",
    "I dag · ": "Idag · ", "dagens planlagte rute": "dagens planerade rutt",
    "planlagt rute": "planerad rutt", "i dag": "idag", "denne dagen": "denna dag",
    "Ingen planlagt rute": "Ingen planerad rutt",
    "Gå til **Planlegging** for å sette opp ruter – eller bruk **Drop-in** for et besøk på sparket.":
        "Gå till **Planering** för att sätta upp rutter – eller använd **Drop-in** för ett besök på direkten.",
    "Besøk i ruta": "Besök i rutten", "Samlet kjøring": "Samlad körning",
    "Beregnet tid": "Beräknad tid", "Klimaavtrykk": "Klimatavtryck",
    "Vis kart": "Visa karta", "Slett dagen": "Radera dagen",
    "Slett hele den planlagte ruta for denne dagen": "Radera hela den planerade rutten för denna dag",
    "Åpne hele ruta i Google Maps": "Öppna hela rutten i Google Maps",
    "#### Dagens besøk": "#### Dagens besök", "Angre besøk": "Ångra besök",
    "Registrer": "Registrera", "Naviger": "Navigera",
    "Fjern fra ruta (kan dukke opp igjen senere)": "Ta bort från rutten (kan dyka upp igen senare)",
    "Skjul": "Dölj",
    "Ikke aktuell – skjul permanent fra framtidige forslag":
        "Inte aktuell – dölj permanent från framtida förslag",
    # Drop-in
    "Søk opp en bedrift og registrer et besøk på sparket":
        "Sök upp ett företag och registrera ett besök på direkten",
    "Søk bedrift (navn eller org.nr)": "Sök företag (namn eller org.nr)",
    "Skriv navn eller org.nr …": "Skriv namn eller org.nr …", "Søk": "Sök",
    "Ingen treff. Prøv færre ord, eller søk på org./bedr.nr.":
        "Inga träffar. Prova färre ord, eller sök på org./företagsnr.",
    "Søk opp en bedrift for å registrere et drop-in-besøk.":
        "Sök upp ett företag för att registrera ett drop-in-besök.",
    # Planlegging
    "Planlegg besøk": "Planera besök", "Finn kundebesøk langs ruta di.": "Hitta kundbesök längs din rutt.",
    "#### Forslag til rute – godkjenn for å legge i Min dag":
        "#### Förslag till rutt – godkänn för att lägga i Min dag",
    "Obs om adressene: Vi bruker den adressen som er registrert i Brønnøysund for Norge, for Sverige: "
    "SCB (Statistiska centralbyrån). Den er ikke alltid den samme som der bedriften faktisk holder til. "
    "Sjekk «Nettside» eller «Google» før du drar hvis noe ser rart ut.":
        "Obs om adresserna: Vi använder adressen som är registrerad i Brønnøysund för Norge, för Sverige: "
        "SCB (Statistiska centralbyrån). Den är inte alltid densamma som där företaget faktiskt håller till. "
        "Kolla «Webbplats» eller «Google» innan du åker om något ser konstigt ut.",
    "Vis hele ruta i kartet": "Visa hela rutten på kartan",
    "Ingen forslag igjen for denne dagen.": "Inga förslag kvar för denna dag.",
    "Zoom til denne bedriften i kartet": "Zooma till detta företag på kartan",
    "Nettside": "Webbplats", "Åpne bedriftens nettside i ny fane": "Öppna företagets webbplats i ny flik",
    "Google": "Google", "Google-søk på bedriften i ny fane": "Google-sökning på företaget i ny flik",
    "Bytt": "Byt", "Finn en annen bedrift i nærheten i stedet": "Hitta ett annat företag i närheten istället",
    "Fant ingen annen aktuell bedrift i nærheten.": "Hittade inget annat aktuellt företag i närheten.",
    "Fjern dette forslaget": "Ta bort detta förslag",
    "Fjern forslag du ikke vil ha med «✕», eller bytt det ut med en annen bedrift i nærheten med «Bytt». "
    "Ingenting lagres før du godkjenner.":
        "Ta bort förslag du inte vill ha med «✕», eller byt ut det mot ett annat företag i närheten med "
        "«Byt». Ingenting sparas förrän du godkänner.",
    "Godkjenn og legg i Min dag": "Godkänn och lägg i Min dag", "Forkast": "Förkasta",
    "#### Når skal du reise?": "#### När ska du resa?",
    "Velg en startdato i kalenderen.": "Välj ett startdatum i kalendern.",
    "Velg sluttdato … (eller klikk samme dag igjen for én dag)":
        "Välj slutdatum … (eller klicka samma dag igen för en dag)",
    "Én dag valgt.": "En dag vald.", "dager valgt.": "dagar valda.",
    "Neste: område  →": "Nästa: område  →", "Endre": "Ändra", "hverdager": "vardagar",
    "#### Hvor skal du?": "#### Vart ska du?",
    "Startsted (base / min posisjon)": "Startplats (bas / min position)",
    "F.eks. Lillehammer": "T.ex. Göteborg", "Til · destinasjon (valgfritt)": "Till · destination (valfritt)",
    "F.eks. Oslo": "T.ex. Malmö",
    "Fyll ut for drop-in langs veien. Tomt = området rundt startstedet.":
        "Fyll i för drop-in längs vägen. Tomt = området runt startplatsen.",
    "Mellomstopp underveis (valgfritt):": "Mellanstopp längs vägen (valfritt):",
    "F.eks. Gjøvik": "T.ex. Jönköping", "Legg til mellomstopp": "Lägg till mellanstopp",
    "Tur-retur – hjem igjen til start samme dag": "Tur och retur – hem igen till start samma dag",
    "Med destinasjon foreslås drop-in langs veien (og hjemveien ved tur-retur). Uten destinasjon "
    "foreslås drop-in rundt startstedet.":
        "Med destination föreslås drop-in längs vägen (och hemvägen vid tur och retur). Utan destination "
        "föreslås drop-in runt startplatsen.",
    "Reisemåte": "Resesätt",
    "Fast base (jobbe flere dager samme sted)": "Fast bas (jobba flera dagar på samma plats)",
    "Rundreise (nytt sted hver dag)": "Rundresa (ny plats varje dag)",
    "Reisemål · der du jobber flere dager": "Resmål · där du jobbar flera dagar",
    "F.eks. Trondheim": "T.ex. Stockholm",
    "Dag 1 er reisedagen": "Dag 1 är resedagen",
    "med bedrifter på veien. De øvrige dagene foreslås bedrifter rundt":
        "med företag längs vägen. Övriga dagar föreslås företag runt",
    "Du slipper å skrive": "Du slipper skriva", "på hver dag.": "varje dag.",
    "Skriv reisemålet én gang – det gjelder alle dagene.":
        "Skriv resmålet en gång – det gäller alla dagarna.",
    "##### Reiserute – én etappe per dag": "##### Resrutt – en etapp per dag",
    "Skriv hvor du ender hver dag (overnatting). Neste dag starter der. Siste dag: sett sluttsted, f.eks. hjem.":
        "Skriv var du slutar varje dag (övernattning). Nästa dag startar där. Sista dagen: sätt slutplats, t.ex. hem.",
    "Dag": "Dag", "fra": "från", "(samme sted)": "(samma plats)",
    "Til / overnatting dag": "Till / övernattning dag",
    "La en dag stå tom hvis du bare vil jobbe området rundt startstedet.":
        "Lämna en dag tom om du bara vill jobba området runt startplatsen.",
    "←  Tilbake": "←  Tillbaka", "Neste: besøksvalg  →": "Nästa: besöksval  →",
    "#### Besøksvalg": "#### Besöksval", "Besøk per dag": "Besök per dag",
    "Så mange forslag spres langs ruta. Kim tar de han rekker på veien.":
        "Så många förslag sprids längs rutten. Säljaren tar dem hen hinner med på vägen.",
    "Minutter per besøk": "Minuter per besök", "Kun for anslått kjøretid.": "Endast för uppskattad körtid.",
    "Alle bransjer": "Alla branscher", "ingen valgt": "inget valt", "Bransjer:": "Branscher:",
    "Alle aktuelle bransjer": "Alla aktuella branscher", "…eller velg selv": "…eller välj själv",
    "Tips: Det svenske registeret (SCB) har ingen egen kode for bilpleie, så flere bilpleie-/biltvätt-bedrifter "
    "ligger under «Verksted». Velg også Verksted for å finne flere.":
        "Tips: Det svenska registret (SCB) har ingen egen kod för bilvård, så fler bilvårds-/biltvättföretag "
        "ligger under «Verkstad». Välj även Verkstad för att hitta fler.",
    "Flere valg": "Fler val", "Selskapsform": "Bolagsform",
    "AS + ENK": "AS + ENK", "Kun AS": "Endast AS", "Kun ENK": "Endast ENK",
    "Maks avstand mellom besøk (km, uten destinasjon)": "Max avstånd mellan besök (km, utan destination)",
    "Maks omvei fra ruta (km)": "Max omväg från rutten (km)",
    "Lag ruteforslag": "Skapa ruttförslag",
    "Velg minst én bransje – eller huk av «Alle aktuelle bransjer».":
        "Välj minst en bransch – eller bocka i «Alla aktuella branscher».",
    # bransjer / kategorier
    "Verksted": "Verkstad", "Bilpleie": "Bilvård", "Bilforhandler": "Bilhandlare",
    "Delegrossist": "Reservdelsgrossist", "Bildeler & rekvisita (butikk)": "Bildelar & tillbehör (butik)",
    "Bilgrossist (import/engros)": "Bilgrossist (import/grossist)",
    "Bensinstasjon / drivstoff": "Bensinstation / drivmedel", "MC-verksted": "MC-verkstad",
    "MC-forhandler": "MC-handlare", "MC-utstyr & klær": "MC-utrustning & kläder",
    "Ditec": "Ditec", "Ditec-senter": "Ditec-center",
    # kommende ruter
    "#### Kommende ruter": "#### Kommande rutter", "Tøm alle": "Töm alla",
    "Slett alle kommende planlagte ruter": "Radera alla kommande planerade rutter",
    "Ingen kommende ruter ennå – planlegg en rute over.": "Inga kommande rutter ännu – planera en rutt ovan.",
    "Hele turen": "Hela resan", "dager": "dagar", "forslag": "förslag",
    "Åpne ruta i Google Maps": "Öppna rutten i Google Maps",
    "Forslag til drop-in (valgfritt) — Bytt = annen bedrift · ✕ fjern fra ruta · Skjul = permanent:":
        "Förslag till drop-in (valfritt) — Byt = annat företag · ✕ ta bort från rutten · Dölj = permanent:",
    "kjøring": "körning", "ingen forslag ennå": "inga förslag ännu",
    "Bytt ut med en annen bedrift i nærheten": "Byt ut mot ett annat företag i närheten",
    "Fjern fra denne ruta (kan dukke opp igjen senere)": "Ta bort från denna rutt (kan dyka upp igen senare)",
    "Ikke aktuell – skjul permanent fra alle framtidige forslag":
        "Inte aktuell – dölj permanent från alla framtida förslag",
    "Slett ruten": "Radera rutten",
    # Historikk
    "Dine besøk og klimaavtrykk": "Dina besök och klimatavtryck", "Periode": "Period",
    "Denne måneden": "Denna månad", "Hittil i år": "Hittills i år", "Alt": "Allt",
    "Besøk": "Besök", "Kjørt (km)": "Kört (km)",
    "Kjøring summeres fra avstanden til hvert registrerte besøk (anslag for konsernrapportering). "
    "Besøk søkt opp manuelt teller 0 km.":
        "Körning summeras från avståndet till varje registrerat besök (uppskattning för koncernrapportering). "
        "Besök som sökts upp manuellt räknas som 0 km.",
    "_Ingen rapporttekst._": "_Ingen rapporttext._", "Takk-e-post til:": "Tack-e-post till:",
    "Rediger": "Redigera", "Angre / slett besøk": "Ångra / radera besök",
    "Ingen besøk i perioden": "Inga besök i perioden",
    # Innstillinger
    "Profil og standardverdier for planlegging": "Profil och standardvärden för planering",
    "**Søkeområde**": "**Sökområde**", "Språk": "Språk",
    "bedrifter i": "företag i",
    "Velg hvilket land bedriftssøk og ruteplanlegging bruker.":
        "Välj vilket land företagssök och ruttplanering använder.",
    "**Selgerprofil**": "**Säljarprofil**", "Navn": "Namn", "Telefon": "Telefon", "+47 …": "+46 …",
    "Settes automatisk inn i takk-e-posten ({telefon}).": "Sätts automatiskt in i tack-e-posten ({telefon}).",
    "E-post for ukesoppsummering": "E-post för veckosammanfattning",
    "Hit sendes fredagens ukesoppsummering.": "Hit skickas fredagens veckosammanfattning.",
    "**Standard for planlegging**": "**Standard för planering**",
    "Fast startposisjon (adresse)": "Fast startposition (adress)",
    "Valgfritt – kan overstyres på «Min dag»": "Valfritt – kan överstyras på «Min dag»",
    "Snitt pr. besøk (min)": "Snitt per besök (min)",
    "Standardverdi i planleggingen og for «Beregnet tid»-anslaget.":
        "Standardvärde i planeringen och för «Beräknad tid»-uppskattningen.",
    "Snittfart bil (km/t)": "Snitthastighet bil (km/h)", "Brukes til å anslå kjøretid.": "Används för att uppskatta körtid.",
    "Utslipp (g CO₂e/km)": "Utsläpp (g CO₂e/km)",
    "For konsernrapportering. Helelektrisk (f.eks. BMW iX1) = 0. Ladbar hybrid ~60, bensin ~165, diesel ~170.":
        "För koncernrapportering. Helelektrisk (t.ex. BMW iX1) = 0. Laddhybrid ~60, bensin ~165, diesel ~170.",
    "Lagre innstillinger": "Spara inställningar", "Lagret.": "Sparat.",
    "Ekskluderte bedrifter (skjult fra forslag)": "Exkluderade företag (dolda från förslag)",
    "Ingen bedrifter er ekskludert.": "Inga företag är exkluderade.", "Angre alle": "Ångra alla",
    "Fjern alle fra ekskludert-lista – de kan foreslås igjen":
        "Ta bort alla från exkluderingslistan – de kan föreslås igen",
    "Angre": "Ångra",
    "Du kommer inn via din personlige lenke. Legg appen til på Hjem-skjerm på mobilen (del-knappen → "
    "«Legg til på Hjem-skjerm») – da åpner snarveien rett inn.":
        "Du kommer in via din personliga länk. Lägg till appen på Hemskärmen på mobilen (dela-knappen → "
        "«Lägg till på Hemskärmen») – då öppnar genvägen direkt.",
    # Brukere (admin)
    "Opprett selgere og gi dem personlig innloggingslenke":
        "Skapa säljare och ge dem en personlig inloggningslänk",
    "Klimaavtrykk pr. selger": "Klimatavtryck per säljare",
    "Ingen registrerte besøk i perioden.": "Inga registrerade besök i perioden.",
    "Selgere": "Säljare", "Totalt CO₂e": "Totalt CO₂e", "(uten navn)": "(utan namn)",
    "**Ny bruker**": "**Ny användare**", "E-post": "E-post", "Opprett / ny lenke": "Skapa / ny länk",
    "Bruker klar – lenken vises til høyre.": "Användare klar – länken visas till höger.",
    "Skriv inn e-post.": "Skriv in e-post.",
    "Legg **APP_BASE_URL** i Streamlit Secrets (appens adresse) for komplette lenker.":
        "Lägg **APP_BASE_URL** i Streamlit Secrets (appens adress) för kompletta länkar.",
    "Ingen lenke ennå – trykk «Ny lenke».": "Ingen länk ännu – tryck «Ny länk».",
    "Ny lenke": "Ny länk", "Steng lenke": "Stäng länk",
    # E-postinnhold (admin)
    "Rediger de automatiske e-postene. Selgerne ser ikke denne siden.":
        "Redigera de automatiska e-postmeddelandena. Säljarna ser inte denna sida.",
    "Emne": "Ämne", "Tekst": "Text",
    "Variabler som fylles automatisk:": "Variabler som fylls i automatiskt:", "Lagre": "Spara",
    "Send testmail (kontroll før lansering)": "Skicka testmejl (kontroll före lansering)",
    "Legger en test i kø og sendes av E-postjobben (kjør den i GitHub Actions eller vent inntil 1 time).":
        "Lägger en test i kö som skickas av E-postjobbet (kör det i GitHub Actions eller vänta upp till 1 timme).",
    "Send test til": "Skicka test till", "Type": "Typ", "Forhåndsvisning:": "Förhandsvisning:",
    "Legg testmail i kø": "Lägg testmejl i kö",
    "Språk for malene": "Språk för mallarna",
    "Svenske e-poster sendes fra info@bncnordic.se via den svenske Brevo-kontoen.":
        "Svenska e-postmeddelanden skickas från info@bncnordic.se via det svenska Brevo-kontot.",
    "Takk for praten (til kunden, 2 virkedager etter besøk)":
        "Tack för pratstunden (till kunden, 2 arbetsdagar efter besök)",
    "Ukesoppsummering (til selger, fredager)": "Veckosammanfattning (till säljare, fredagar)",
    "Besøksrapport (til backoffice, samme dag)": "Besöksrapport (till backoffice, samma dag)",
    # Brukere – språkvalg
    "Språk (e-post + app)": "Språk (e-post + app)",
    "Selgerens språk styrer appen og språket/avsenderen på utgående e-post.":
        "Säljarens språk styr appen och språket/avsändaren på utgående e-post.",
    # feil / toast
    "Kunne ikke lagre:": "Kunde inte spara:", "Kunne ikke hente:": "Kunde inte hämta:",
    "Kunne ikke hente brukere:": "Kunde inte hämta användare:", "Kunne ikke legge i kø:": "Kunde inte lägga i kö:",
    "Besøk oppdatert.": "Besök uppdaterat.", "Dagens rute slettet.": "Dagens rutt raderad.",
    "Alle ekskluderte bedrifter er gjenåpnet.": "Alla exkluderade företag är återöppnade.",
    "Besøk slettet.": "Besök raderat.", "Besøk nullstilt.": "Besök återställt.",
    "Alle kommende ruter slettet.": "Alla kommande rutter raderade.",
    "Fant ingen annen passende bedrift i nærheten.": "Hittade inget annat passande företag i närheten.",
    "fjernet fra ruta.": "borttaget från rutten.", "skjult permanent.": "dolt permanent.",
    "Byttet til": "Bytte till", "dag(er) lagt i Min dag.": "dag(ar) lagd i Min dag.",
    "Besøk registrert – rapport sendt.": "Besök registrerat – rapport skickad.",
    " Kunden skjules fra framtidige forslag.": " Kunden döljs från framtida förslag.",
    " Bedriften er merket besøkt.": " Företaget är markerat som besökt.",
    "Lagret. Rapporten er sendt.": "Sparat. Rapporten är skickad.",
    "Lagret. Rapport sendt og «takk for praten» lagt i kø.":
        "Sparat. Rapport skickad och «tack för pratstunden» lagd i kö.",
    "Startposisjon:": "Startposition:",
    " (Fant ikke adressen – startposisjon ikke satt.)": " (Hittade inte adressen – startposition ej satt.)",
    "Testmail lagt i kø til": "Testmejl lagt i kö till",
    "Kjør «E-postjobb» i GitHub Actions for å sende nå.":
        "Kör «E-postjobb» i GitHub Actions för att skicka nu.",
    # kalender-ukedager (korte)
    "Man": "Mån", "Tir": "Tis", "Lør": "Lör", "Søn": "Sön",
}

UKEDAGER = _Loc(
    ["mandag", "tirsdag", "onsdag", "torsdag", "fredag", "lørdag", "søndag"],
    ["måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag"])

DEFAULT_CONTENT = {
    "thankyou": {
        "subject": "Takk for praten – BNC Nordic",
        "body": (
            "Hei,\n\n"
            "Takk for at du tok deg tid til en hyggelig prat da jeg var innom dere! \n\n"
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

# Svenske standardmaler. Samme variabel-navn ({navn}, {antall} …) som de norske,
# så utfyllingen fungerer likt. Admin kan overstyre dem pr. språk i E-postinnhold.
DEFAULT_CONTENT_SV = {
    "thankyou": {
        "subject": "Tack för pratstunden – BNC Nordic",
        "body": (
            "Hej,\n\n"
            "Tack för att du tog dig tid till en trevlig pratstund när jag var förbi hos er! \n\n"
            "Vi på BNC Nordic levererar produkter inom bilvård, fälgar, underrede och bromsar, "
            "och hjälper gärna till att hitta produkter som passar era behov.\n\n"
            "Har du frågor efter besöket, vill ha mer information eller en offert, är "
            "det bara att svara på detta mejl eller ringa mig på {telefon}.\n\n"
            "Trevligt att lära känna er – vi håller kontakten!\n\n"
            "{navn}\n"
            "Produktansvarig / Product manager\n\n"
            "BNC Nordic Distribution AS\n"
            "{telefon}\n"
            "{epost}\n"
            "Strömsveien 323B, 1081 Oslo"
        ),
    },
    "summary": {
        "subject": "Veckosammanfattning: {antall} besök ({periode})",
        "body": (
            "Hej,\n\n"
            "Här är sammanfattningen av veckan ({periode}):\n\n"
            "Totalt {antall} besök.\n"
            "Utfall: {utfall}\n"
            "Kört denna vecka: {total_km} km (~{total_co2} kg CO₂e)\n\n"
            "Besök:\n{besok_liste}\n\n"
            "Kör hårt!\nBNC Nordic"
        ),
    },
    "report": {
        "subject": "Besöksrapport: {bedrift} ({utfall})",
        "body": (
            "Säljare: {selger}\n"
            "Företag: {bedrift} (org {orgnr})\n"
            "Adress: {adresse}\n"
            "Datum: {dato}\n"
            "Utfall: {utfall}\n"
            "Körning till besöket: {km} km (~{co2} kg CO₂e)\n\n"
            "Rapport:\n{rapport}\n"
        ),
    },
}


def default_content(kind, lang="no"):
    base = DEFAULT_CONTENT[kind]
    if lang == "sv":
        sv = DEFAULT_CONTENT_SV.get(kind, {})
        return {"subject": sv.get("subject") or base["subject"],
                "body": sv.get("body") or base["body"],
                "vars": base["vars"], "label": base["label"]}
    return base


def co2_factor(settings):
    # NB: bruk eksplisitt None-sjekk – 0 (helelektrisk, f.eks. BMW iX1) er en gyldig verdi.
    v = settings.get("co2_g_per_km")
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except Exception:
        return 0.0


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


st.set_page_config(page_title="BNC Selger", page_icon="", layout="wide")

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
  /* Klikkbart bedriftsnavn i forslagslista: venstrestilt, fet, mørk – som ren tekst.
     Bred selektor så den treffer uansett Streamlit-versjon (kind- eller data-testid). */
  button[kind="tertiary"], button[data-testid="stBaseButton-tertiary"],
  [data-testid="stButton"] button[kind="tertiary"]{
    justify-content:flex-start !important; text-align:left !important;
    padding:0 !important; min-height:0 !important; border:none !important;
    color:#1c2534 !important; font-weight:700; font-size:15px; line-height:1.35; }
  button[kind="tertiary"] p, button[data-testid="stBaseButton-tertiary"] p,
  button[kind="tertiary"] div, button[data-testid="stBaseButton-tertiary"] div{
    text-align:left !important; width:100%; font-weight:700; margin:0; }
  button[kind="tertiary"]:hover, button[data-testid="stBaseButton-tertiary"]:hover{
    color:#1b2a4a !important; text-decoration:underline; }
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
  /* --- Mobil: topplinje + bunnmeny (skjult på desktop) --- */
  .st-key-bnc_topbar [data-testid="stHorizontalBlock"],
  .st-key-bnc_bnav [data-testid="stHorizontalBlock"]{ flex-wrap:nowrap !important; gap:4px !important }
  .st-key-bnc_topbar [data-testid="stColumn"], .st-key-bnc_topbar [data-testid="column"],
  .st-key-bnc_bnav [data-testid="stColumn"], .st-key-bnc_bnav [data-testid="column"]{
    min-width:0 !important; flex:1 1 0 !important; width:auto !important }
  @media (min-width: 901px){
    .st-key-bnc_topbar, .st-key-bnc_bnav{ display:none !important }
  }
  /* Skjul anker-lenke-ikon () på overskrifter overalt */
  [data-testid="stHeaderActionElements"]{ display:none !important }
  @media (max-width: 900px){
    section[data-testid="stSidebar"]{ display:none !important }
    header[data-testid="stHeader"]{ display:none !important }
    [data-testid="stSidebarCollapsedControl"], [data-testid="collapsedControl"]{ display:none !important }
    .block-container{ padding-top:58px !important;
      padding-bottom:calc(90px + env(safe-area-inset-bottom)) !important }
    .st-key-bnc_topbar{ position:fixed; top:0; left:0; right:0; z-index:1000; background:#1b2a4a;
      padding:4px 12px !important; box-shadow:0 1px 6px rgba(0,0,0,.25);
      display:flex; align-items:center }
    .st-key-bnc_topbar [data-testid="stImage"]{ margin:2px 0 !important }
    .st-key-bnc_topbar img{ max-height:34px !important; width:auto !important }
    .st-key-bnc_topbar button{ background:transparent !important;
      border:1px solid rgba(255,255,255,.4) !important; color:#fff !important;
      font-size:12.5px !important; font-weight:600 !important; padding:4px 0 !important;
      border-radius:8px !important; min-height:32px }
    .st-key-bnc_bnav{ position:fixed; bottom:0; left:0; right:0; z-index:1000; background:#fff;
      border-top:1px solid #e3e8f0; box-shadow:0 -1px 10px rgba(0,0,0,.08);
      padding:4px 4px calc(4px + env(safe-area-inset-bottom)) !important }
    /* fallback-knapper (uten option_menu) */
    .st-key-bnc_bnav button{ font-size:10.5px !important; font-weight:600 !important;
      padding:5px 0 !important; min-height:48px; border:none !important;
      background:transparent !important; color:#8892a4 !important; line-height:1.25 }
    .st-key-bnc_bnav button[kind="primary"]{ color:#1b2a4a !important; background:#eef2f8 !important;
      border-radius:10px !important }
  }
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


def enqueue_email(row):
    """Legg en e-post i køen. Tåler at «lang»-kolonnen ikke er lagt inn ennå –
    prøver da på nytt uten den, så besøksregistrering aldri feiler av den grunn."""
    try:
        sb_insert("email_queue", row)
    except Exception:
        if "lang" in row:
            row = {k: v for k, v in row.items() if k != "lang"}
            sb_insert("email_queue", row)
        else:
            raise


def upsert_settings(row):
    """Lagre selgerprofil. Tåler at «lang»-kolonnen ikke finnes ennå."""
    try:
        sb_upsert("salesperson_settings", row, "email")
    except Exception:
        if "lang" in row:
            sb_upsert("salesperson_settings", {k: v for k, v in row.items() if k != "lang"}, "email")
        else:
            raise


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


def sb_update(table, params, patch):
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{table}",
                       headers={**_sb_headers(), "Prefer": "return=minimal"},
                       params=params, json=patch, timeout=20)
    r.raise_for_status()


# ------------------------------------------------------------------- data -----
@st.cache_data(show_spinner=False)
def load_companies(land="NO"):
    f = DATA_FILES.get(land, DATA_FILES["NO"])
    if not f.exists():
        return []
    with open(f, encoding="utf-8") as fh:
        data = json.load(fh)
    return [c for c in data if c.get("lat") is not None and c.get("lon") is not None]


DITEC_CMS_TOKEN = _secret("DITEC_CMS_API_TOKEN")


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_ditec_centers():
    """Aktive Ditec-sentre (Sverige) direkte fra Payload-CMS (cms.ditec.se), med
    koordinater. Mellomlagret 6 t. Tom liste ved feil / manglende nøkkel."""
    if not DITEC_CMS_TOKEN:
        return []
    try:
        r = requests.get("https://cms.ditec.se/api/centers",
                         params={"limit": 300, "depth": 0},
                         headers={"Authorization": f"users API-Key {DITEC_CMS_TOKEN}",
                                  "Accept": "application/json"}, timeout=20)
        docs = (r.json() or {}).get("docs") or []
    except Exception:
        return []
    out = []
    for c in docs:
        if str(c.get("status", "")).lower() != "active":
            continue
        co = c.get("coordinates")
        if not (isinstance(co, list) and len(co) == 2 and co[0] and co[1]):
            continue
        a = c.get("address") or {}
        out.append({"name": (c.get("name") or "").strip(), "slug": c.get("slug") or "",
                    "lat": float(co[0]), "lon": float(co[1]),
                    "adresse": (a.get("street") or "").strip(),
                    "postnr": str(a.get("zip") or "").replace(" ", ""),
                    "poststed": a.get("city") or "", "mobil": c.get("phoneNumber") or "",
                    "epost": c.get("emailAddress") or ""})
    return out


def _merge_ditec(comps):
    """Merk nærmeste SCB-bedrift (innen 60 m) med «(Ditec)», eller legg til senteret
    som egen post hvis det ikke finnes i SCB. Koordinatene kommer fra CMS-et."""
    centers = fetch_ditec_centers()
    for ct in centers:
        best, bestd = None, 1e9
        for c in comps:
            if c.get("lat") is None:
                continue
            d = routing.haversine_km(ct["lat"], ct["lon"], c["lat"], c["lon"])
            if d < bestd:
                bestd, best = d, c
        if best and bestd <= 0.06:   # ≤60 m – samme sted, dette er driftsselskapet
            if "(Ditec)" not in best["navn"]:
                best["navn"] = best["navn"].rstrip() + " (Ditec)"
            best["ditec"] = True
        else:
            comps.append({"orgnr": "ditec-" + str(ct["slug"]), "parent_orgnr": "",
                          "navn": ct["name"], "orgform": "", "bransje_kode": "",
                          "bransje": "Ditec-senter", "kategori": "Bilpleie",
                          "adresse": ct["adresse"], "postnr": ct["postnr"],
                          "poststed": ct["poststed"], "kommune": "",
                          "lat": ct["lat"], "lon": ct["lon"], "geo_nivaa": "ditec-cms",
                          "epost": ct["epost"], "mobil": ct["mobil"], "nettside": "",
                          "ansatte": None, "aktivitet": "", "ditec": True})
    return comps


@st.cache_data(ttl=21600, show_spinner=False)
def companies_for(land):
    """Bedriftslista for landet, med live Ditec-sentre flettet inn for Sverige.
    Mellomlagret, så fletting/henting gjøres ikke på hver rerun."""
    comps = [dict(c) for c in load_companies(land)]
    if land == "SE":
        comps = _merge_ditec(comps)
    return comps


def current_land():
    return st.session_state.get("land", "NO")


def land_picker(widget_key, **kw):
    """Robust land-velger: skriver til den vanlige nøkkelen ss['land'] (ikke en
    widget-nøkkel), så valget huskes på tvers av steg/sider – en widget-verdi kan
    ellers nullstilles når widgeten ikke vises på alle steg."""
    opts = list(LAND_NAVN)
    cur = st.session_state.get("land", "NO")
    sel = st.radio(t("Land"), opts, index=opts.index(cur) if cur in opts else 0,
                   key=widget_key, horizontal=True,
                   format_func=lambda x: LAND_NAVN[x], **kw)
    st.session_state["land"] = sel
    return sel


def google_q(c):
    """Vanlig Google-søk på bedriftsnavnet."""
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(c.get("navn", ""))


def geocode(text, land=None):
    """Startpunkt: 1) adresse, 2) stedsnavn, 3) POI (Nominatim). Dekker «gate nr,
    sted», «Molde lufthavn» og dagligtale som «molde flyplass».
    For Sverige (land=SE) brukes Nominatim mot svenske adresser/steder."""
    t = (text or "").strip()
    if not t:
        return None, None, text
    if land is None:
        land = current_land()
    if land == "SE":
        ua = {"User-Agent": "BNC-Selger/1.0 (post@bncnordic.no)"}
        # 1) Photon (server-vennlig, tåler ø/ä/ö). Velg første svenske treff.
        try:
            r = requests.get("https://photon.komoot.io/api/",
                             params={"q": t, "limit": 5, "lang": "default"},
                             headers=ua, timeout=15)
            for f in (r.json().get("features") or []):
                p = f.get("properties") or {}
                if p.get("countrycode") == "SE":
                    co = f.get("geometry", {}).get("coordinates")
                    if co:
                        return float(co[1]), float(co[0]), p.get("name") or t
        except Exception:
            pass
        # 2) Nominatim som reserve (prøv også ø→ö / æ→ä for norsk skrivemåte).
        variants = [t]
        norm = (t.replace("ø", "ö").replace("Ø", "Ö").replace("æ", "ä").replace("Æ", "Ä"))
        if norm != t:
            variants.append(norm)
        for qq in variants:
            try:
                r = requests.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": qq, "format": "json", "limit": 1,
                                         "countrycodes": "se"}, headers=ua, timeout=15)
                js = r.json()
                if js:
                    return (float(js[0]["lat"]), float(js[0]["lon"]),
                            js[0].get("display_name", qq).split(",")[0] or qq)
            except Exception:
                pass
        return None, None, text
    def _adr():
        r = requests.get("https://ws.geonorge.no/adresser/v1/sok",
                         params={"sok": t, "treffPerSide": 1, "utkoordsys": 4258}, timeout=15)
        adr = (r.json().get("adresser") or [])
        if adr:
            rp = adr[0].get("representasjonspunkt") or {}
            if rp.get("lat"):
                return float(rp["lat"]), float(rp["lon"]), adr[0].get("adressetekst", t)
        return None

    def _sted():
        r = requests.get("https://api.kartverket.no/stedsnavn/v1/navn",
                         params={"sok": t, "utkoordsys": 4258, "treffPerSide": 1}, timeout=15)
        navn = (r.json().get("navn") or [])
        if navn:
            rp = navn[0].get("representasjonspunkt") or {}
            if rp.get("nord"):
                return float(rp["nord"]), float(rp["øst"]), navn[0].get("skrivemåte", t)
        return None

    def _nom():
        r = requests.get("https://nominatim.openstreetmap.org/search",
                         params={"q": t, "format": "json", "limit": 1, "countrycodes": "no"},
                         headers={"User-Agent": "BNC-Selger/1.0 (post@bncnordic.no)"}, timeout=15)
        js = r.json()
        if js:
            return float(js[0]["lat"]), float(js[0]["lon"]), t
        return None

    # Har teksten et husnummer? Da er det en adresse → adressesøk først.
    # Ellers er det et stedsnavn (f.eks. «Bergen») → stedsnavnsøk først,
    # slik at bynavn ikke treffer en tilfeldig gateadresse med samme navn.
    order = ([_adr, _sted, _nom] if any(ch.isdigit() for ch in t)
             else [_sted, _adr, _nom])
    for fn in order:
        try:
            res = fn()
            if res:
                return res
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


@st.cache_data(ttl=60, show_spinner=False)
def planned_rows(email):
    """Alle planlagte ruter for en bruker – mellomlagret, så måned/dato-klikk ikke henter
    fra databasen hver gang. Tømmes ved lagring/sletting."""
    try:
        return sb_get("planned_routes", "plan_date,companies,start,meta",
                      {"user_email": f"eq.{email}", "order": "plan_date.asc"})
    except Exception:
        return []


def save_plan(email, d, start, companies, meta):
    sb_upsert("planned_routes", {
        "user_email": email, "plan_date": d.isoformat(),
        "start": start, "companies": companies, "meta": meta,
        "created_at": datetime.now(timezone.utc).isoformat()}, "user_email,plan_date")
    planned_rows.clear()


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
    planned_rows.clear()


def load_settings(email):
    try:
        rows = sb_get("salesperson_settings", "*", {"email": f"eq.{email}"})
        return rows[0] if rows else {}
    except Exception:
        return {}


def get_content(kind, lang="no"):
    """Admin-redigert e-postinnhold for (kind, språk). Faller tilbake til innebygd
    standardtekst. Tåler at «lang»-kolonnen ikke er lagt inn ennå (leser da den
    gamle raden pr. kind for norsk)."""
    d = default_content(kind, lang)
    rows = None
    try:
        rows = sb_get("email_content", "subject,body", {"kind": f"eq.{kind}", "lang": f"eq.{lang}"})
    except Exception:
        rows = None
    if rows is None and lang == "no":
        try:
            rows = sb_get("email_content", "subject,body", {"kind": f"eq.{kind}"})
        except Exception:
            rows = []
    if rows:
        return {"subject": rows[0].get("subject") or d["subject"],
                "body": rows[0].get("body") or d["body"]}
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
        st.subheader(t("Selger på veien"))
        _pw = st.text_input(t("Passord"), type="password")
        if st.button(t("Logg inn"), type="primary", use_container_width=True):
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
        st.subheader(t("Selger på veien"))
        e = st.text_input(t("Din e-post"), placeholder=t("navn@bncnordic.no"))
        if st.button(t("Fortsett"), type="primary", use_container_width=True):
            if e.strip():
                st.session_state["user_email"] = e.strip().lower()
                st.rerun()
        st.caption(t("Selgere kommer vanligvis inn via sin personlige lenke og slipper dette."))
    st.stop()

if _qp.get("u") != email:
    st.query_params["u"] = email
if LOGIN_TOKEN and _qp.get("t") != LOGIN_TOKEN:
    st.query_params["t"] = LOGIN_TOKEN

_settings = load_settings(email)
# Visningsspråk: fra lagret innstilling / URL (?lang=) / default norsk. Settes i Innstillinger.
if "lang" not in st.session_state:
    _sl = (_settings.get("lang") or "").lower()
    if _sl not in ("no", "sv"):
        try:
            _sl = st.query_params.get("lang")
        except Exception:
            _sl = None
    st.session_state["lang"] = _sl if _sl in ("no", "sv") else "no"
is_admin = email in ADMIN_EMAILS
companies = companies_for(current_land())
if not companies and current_land() != "NO":
    # Valgt land mangler data ennå – fall tilbake til Norge så appen ikke låser seg.
    _miss = current_land()
    st.session_state["land"] = "NO"
    companies = companies_for("NO")
    st.session_state["_toast"] = (f"{t('Data for')} {LAND_NAVN.get(_miss, _miss)} "
                                   + t("er ikke lagt inn ennå – viser Norge."))


@st.cache_data(ttl=120, show_spinner=False)
def user_co2_totals(user):
    """Klimaavtrykk (kg) denne mnd / i år – mellomlagret 2 min for raskere sidevisning."""
    try:
        rows = sb_get("visits", "co2_g,visited_on,month_key", {"user_email": f"eq.{user}"})
    except Exception:
        return (0.0, 0.0)
    cm = sum(float(r.get("co2_g") or 0) for r in rows if r.get("month_key") == month_key()) / 1000.0
    cy = sum(float(r.get("co2_g") or 0) for r in rows
             if str(r.get("visited_on", "")).startswith(str(date.today().year))) / 1000.0
    return (round(cm, 1), round(cy, 1))


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
    _navlabels = [t(p) for p in pages]   # vist tekst (språk) – intern verdi holdes norsk
    if HAS_OPTION_MENU:
        _mi = pages.index(_goto) if _goto in pages else None
        _navsel = option_menu(
            None, _navlabels, icons=[PAGE_ICONS[p] for p in pages],
            default_index=0, key="nav_menu", manual_select=_mi,
            styles={
                "container": {"background-color": "#1b2a4a", "padding": "8px 4px"},
                "nav-link": {"color": "#c3ccdd", "font-size": "14.5px", "font-weight": "500",
                             "margin": "2px 0", "--hover-color": "#2a3e66"},
                "nav-link-selected": {"background-color": "#ffffff", "color": "#1b2a4a",
                                      "font-weight": "600"},
                "icon": {"color": "#9fb0cb", "font-size": "15px"},
            })
        nav = pages[_navlabels.index(_navsel)] if _navsel in _navlabels else pages[0]
    else:
        if _goto in pages:
            st.session_state["nav_menu"] = _goto
        nav = st.radio(t("Meny"), pages, label_visibility="collapsed", key="nav_menu", format_func=t)
    st.markdown("<div style='flex:1'></div>", unsafe_allow_html=True)
    st.markdown(
        f"<div style='color:#c3ccdd;font-size:12.5px;border-top:1px solid #33456b;"
        f"padding-top:10px;margin-top:8px'><b style='color:#fff'>"
        f"{_settings.get('name') or email}</b><br>{email}"
        f"{' · admin' if is_admin else ''}</div>", unsafe_allow_html=True)
    try:
        _cm, _cy = user_co2_totals(email)
        st.markdown(
            f"<div style='color:#9fe3bf;font-size:12px;margin-top:6px'>{t('Klimaavtrykk:')} "
            f"<b>{_cm} kg</b> {t('denne mnd')} · {_cy} {t('kg i år')}</div>",
            unsafe_allow_html=True)
    except Exception:
        pass
    if st.button(t("Logg ut"), use_container_width=True):
        st.query_params.clear()
        for k in list(st.session_state.keys()):
            st.session_state.pop(k, None)
        st.rerun()
if _goto in pages and nav != _goto:
    nav = _goto

if not companies:
    _lf = DATA_FILES.get(current_land())
    st.warning(f"{t('Bedriftslista for')} {LAND_NAVN.get(current_land(), current_land())} "
               f"({_lf.name if _lf else '?'}) "
               + t("mangler eller er tom. Bytt søkeområde under Innstillinger."))
    st.stop()


def page_header(title, sub):
    st.markdown(f"## {title}")
    st.markdown(f"<div class='bnc-sub'>{sub}</div>", unsafe_allow_html=True)
    st.write("")


# (UKEDAGER er definert språkavhengig lenger opp)
_kat_present = [k for k in KATEGORIER if any(c.get("kategori") == k for c in companies)]
if any(c.get("ditec") for c in companies):
    _kat_present = ["Ditec"] + _kat_present   # eget filtervalg for Ditec-sentre


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


@st.cache_data(ttl=21600, show_spinner=False)
def _osrm_routes(coords_key):
    """Hent kjøreruter (ekte veier) fra OSRM. coords_key = 'lon,lat;lon,lat;…'.
    Returnerer liste av {coords:[(lat,lon)…], dist_km}. Tom liste ved feil."""
    try:
        url = f"https://router.project-osrm.org/route/v1/driving/{coords_key}"
        r = requests.get(url, params={"overview": "simplified", "geometries": "geojson",
                                      "alternatives": "true"}, timeout=25)
        j = r.json()
    except Exception:
        return []
    if j.get("code") != "Ok":
        return []
    out = []
    for rt in j.get("routes", []):
        g = (rt.get("geometry") or {}).get("coordinates") or []
        if len(g) >= 2:
            out.append({"coords": [(la, lo) for lo, la in g],
                        "dist_km": round((rt.get("distance") or 0) / 1000.0, 1)})
    return out


def _downsample(coords, step_km=2.0):
    if len(coords) <= 2:
        return coords
    out, acc = [coords[0]], 0.0
    for i in range(1, len(coords)):
        acc += routing.haversine_km(coords[i - 1][0], coords[i - 1][1], coords[i][0], coords[i][1])
        if acc >= step_km:
            out.append(coords[i])
            acc = 0.0
    if out[-1] != coords[-1]:
        out.append(coords[-1])
    return out


def _count_near(coords, cands, within_km):
    ds = _downsample(coords, 3.0)
    lat0, lon0 = ds[0]
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    pxy = [((lo - lon0) * kx, (la - lat0) * ky) for la, lo in ds]
    n = 0
    for c in cands:
        if c.get("lat") is None:
            continue
        cx, cy = (c["lon"] - lon0) * kx, (c["lat"] - lat0) * ky
        bp = 1e9
        for i in range(len(pxy) - 1):
            ax, ay = pxy[i]
            bx, by = pxy[i + 1]
            dx, dy = bx - ax, by - ay
            s2 = dx * dx + dy * dy
            tf = 0.0 if s2 == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / s2))
            d = math.hypot(cx - (ax + tf * dx), cy - (ay + tf * dy))
            if d < bp:
                bp = d
            if bp <= within_km:
                break
        if bp <= within_km:
            n += 1
    return n


def road_corridor(waypoints, cands, within_km):
    """Ekte vei langs waypoints [(lat,lon)…]. Blant alternativene (f.eks. E16 vs E134)
    velges den med FLEST bedrifter innen within_km. Returnerer (coords[(lat,lon)…],
    dist_km) nedsamplet, eller None ved feil (da brukes rett linje)."""
    if len(waypoints) < 2:
        return None
    key = ";".join(f"{lo:.5f},{la:.5f}" for la, lo in waypoints)
    routes = _osrm_routes(key)
    if not routes:
        return None
    if len(routes) == 1:
        best = routes[0]
    else:
        best = max(routes, key=lambda rt: (_count_near(rt["coords"], cands, within_km),
                                           -rt["dist_km"]))
    return _downsample(best["coords"], 1.5), best["dist_km"]


def plan_poly(points, cands, hours, visit_min, speed, detour, target_n,
              drive_points=None, split_return=False):
    """Finner valgfrie drop-in-forslag langs en FAST kjørerute.

    `points`     – strekningen besøkene posisjoneres/spres langs (for tur-retur: ÉN vei).
    `drive_points` – den faktiske kjøreruta for avstand/tid/kartlinje (for tur-retur: hele
                    løkka Fra→…→Til→Fra). Er None brukes `points` til begge.
    Ruta til målet kjøres uansett – kun OMVEIEN til hvert forslag er ekstra tid/avstand.
    `split_return` merker forslagene som «Utreise» / «Retur» på en tur-retur.
    Returnerer også `route_pts` (kjøreruta) for kartet."""
    seg, cum, total = _poly_cum(points)
    dpts = drive_points or points
    _, _, drive_total = _poly_cum(dpts)
    base_drive = routing.travel_minutes(drive_total, speed, 1.3)
    res = {"route": [], "total_min": round(base_drive), "total_km": round(drive_total, 1),
           "n_candidates": len(cands), "route_pts": [[la, lo] for la, lo in dpts]}
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
    n = max(1, min(int(target_n or 6), len(scored), 40))
    binw = total / n
    picks, usedc = [], set()
    for j in range(n):
        lo = j * binw
        hi = (j + 1) * binw if j < n - 1 else total + 1
        inb = [(t, p, c) for (t, p, c) in scored
               if lo <= t < hi and str(c.get("orgnr")) not in usedc]
        if inb:
            inb.sort(key=lambda x: x[1])      # minst omvei vinner i hver luke
            picks.append(inb[0])
            usedc.add(str(inb[0][2].get("orgnr")))
    picks.sort(key=lambda x: x[0])
    # Utreise/retur: del jevnt i to; retur vises i motsatt geografisk rekkefølge.
    if split_return and len(picks) > 1:
        half = (len(picks) + 1) // 2
        out, ret = picks[:half], list(reversed(picks[half:]))
        ordered = [(p, "Utreise") for p in out] + [(p, "Retur") for p in ret]
    else:
        ordered = [(p, "") for p in picks]
    # Antall besøk styrer (target_n) – ikke en timegrense. Kim tar de han rekker på veien.
    route, extra_min, extra_km = [], 0.0, 0.0
    for (t, perp, c), retning in ordered:
        emin = routing.travel_minutes(2 * perp, speed, 1.3)   # omvei tur/retur
        extra_min += visit_min + emin
        extra_km += 2 * perp
        route.append({**c, "detour_km": round(perp, 1), "extra_min": round(emin),
                      "leg_km": round(2 * perp, 1), "leg_min": round(emin),
                      "retning": retning, "visit_min": round(visit_min),
                      "cum_min": round(base_drive * (t / total) + extra_min),
                      "stop": len(route) + 1})
    res.update({"route": route, "total_min": round(base_drive + extra_min),
                "total_km": round(drive_total + extra_km, 1)})
    return res


def _plan_pool(valgte, orgvalg, skip_dates):
    """Aktuelle bedrifter: valgte bransjer, ikke besøkt siste 4 uker / ekskludert / allerede
    planlagt på andre dager, og riktig selskapsform."""
    skip = (recently_visited(email) | excluded_orgnr(email)
            | planned_orgnr(email, skip_dates=skip_dates))
    off = {"Kun AS": "AS", "Kun ENK": "ENK"}.get(orgvalg)
    want_ditec = "Ditec" in valgte   # «Ditec» er et eget filter på tvers av kategorier
    return [c for c in companies
            if (c.get("kategori") in valgte or (want_ditec and c.get("ditec")))
            and str(c.get("orgnr")) not in skip
            and (off is None or c.get("orgform") == off)]


def save_plans(plans):
    for p in plans:
        save_plan(email, p["date"], p["start"], p["companies"], p["meta"])


def plan_area(days, start_txt, hours, visit_min, valgte, orgvalg, maxkm, n_visits):
    """Uten destinasjon: foreslå drop-in rundt startstedet, én plan pr. dag (dedup).
    BEREGNER uten å lagre – returnerer (status, melding, planer)."""
    slat, slon, slabel = geocode(start_txt) if start_txt else (None, None, start_txt)
    if not slat:
        return ("feil", "Fant ikke startadressen. Prøv «gate nr, sted» eller et stedsnavn.", [])
    pool = _plan_pool(valgte, orgvalg, days)
    used, plans = set(), []
    for day in days:
        cands = [c for c in pool if str(c.get("orgnr")) not in used]
        res = routing.plan_route(slat, slon, cands, hours,
                                 visit_minutes=visit_min, max_leg_km=maxkm,
                                 max_stops=int(n_visits) if n_visits else 8)
        route = res["route"]
        if n_visits:
            route = route[:int(n_visits)]
        for s in route:
            used.add(str(s.get("orgnr")))
        if route:
            plans.append({"plan_date": day.isoformat(), "date": day,
                          "start": {"lat": slat, "lon": slon, "label": slabel},
                          "companies": route,
                          "meta": {"total_min": res["total_min"], "total_km": res["total_km"],
                                   "n_candidates": res["n_candidates"], "route_pts": [[slat, slon]],
                                   "filter": {"kat": valgte, "org": orgvalg, "detour": None}}})
    if not plans:
        return ("tom", "Fant ingen bedrifter med disse valgene. Prøv flere bransjer.", [])
    tv = sum(len(p["companies"]) for p in plans)
    return ("ok", f"{len(plans)} dag(er) med til sammen {tv} drop-in-forslag.", plans)


def plan_legs(legs, hours, visit_min, valgte, orgvalg, detour, n_visits, area_km=60):
    """Planlegg én eller flere etapper. Hver etappe = {date, places:[tekst,…], retur:bool}.
    places er den FASTE kjøreruta (start → mellomstopp → mål). Ved retur legges hjemveien
    til og forslagene spres langs korridoren (merket Utreise/Retur).
    Når start og mål er samme sted (Kim blir værende i byen) søkes det i stedet drop-in
    rundt byen. Ingen duplikater på tvers av dagene."""
    dates = [lg["date"] for lg in legs]
    pool = _plan_pool(valgte, orgvalg, dates)
    speed = float(_settings.get("speed_kmh") or 70)
    used, plans = set(), []
    for lg in legs:
        pts, labels = [], []
        for pl in lg.get("places", []):
            if not (pl and str(pl).strip()):
                continue
            la, lo, lb = geocode(pl)
            if not la:
                return ("feil", f"Fant ikke «{pl}». Prøv «gate nr, sted» eller et stedsnavn.", [])
            pts.append((la, lo))
            labels.append(lb or pl)
        if len(pts) < 2:
            return ("feil", "Hver etappe trenger både et startsted og et sluttsted.", [])

        # Blir Kim værende på samme sted (start ≈ mål)? Da: drop-in rundt byen.
        stay = routing.haversine_km(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1]) < 3
        if stay:
            center = pts[-1]
            clabel = labels[-1]
            cands = [c for c in pool if str(c.get("orgnr")) not in used]
            res = routing.plan_route(center[0], center[1], cands, hours,
                                     visit_minutes=visit_min, max_leg_km=area_km,
                                     max_stops=int(n_visits) if n_visits else 8)
            route = res["route"]
            if n_visits:
                route = route[:int(n_visits)]
            for s in route:
                used.add(str(s.get("orgnr")))
            plans.append({"plan_date": lg["date"].isoformat(), "date": lg["date"],
                          "start": {"lat": center[0], "lon": center[1], "label": clabel},
                          "companies": route,
                          "meta": {"total_min": res["total_min"], "total_km": res["total_km"],
                                   "n_candidates": res["n_candidates"],
                                   "dest": {"lat": center[0], "lon": center[1], "label": clabel},
                                   "route_pts": [[center[0], center[1]]], "way": [clabel],
                                   "retur": False, "base": clabel,
                                   "filter": {"kat": valgte, "org": orgvalg, "detour": None}}})
            continue

        retur = bool(lg.get("retur"))
        cands = [c for c in pool if str(c.get("orgnr")) not in used]
        # Følg ekte hovedveier (E/Rv/Fv) der det er mulig; velg alternativet med
        # flest bedrifter. Faller tilbake til rett linje hvis veirouteren ikke svarer.
        road = road_corridor(pts, cands, max(detour, 10))
        corridor = road[0] if road else pts
        if retur:
            drive = corridor + list(reversed(corridor))
            dest = {"lat": pts[0][0], "lon": pts[0][1], "label": labels[0]}
        else:
            drive = corridor
            dest = {"lat": pts[-1][0], "lon": pts[-1][1], "label": labels[-1]}
        res = plan_poly(corridor, cands, hours, visit_min, speed, detour, n_visits,
                        drive_points=drive, split_return=retur)
        route = res["route"]
        for s in route:
            used.add(str(s.get("orgnr")))
        plans.append({"plan_date": lg["date"].isoformat(), "date": lg["date"],
                      "start": {"lat": pts[0][0], "lon": pts[0][1], "label": labels[0]},
                      "companies": route,
                      "meta": {"total_min": res["total_min"], "total_km": res["total_km"],
                               "n_candidates": res["n_candidates"], "dest": dest,
                               "route_pts": res["route_pts"], "way": labels, "retur": retur,
                               "filter": {"kat": valgte, "org": orgvalg, "detour": detour}}})
    if not plans:
        return ("tom", "Ingen etapper å planlegge.", [])
    tv = sum(len(p["companies"]) for p in plans)
    return ("ok", f"{len(plans)} etappe(r) med til sammen {tv} drop-in-forslag langs ruta.", plans)


def alt_company(meta, current_comps, orgnr):
    """Finn en annen aktuell bedrift nær samme punkt på ruta (til «bytt ut»-knappen)."""
    rp = meta.get("route_pts") or []
    poly = [(p[0], p[1]) for p in rp]
    f = meta.get("filter") or {}
    target = next((c for c in current_comps if str(c.get("orgnr")) == str(orgnr)), None)
    if not target or target.get("lat") is None:
        return None
    det = f.get("detour") or 25
    off = {"Kun AS": "AS", "Kun ENK": "ENK"}.get(f.get("org"))
    kat = f.get("kat") or _kat_present
    want_ditec = "Ditec" in kat
    skip = (recently_visited(email) | excluded_orgnr(email)
            | {str(c.get("orgnr")) for c in current_comps})
    speed = float(_settings.get("speed_kmh") or 70)
    best, bestd = None, 1e9
    for c in companies:
        if ((c.get("kategori") not in kat and not (want_ditec and c.get("ditec")))
                or c.get("lat") is None
                or str(c.get("orgnr")) in skip
                or (off is not None and c.get("orgform") != off)):
            continue
        d = routing.haversine_km(target["lat"], target["lon"], c["lat"], c["lon"])
        if d < bestd:
            best, bestd = c, d
    if not best or bestd > 60:
        return None
    # omvei fra ruta for den nye bedriften
    perp = _perp_to_poly(poly, best["lat"], best["lon"]) if len(poly) >= 2 else 0.0
    emin = routing.travel_minutes(2 * perp, speed, 1.3)
    return {**best, "detour_km": round(perp, 1), "extra_min": round(emin),
            "leg_km": round(2 * perp, 1), "leg_min": round(emin),
            "retning": target.get("retning", ""), "visit_min": target.get("visit_min", 60),
            "cum_min": target.get("cum_min", 0), "stop": target.get("stop", 1)}


def _save_recompute(user, d, start, comps, meta):
    """Lagre plan på nytt etter at forslag er fjernet/byttet, og oppdater km/tid
    (fast korridor + omvei pr. gjenværende forslag)."""
    meta = dict(meta)
    poly = [(p[0], p[1]) for p in (meta.get("route_pts") or [])]
    speed = float(_settings.get("speed_kmh") or 70)
    if len(poly) >= 2:
        _, _, base_km = _poly_cum(poly)
        base_drive = routing.travel_minutes(base_km, speed, 1.3)
    else:
        base_km, base_drive = 0.0, 0.0
    extra_km = sum(float(c.get("leg_km") or 0) for c in comps)
    extra_min = sum(float(c.get("visit_min") or 0) + float(c.get("extra_min") or 0) for c in comps)
    meta["total_km"] = round(base_km + extra_km, 1)
    meta["total_min"] = round(base_drive + extra_min)
    save_plan(user, d, start, comps, meta)


def _preview_recompute(plan):
    """Oppdater km/tid og stopp-nummer for en plan i forhåndsvisningen (uten lagring)."""
    comps = plan["companies"]
    meta = plan["meta"]
    poly = [(p[0], p[1]) for p in (meta.get("route_pts") or [])]
    speed = float(_settings.get("speed_kmh") or 70)
    if len(poly) >= 2:
        _, _, base_km = _poly_cum(poly)
        base_drive = routing.travel_minutes(base_km, speed, 1.3)
    else:
        base_km, base_drive = 0.0, 0.0
    extra_km = sum(float(c.get("leg_km") or 0) for c in comps)
    extra_min = sum(float(c.get("visit_min") or 0) + float(c.get("extra_min") or 0) for c in comps)
    for i, c in enumerate(comps, 1):
        c["stop"] = i
    meta["total_km"] = round(base_km + extra_km, 1)
    meta["total_min"] = round(base_drive + extra_min)


def _perp_to_poly(poly, lat, lon):
    lat0, lon0 = poly[0]
    kx = 111.320 * math.cos(math.radians(lat0))
    ky = 110.574
    cx, cy = (lon - lon0) * kx, (lat - lat0) * ky
    best = 1e9
    for i in range(len(poly) - 1):
        ax = (poly[i][1] - lon0) * kx
        ay = (poly[i][0] - lat0) * ky
        bx = (poly[i + 1][1] - lon0) * kx
        by = (poly[i + 1][0] - lat0) * ky
        dx, dy = bx - ax, by - ay
        s2 = dx * dx + dy * dy
        tf = 0.0 if s2 == 0 else max(0.0, min(1.0, ((cx - ax) * dx + (cy - ay) * dy) / s2))
        best = min(best, math.hypot(cx - (ax + tf * dx), cy - (ay + tf * dy)))
    return best


# -------------------------------------------------- dialog: registrer besøk ---
@st.dialog("Registrer besøk")
def register_dialog(company):
    st.markdown(f"**{company.get('navn','')}**  \n"
                f"<span class='bnc-sub'>{company.get('adresse','')}, "
                f"{company.get('postnr','')} {company.get('poststed','')}</span>",
                unsafe_allow_html=True)
    with st.form(f"reg_{company.get('orgnr')}"):
        outcome = st.selectbox(t("Besøksresultat"), ["Interessert", "Vil tenke", "Ikke interessert",
                                                  "Ikke til stede", "Solgt", "Annet"], format_func=t)
        report = st.text_area(t("Besøksrapport"), placeholder=t("Hva skjedde på besøket …"), height=130)
        contact = st.text_input(t("Kundens e-post"), value=company.get("epost", ""))
        send_thanks = st.checkbox(t("Send «takk for praten» (går ut 2 virkedager etter)"),
                                  value=bool(company.get("epost")))
        no_more = st.checkbox(t("Ikke besøk denne kunden igjen"),
                              help=t("Skjuler bedriften permanent fra framtidige forslag. "
                                   "Kan angres under Innstillinger → Ekskluderte bedrifter."))
        lagre_besok = st.form_submit_button(t("Lagre besøk og send rapport"), type="primary",
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
            _mlang = current_lang()   # selgerens språk styrer e-post + avsender
            rc = get_content("report", _mlang)
            rmap = {"selger": email, "bedrift": company.get("navn", ""),
                    "orgnr": company.get("orgnr", ""),
                    "adresse": f"{company.get('adresse','')}, {company.get('postnr','')} {company.get('poststed','')}",
                    "dato": today.isoformat(), "utfall": outcome, "rapport": report,
                    "km": leg_km, "co2": co2_kg(leg_km, _settings)}
            enqueue_email({
                "kind": "report", "to_email": BACKOFFICE_EMAIL, "lang": _mlang,
                "subject": apply_vars(rc["subject"], rmap),
                "body": apply_vars(rc["body"], rmap),
                "send_after": today.isoformat(),
                "payload": {"orgnr": str(company.get("orgnr")), "user": email}})
            if send_thanks and contact:
                tc = get_content("thankyou", _mlang)
                enqueue_email({
                    "kind": "thankyou", "to_email": contact, "lang": _mlang,
                    "subject": fill_thanks(tc["subject"], _settings, email),
                    "body": fill_thanks(tc["body"], _settings, email),
                    "send_after": add_business_days(today, 2).isoformat(),
                    "payload": {"navn": company.get("navn", ""), "selger": email}})
            if no_more:
                exclude_company(email, company)
            user_co2_totals.clear()
            _m = t("Besøk registrert – rapport sendt.")
            st.session_state["_toast"] = _m + (t(" Kunden skjules fra framtidige forslag.")
                                               if no_more else t(" Bedriften er merket besøkt."))
            st.rerun()
        except Exception as exc:
            st.error(t("Kunne ikke lagre:") + f" {exc}")


VISIT_OUTCOMES = ["Interessert", "Vil tenke", "Ikke interessert",
                  "Ikke til stede", "Solgt", "Annet"]


@st.dialog("Rediger besøk")
def edit_visit_dialog(r):
    st.markdown(f"**{r.get('navn','')}**  \n"
                f"<span class='bnc-sub'>{r.get('visited_on','')} · {r.get('kommune','')}</span>",
                unsafe_allow_html=True)
    opts = VISIT_OUTCOMES[:]
    cur = r.get("outcome", "")
    if cur and cur not in opts:
        opts = [cur] + opts
    with st.form(f"edit_{r.get('id')}"):
        outcome = st.selectbox(t("Besøksresultat"), opts, format_func=t,
                               index=opts.index(cur) if cur in opts else 0)
        report = st.text_area(t("Besøksrapport"), value=r.get("report") or "", height=140)
        contact = st.text_input(t("Kundens e-post"), value=r.get("contact_email") or "")
        lagre = st.form_submit_button(t("Lagre endringer"), type="primary",
                                      use_container_width=True)
    if lagre:
        try:
            sb_update("visits", {"id": f"eq.{r.get('id')}"},
                      {"outcome": outcome, "report": report, "contact_email": contact})
            st.session_state["_toast"] = t("Besøk oppdatert.")
            st.rerun()
        except Exception as exc:
            st.error(t("Kunne ikke lagre:") + f" {exc}")


def route_map(start, route, dest=None, route_pts=None):
    try:
        import pydeck as pdk
        pts = [{"lat": start["lat"], "lon": start["lon"], "navn": "START",
                "label": "S", "color": [31, 157, 91]}]
        pts += [{"lat": s["lat"], "lon": s["lon"],
                 "navn": s.get("navn", "") + (f" · {s.get('retning')}" if s.get("retning") else ""),
                 "label": str(s["stop"]), "color": [27, 42, 74]} for s in route]
        if dest and dest.get("lat") is not None:
            pts.append({"lat": dest["lat"], "lon": dest["lon"],
                        "navn": dest.get("label") or "Mål", "label": "M", "color": [192, 57, 43]})
        if route_pts and len(route_pts) >= 2:
            path = [[p[1], p[0]] for p in route_pts]
        else:
            path = [[p["lon"], p["lat"]] for p in pts]
        lats = [p["lat"] for p in pts] + [p[1] for p in path]
        lons = [p["lon"] for p in pts] + [p[0] for p in path]
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


def leaflet_route_html(start, route, height=360, line_color="#1b2a4a", dest=None,
                       route_pts=None, focus=None):
    """Robust kart (Leaflet i iframe) – virker også inne i expandere, der pydeck feiler.
    route_pts = den faste kjøreruta (korridoren) som tegnes som linje. dest = sluttpunkt
    (Til/hjem, rødt). Besøkene vises som nummererte punkter langs ruta."""
    pts = []
    if start and start.get("lat") is not None:
        pts.append({"lat": start["lat"], "lon": start["lon"], "label": "S",
                    "navn": start.get("label") or "Start", "kind": "start"})
    nr = 0
    for s in route:
        if s.get("lat") is None:
            continue
        nr += 1
        rt = s.get("retning")
        pts.append({"lat": s["lat"], "lon": s["lon"], "label": str(nr),
                    "navn": s.get("navn", "") + (f" · {rt}" if rt else ""), "kind": "stop"})
    if dest and dest.get("lat") is not None:
        pts.append({"lat": dest["lat"], "lon": dest["lon"], "label": "M",
                    "navn": dest.get("label") or "Mål", "kind": "dest"})
    if not pts:
        st.caption(t("Ingen punkter å vise på kart."))
        return
    # Linje = korridoren (route_pts) hvis vi har den, ellers koble start→stopp→mål.
    line = route_pts if (route_pts and len(route_pts) >= 2) else [[p["lat"], p["lon"]] for p in pts]
    tmpl = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{margin:0}#map{height:__H__px;width:100%}</style></head>
<body><div id="map"></div><script>
var pts=__DATA__, line="__LINE__", path=__PATH__, focus=__FOCUS__;
var map=L.map('map',{scrollWheelZoom:false});
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
 {maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
L.polyline(path,{color:line,weight:4,opacity:.85}).addTo(map);
function ic(l,c){return L.divIcon({className:'',iconSize:[24,24],iconAnchor:[12,12],
 html:'<div style="background:'+c+';color:#fff;width:24px;height:24px;border-radius:50%;'
 +'display:flex;align-items:center;justify-content:center;font:700 12px sans-serif;'
 +'border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.4)">'+l+'</div>'});}
pts.forEach(function(p){var c=p.kind=='start'?'#1f9d5b':(p.kind=='dest'?'#c0392b':line);
 L.marker([p.lat,p.lon],{icon:ic(p.label,c)}).addTo(map).bindTooltip(p.label+'. '+p.navn);});
var allpts=path.concat(pts.map(function(p){return [p.lat,p.lon];}));
var b=L.latLngBounds(allpts);
if(focus){L.circleMarker(focus,{radius:17,color:'#e67e22',weight:3,fill:false}).addTo(map);}
function fit(){if(focus){map.setView(focus,16);return;}
 if(allpts.length>1){map.fitBounds(b,{padding:[30,30],maxZoom:13});}
 else{map.setView(allpts[0],12);}}
fit();
setTimeout(function(){map.invalidateSize();fit();},300);
setTimeout(function(){map.invalidateSize();fit();},800);
</script></body></html>"""
    html = (tmpl.replace("__DATA__", json.dumps(pts))
                .replace("__PATH__", json.dumps(line))
                .replace("__FOCUS__", json.dumps(focus if focus else None))
                .replace("__LINE__", line_color)
                .replace("__H__", str(height)))
    import streamlit.components.v1 as components
    components.html(html, height=height + 4)


DAY_HEX = ["#1b2a4a", "#c0392b", "#1f9d5b", "#e67e22", "#8e44ad",
           "#2980b9", "#16a085", "#d4ac0d"]


def leaflet_multi_html(plans, height=420, focus=None):
    """Samlet flerdagerskart (Leaflet) – én farge per dag, nummererte stopp + forklaring."""
    paths, pts, legend = [], [], []
    for di, p in enumerate(plans):
        comps = p.get("companies") or []
        meta = p.get("meta") or {}
        col = DAY_HEX[di % len(DAY_HEX)]
        rpts = meta.get("route_pts")
        if rpts and len(rpts) >= 2:
            paths.append({"line": [[a, b] for a, b in rpts], "color": col})
        try:
            dt = datetime.fromisoformat(p["plan_date"]).date()
            dag = f"{UKEDAGER[dt.weekday()].capitalize()} {dt.strftime('%d.%m')}"
        except Exception:
            dag = p.get("plan_date", "")
        if comps:
            legend.append((col, dag))
        nr = 0
        for s in comps:
            if s.get("lat") is None:
                continue
            nr += 1
            pts.append({"lat": s["lat"], "lon": s["lon"], "label": str(nr),
                        "navn": s.get("navn", ""), "dag": dag, "color": col})
    if not pts and not paths:
        st.caption(t("Ingen punkter å vise på kart."))
        return
    tmpl = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>html,body{margin:0}#map{height:__H__px;width:100%}</style></head>
<body><div id="map"></div><script>
var paths=__PATHS__, pts=__PTS__, focus=__FOCUS__;
var map=L.map('map',{scrollWheelZoom:false});
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
 {maxZoom:19,attribution:'© OpenStreetMap'}).addTo(map);
var all=[];
paths.forEach(function(pa){ L.polyline(pa.line,{color:pa.color,weight:4,opacity:.85}).addTo(map);
 pa.line.forEach(function(x){all.push(x);}); });
function ic(l,c){return L.divIcon({className:'',iconSize:[22,22],iconAnchor:[11,11],
 html:'<div style="background:'+c+';color:#fff;width:22px;height:22px;border-radius:50%;'
 +'display:flex;align-items:center;justify-content:center;font:700 11px sans-serif;'
 +'border:2px solid #fff;box-shadow:0 0 3px rgba(0,0,0,.4)">'+l+'</div>'});}
pts.forEach(function(p){ L.marker([p.lat,p.lon],{icon:ic(p.label,p.color)}).addTo(map)
 .bindTooltip(p.dag+' · '+p.label+'. '+p.navn); all.push([p.lat,p.lon]); });
var b=L.latLngBounds(all);
if(focus){L.circleMarker(focus,{radius:17,color:'#e67e22',weight:3,fill:false}).addTo(map);}
function fit(){ if(focus){map.setView(focus,16);return;}
 if(all.length>1){map.fitBounds(b,{padding:[25,25]});} else if(all.length){map.setView(all[0],11);} }
fit();
setTimeout(function(){map.invalidateSize();fit();},300);
setTimeout(function(){map.invalidateSize();fit();},800);
</script></body></html>"""
    html = (tmpl.replace("__PATHS__", json.dumps(paths))
                .replace("__PTS__", json.dumps(pts))
                .replace("__FOCUS__", json.dumps(focus if focus else None))
                .replace("__H__", str(height)))
    import streamlit.components.v1 as components
    components.html(html, height=height + 4)
    chips = "".join(
        f"<span style='display:inline-block;margin:2px 10px 2px 0'>"
        f"<span style='display:inline-block;width:11px;height:11px;border-radius:3px;"
        f"background:{c};margin-right:5px'></span>{d}</span>" for c, d in legend)
    if chips:
        st.markdown(f"<div style='font-size:13px'>{chips}</div>", unsafe_allow_html=True)


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
            rpts = (p.get("meta") or {}).get("route_pts")
            if rpts and len(rpts) >= 2:
                line = [[pt[1], pt[0]] for pt in rpts]   # korridoren for dagen
                for pt in rpts:
                    all_lat.append(pt[0])
                    all_lon.append(pt[1])
            else:
                line = []
                if start.get("lat") is not None:
                    line.append([start["lon"], start["lat"]])
                    all_lat.append(start["lat"])
                    all_lon.append(start["lon"])
            for s in comps:
                if s.get("lat") is None:
                    continue
                if not (rpts and len(rpts) >= 2):
                    line.append([s["lon"], s["lat"]])
                all_lat.append(s["lat"])
                all_lon.append(s["lon"])
                pts.append({"lat": s["lat"], "lon": s["lon"], "navn": s.get("navn", ""),
                            "label": str(s.get("stop", "")), "dag": dag, "color": col})
            if len(line) >= 2:
                paths.append({"path": line, "color": col})
        if not pts:
            st.caption(t("Ingen punkter å vise på kart."))
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
MND_FULL = _Loc(
    ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august",
     "september", "oktober", "november", "desember"],
    ["januari", "februari", "mars", "april", "maj", "juni", "juli", "augusti",
     "september", "oktober", "november", "december"])
MND_ABBR = _Loc(
    ["jan.", "feb.", "mars", "apr.", "mai", "juni", "juli", "aug.", "sep.", "okt.", "nov.", "des."],
    ["jan.", "feb.", "mars", "apr.", "maj", "juni", "juli", "aug.", "sep.", "okt.", "nov.", "dec."])


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
                f"<div class='{vcls}'>{val}</div></div>")
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
    _plans = [p for p in planned_rows(email)      # kommende planlagte dager (>= i dag)
              if p["plan_date"] >= today.isoformat()]
    byd = {p["plan_date"]: p for p in _plans}
    opts = sorted(set(byd.keys()) | {today.isoformat()})
    default_iso = today.isoformat() if today.isoformat() in opts else opts[0]
    sel_iso = default_iso
    if len(opts) > 1:
        def _fmt_day(iso):
            dt = datetime.fromisoformat(iso).date()
            n = len(byd.get(iso, {}).get("companies") or [])
            tag = f" — {n} {t('besøk')}" if n else t(" — ingen rute")
            pre = t("I dag · ") if dt == today else ""
            return f"{pre}{UKEDAGER[dt.weekday()].capitalize()} {dt.strftime('%d.%m.%Y')}{tag}"
        sel_iso = st.selectbox(t("Velg dag"), opts, index=opts.index(default_iso),
                               format_func=_fmt_day)
    seldate = datetime.fromisoformat(sel_iso).date()
    wd = UKEDAGER[seldate.weekday()].capitalize()
    _subtitle = t("dagens planlagte rute") if seldate == today else t("planlagt rute")
    page_header(t("Min dag"), f"{wd} {seldate.strftime('%d.%m.%Y')} – {_subtitle}")
    plan = byd.get(sel_iso) or get_plan(email, seldate)
    route = (plan or {}).get("companies") or []
    start = (plan or {}).get("start")
    meta = (plan or {}).get("meta") or {}

    if not route:
        _når = t("i dag") if seldate == today else t("denne dagen")
        st.info(t("Ingen planlagt rute") + f" {_når}. "
                + t("Gå til **Planlegging** for å sette opp ruter – eller bruk **Drop-in** for et besøk på sparket."))
    else:
        h = int(meta.get("total_min", 0)) // 60
        m = int(meta.get("total_min", 0)) % 60
        day_co2 = co2_kg(meta.get("total_km", 0), _settings)
        s1, s2, s3, s4 = st.columns(4)
        s1.markdown(f"<div class='bnc-stat'><div class='k'>{t('Besøk i ruta')}</div>"
                    f"<div class='v'>{len(route)}</div></div>", unsafe_allow_html=True)
        s2.markdown(f"<div class='bnc-stat'><div class='k'>{t('Samlet kjøring')}</div>"
                    f"<div class='v'>{meta.get('total_km',0)} <small>km</small></div></div>", unsafe_allow_html=True)
        s3.markdown(f"<div class='bnc-stat'><div class='k'>{t('Beregnet tid')}</div>"
                    f"<div class='v'>{h} t {m} <small>min</small></div></div>", unsafe_allow_html=True)
        s4.markdown(f"<div class='bnc-stat'><div class='k'>{t('Klimaavtrykk')}</div>"
                    f"<div class='v'>{day_co2} <small>kg CO₂e</small></div></div>", unsafe_allow_html=True)
        st.write("")

        mc1, mc2 = st.columns([3, 1])
        show_map = mc1.checkbox(t("Vis kart"), value=True)
        if mc2.button(t("Slett dagen"), key="mindag_del", use_container_width=True,
                      help=t("Slett hele den planlagte ruta for denne dagen")):
            try:
                delete_plan(email, seldate.isoformat())
                st.session_state["_toast"] = t("Dagens rute slettet.")
                st.rerun()
            except Exception as exc:
                st.error(f"{exc}")
        list_col, map_col = st.columns([5, 6]) if show_map else (st.container(), None)
        with list_col:
            g_url = gmaps_route_url(start, route, dest=meta.get("dest")) if start else None
            if g_url:
                st.link_button(t("Åpne hele ruta i Google Maps"), g_url, use_container_width=True)
            st.markdown(t("#### Dagens besøk"))
            visited = visited_orgnr_this_month(email)
            for s in route:
                org = str(s.get("orgnr"))
                done = org in visited
                _of = s.get("orgform", "")
                _ofh = f" <small style='color:#6a7488'>{_of}</small>" if _of else ""
                _rt = s.get("retning")
                _rtb = f"<span class='bnc-badge'>{_rt}</span> " if _rt else ""
                badge = ("<span class='bnc-badge b-ok'>✓ Besøkt</span>" if done
                         else f"{_rtb}<span class='bnc-badge'>{s.get('kategori', s.get('bransje_kode',''))}</span>")
                _dk = s.get("detour_km")
                _omv = (f"omvei {_dk} km (+{s.get('extra_min','?')} min)" if _dk is not None
                        else f"+{s.get('leg_km')} km / {s.get('leg_min')} min")
                st.markdown(
                    f"<div class='bnc-stop{' done' if done else ''}'><b>{s.get('stop','')}. {s.get('navn','')}</b><br>"
                    f"{badge}{_ofh} {s.get('adresse','')}, {s.get('postnr','')} {s.get('poststed','')}<br>"
                    f"<small>{_omv}</small></div>",
                    unsafe_allow_html=True)
                if done:
                    if st.button(t("Angre besøk"), key=f"undo_{org}"):
                        try:
                            delete_visit(email, org)
                            st.session_state["_toast"] = t("Besøk nullstilt.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")
                else:
                    b1, b2, b3, b4 = st.columns([3, 3, 1, 1.5])
                    if b1.button(t("Registrer"), key=f"reg_{org}", use_container_width=True):
                        register_dialog(s)
                    b2.link_button(t("Naviger"), gmaps_dest_url(s), use_container_width=True)
                    if b3.button(t("✕"), key=f"rm_{org}",
                                 help=t("Fjern fra ruta (kan dukke opp igjen senere)"),
                                 use_container_width=True):
                        try:
                            rest = [x for x in route if str(x.get("orgnr")) != org]
                            for i, x in enumerate(rest):
                                x["stop"] = i + 1
                            _save_recompute(email, seldate, start, rest, meta)
                            st.session_state["_toast"] = f"{s.get('navn','')} " + t("fjernet fra ruta.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")
                    if b4.button(t("Skjul"), key=f"exc_{org}",
                                 help=t("Ikke aktuell – skjul permanent fra framtidige forslag"),
                                 use_container_width=True):
                        try:
                            exclude_company(email, s)
                            rest = [x for x in route if str(x.get("orgnr")) != org]
                            for i, x in enumerate(rest):
                                x["stop"] = i + 1
                            _save_recompute(email, seldate, start, rest, meta)
                            st.session_state["_toast"] = f"{s.get('navn','')} " + t("skjult permanent.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")
        if show_map and map_col is not None and start:
            with map_col:
                leaflet_route_html(start, route, dest=meta.get("dest"),
                                   route_pts=meta.get("route_pts"))


# =============================================================== DROP-IN ======
if nav == "Drop-in":
    page_header(t("Drop-in"), t("Søk opp en bedrift og registrer et besøk på sparket"))
    with st.form("dropin_sok"):
        sc1, sc2 = st.columns([5, 1])
        q = sc1.text_input(t("Søk bedrift (navn eller org.nr)"),
                           placeholder=t("Skriv navn eller org.nr …"), label_visibility="collapsed")
        sc2.form_submit_button(t("Søk"), use_container_width=True)
    if q:
        toks = [t for t in q.strip().lower().split() if t]
        qd = "".join(ch for ch in q if ch.isdigit())

        def _match(c):
            hay = (f"{c.get('navn','')} {c.get('poststed','')} "
                   f"{c.get('adresse','')}").lower()
            if toks and all(t in hay for t in toks):
                return True
            return bool(qd and len(qd) >= 3 and qd in str(c.get("orgnr", "")))

        hits = [c for c in companies if _match(c)][:25]
        if not hits:
            st.caption(t("Ingen treff. Prøv færre ord, eller søk på org./bedr.nr."))
        for c in hits:
            cc = st.columns([5, 2])
            cc[0].markdown(f"**{c.get('navn','')}**  \n"
                           f"<span class='bnc-sub'>{c.get('kategori','')} · "
                           f"{c.get('poststed','')} · nr {c.get('orgnr','')}</span>",
                           unsafe_allow_html=True)
            if cc[1].button(t("Registrer"), key=f"d_{c.get('orgnr')}", use_container_width=True):
                register_dialog(c)
    else:
        st.info(t("Søk opp en bedrift for å registrere et drop-in-besøk."))


# ============================================================ PLANLEGGING ======
if nav == "Planlegging":
    page_header(t("Planlegg besøk"), t("Finn kundebesøk langs ruta di."))
    today = date.today()
    ss = st.session_state
    ss.setdefault("pl_step", 1)
    ss.setdefault("w_from", None)
    ss.setdefault("w_to", None)
    ss.setdefault("w_calym", (today.year, today.month))
    ss.setdefault("w_start", _settings.get("start_label", ""))
    ss.setdefault("w_til", "")
    ss.setdefault("w_vias", [])
    ss.setdefault("w_legs", [])
    ss.setdefault("w_trip_mode", "base")
    ss.setdefault("w_base", "")
    ss.setdefault("w_retur", False)
    ss.setdefault("w_antall", 6)
    ss.setdefault("w_org", "AS + ENK")
    ss.setdefault("w_all", False)
    ss.setdefault("w_cat", [])
    ss.setdefault("w_hours", float(_settings.get("hours_per_day") or 7))
    ss.setdefault("w_visit", int(_settings.get("avg_visit_minutes") or 60))
    ss.setdefault("w_max", 120)
    ss.setdefault("w_det", 15)
    ss.setdefault("pl_review", False)
    ss.setdefault("pl_preview", [])
    ss.setdefault("pv_focus", None)
    if ss["pl_step"] > 1 and ss["w_from"] is None:
        ss["pl_step"] = 1

    allplans = planned_rows(email)
    planned_set = {p["plan_date"] for p in allplans if (p.get("companies") or [])}

    # --- Godkjenning: vis ruteforslag før det legges i Min dag --------------
    if ss.get("pl_review") and ss.get("pl_preview"):
        plans = ss["pl_preview"]
        tot_km = sum(float(p["meta"].get("total_km") or 0) for p in plans)
        tot_n = sum(len(p["companies"]) for p in plans)
        st.markdown(t("#### Forslag til rute – godkjenn for å legge i Min dag"))
        st.caption(f"{len(plans)} dag(er) · {tot_n} forslag · ca. {tot_km:.0f} km totalt. "
                   "Klikk på en bedrift for å zoome til den i kartet. "
                   "Ingenting lagres før du trykker «Godkjenn».")
        st.caption(
            t("Obs om adressene: Vi bruker den adressen som er registrert i Brønnøysund for "
            "Norge, for Sverige: SCB (Statistiska centralbyrån). Den er ikke alltid den samme "
            "som der bedriften faktisk holder til. Sjekk «Nettside» eller «Google» før du drar "
            "hvis noe ser rart ut."))
        _focus = ss.get("pv_focus")
        if _focus and st.button(t("Vis hele ruta i kartet"), key="pv_focus_clear"):
            ss["pv_focus"] = None
            st.rerun()
        if len(plans) > 1:
            leaflet_multi_html(plans, focus=_focus)
        for pi, p in enumerate(plans):
            d = date.fromisoformat(p["plan_date"])
            meta = p["meta"]
            with st.expander(f"{UKEDAGER[d.weekday()].capitalize()} {d.strftime('%d.%m.%Y')}"
                             f"  ·  {len(p['companies'])} forslag  ·  "
                             f"{float(meta.get('total_km') or 0):.0f} km", expanded=(len(plans) == 1)):
                if len(plans) == 1:
                    leaflet_route_html(p["start"], p["companies"], dest=meta.get("dest"),
                                       route_pts=meta.get("route_pts"), focus=_focus)
                if not p["companies"]:
                    st.caption(t("Ingen forslag igjen for denne dagen."))
                for i, c in enumerate(p["companies"], 1):
                    org = str(c.get("orgnr"))
                    sub = []
                    if c.get("poststed"):
                        sub.append(c["poststed"])
                    if c.get("kategori"):
                        sub.append(c["kategori"])
                    if c.get("detour_km") is not None:
                        sub.append(f"omvei {c['detour_km']:.0f} km (+{c.get('extra_min', 0):.0f} min)")
                    if c.get("retning"):
                        sub.append(c["retning"])
                    ctxt, cgoog, cbyt, cfj = st.columns([5, 1.5, 1.3, 1])
                    with ctxt:
                        if st.button(f"{i}. {c.get('navn', '')}", key=f"pv_focus_{pi}_{org}",
                                     type="tertiary", use_container_width=True,
                                     help=t("Zoom til denne bedriften i kartet")):
                            if c.get("lat") is not None:
                                ss["pv_focus"] = [c["lat"], c["lon"]]
                                st.rerun()
                        if sub:
                            st.markdown(
                                f"<div style='color:#6a7488;font-size:13px;margin-top:-8px'>"
                                f"{' · '.join(sub)}</div>", unsafe_allow_html=True)
                    site = (c.get("nettside") or "").strip()
                    if site:
                        _url = (site if site.lower().startswith(("http://", "https://"))
                                else "https://" + site)
                        cgoog.link_button(t("Nettside"), _url, use_container_width=True,
                                          help=t("Åpne bedriftens nettside i ny fane"))
                    else:
                        cgoog.link_button(t("Google"), google_q(c), use_container_width=True,
                                          help=t("Google-søk på bedriften i ny fane"))
                    if cbyt.button(t("Bytt"), key=f"pv_byt_{pi}_{org}", use_container_width=True,
                                   help=t("Finn en annen bedrift i nærheten i stedet")):
                        alt = alt_company(meta, p["companies"], org)
                        if alt:
                            p["companies"][i - 1] = alt
                            _preview_recompute(p)
                            st.rerun()
                        else:
                            st.toast(t("Fant ingen annen aktuell bedrift i nærheten."))
                    if cfj.button(t("✕"), key=f"pv_fj_{pi}_{org}", use_container_width=True,
                                  help=t("Fjern dette forslaget")):
                        p["companies"].pop(i - 1)
                        _preview_recompute(p)
                        st.rerun()
        st.caption(t("Fjern forslag du ikke vil ha med «✕», eller bytt det ut med en annen "
                   "bedrift i nærheten med «Bytt». Ingenting lagres før du godkjenner."))
        st.divider()
        c_ok, c_no = st.columns([2, 1])
        if c_ok.button(t("Godkjenn og legg i Min dag"), type="primary",
                       use_container_width=True):
            save_plans(plans)
            n = len(plans)
            ss["pl_preview"] = []
            ss["pl_review"] = False
            ss["pv_focus"] = None
            ss["pl_step"] = 1
            st.session_state["_toast"] = f"{n} " + t("dag(er) lagt i Min dag.")
            st.rerun()
        if c_no.button(t("Forkast"), use_container_width=True):
            ss["pl_preview"] = []
            ss["pl_review"] = False
            ss["pv_focus"] = None
            st.rerun()
        st.stop()

    wizard_indicator(ss["pl_step"])

    # ------------------------------------------------- STEG 1 · PERIODE --------
    if ss["pl_step"] == 1:
        lc1, lc2 = st.columns([1, 2])
        with lc1:
            land_picker("land_pl",
                        help="Velg land før du søker. Styrer både stedssøk og hvilke "
                             "bedrifter som foreslås.")
        st.markdown(t("#### Når skal du reise?"))
        st.caption(f"Velg én dag eller en periode. I dag er {UKEDAGER[today.weekday()]} "
                   f"{today.strftime('%d.%m.%Y')}.")
        active = "from" if (ss["w_from"] is None or ss["w_to"] is not None) else "to"
        date_cards(ss["w_from"], ss["w_to"], active)

        cy, cm = ss["w_calym"]
        calendar_daystyle(cy, cm, ss["w_from"], ss["w_to"], planned_set, today)
        with st.container(key="bnc_cal"):
            nv1, nv2, nv3 = st.columns([1, 2, 1])
            if nv1.button(t("‹"), use_container_width=True, key="cal_prev"):
                ss["w_calym"] = (cy - 1, 12) if cm == 1 else (cy, cm - 1)
                st.rerun()
            nv2.markdown(f"<div style='text-align:center;font-weight:700;"
                         f"font-family:Archivo,sans-serif;padding-top:7px'>"
                         f"{MND_FULL[cm-1].capitalize()} {cy}</div>", unsafe_allow_html=True)
            if nv3.button(t("›"), use_container_width=True, key="cal_next"):
                ss["w_calym"] = (cy + 1, 1) if cm == 12 else (cy, cm + 1)
                st.rerun()
            hdr = st.columns(7)
            for i, wd in enumerate([t("Man"), t("Tir"), t("Ons"), t("Tor"), t("Fre"), t("Lør"), t("Søn")]):
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
                        _wf, _wt = ss["w_from"], ss["w_to"]
                        if _wf is None or _wt is not None:
                            ss["w_from"], ss["w_to"] = d, None
                        elif d >= _wf:
                            ss["w_to"] = d
                        else:
                            ss["w_from"], ss["w_to"] = d, None
                        st.rerun()

        if not ss["w_from"]:
            st.caption(t("Velg en startdato i kalenderen."))
        elif not ss["w_to"]:
            st.caption(t("Velg sluttdato … (eller klikk samme dag igjen for én dag)"))
        elif ss["w_from"] == ss["w_to"]:
            st.caption(t("Én dag valgt."))
        else:
            _nd = (ss["w_to"] - ss["w_from"]).days + 1
            st.caption(f"{_nd} " + t("dager valgt."))

        if st.button(t("Neste: område  →"), type="primary", use_container_width=True,
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
                    f"({count_weekdays(fr, to)} {t('hverdager')})")
        ps1, ps2 = st.columns([4, 1])
        ps1.markdown(f"<div class='psum'><span><span class='pv'>{rtxt}</span></span></div>",
                     unsafe_allow_html=True)
        if ps2.button(t("Endre"), use_container_width=True):
            ss["pl_step"] = 1
            st.rerun()

        days = []
        _d = fr
        while _d <= to and len(days) < 10:
            days.append(_d)
            _d += timedelta(days=1)
        multi = len(days) > 1

        st.markdown(t("#### Hvor skal du?"))
        ss["w_start"] = st.text_input(t("Startsted (base / min posisjon)"), value=ss["w_start"],
                                      placeholder=t("F.eks. Lillehammer"), key="_w_start")
        if not multi:
            ss["w_til"] = st.text_input(t("Til · destinasjon (valgfritt)"), value=ss["w_til"],
                                        placeholder=t("F.eks. Oslo"), key="_w_til",
                                        help=t("Fyll ut for drop-in langs veien. "
                                             "Tomt = området rundt startstedet."))
            st.caption(t("Mellomstopp underveis (valgfritt):"))
            for i in range(len(ss["w_vias"])):
                mc1, mc2 = st.columns([6, 1])
                ss["w_vias"][i] = mc1.text_input(f"Mellomstopp {i + 1}", value=ss["w_vias"][i],
                                                 key=f"_w_via_{i}", label_visibility="collapsed",
                                                 placeholder=t("F.eks. Gjøvik"))
                if mc2.button(t("✕"), key=f"w_via_del_{i}", use_container_width=True):
                    ss["w_vias"].pop(i)
                    st.rerun()
            if st.button(t("Legg til mellomstopp"), use_container_width=True):
                ss["w_vias"].append("")
                st.rerun()
            ss["w_retur"] = st.checkbox(t("Tur-retur – hjem igjen til start samme dag"),
                                        value=ss["w_retur"], key="_w_retur")
            st.caption(t("Med destinasjon foreslås drop-in langs veien (og hjemveien ved tur-retur). "
                       "Uten destinasjon foreslås drop-in rundt startstedet."))
        else:
            st.radio(t("Reisemåte"), ["base", "tour"], key="w_trip_mode", horizontal=True,
                     format_func=lambda x: {"base": t("Fast base (jobbe flere dager samme sted)"),
                                            "tour": t("Rundreise (nytt sted hver dag)")}[x])
            if ss["w_trip_mode"] == "base":
                ss["w_base"] = st.text_input(t("Reisemål · der du jobber flere dager"),
                                             value=ss["w_base"], key="_w_base",
                                             placeholder=t("F.eks. Trondheim"))
                _b = (ss["w_base"] or "").strip()
                if _b:
                    st.caption(t("Dag 1 er reisedagen") + f" {ss['w_start'] or 'start'} → {_b} "
                               + t("med bedrifter på veien. De øvrige dagene foreslås bedrifter rundt")
                               + f" {_b}. " + t("Du slipper å skrive") + f" {_b} " + t("på hver dag."))
                else:
                    st.caption(t("Skriv reisemålet én gang – det gjelder alle dagene."))
            else:
                st.markdown(t("##### Reiserute – én etappe per dag"))
                st.caption(t("Skriv hvor du ender hver dag (overnatting). Neste dag starter der. "
                           "Siste dag: sett sluttsted, f.eks. hjem."))
                if len(ss["w_legs"]) != len(days):
                    old = ss["w_legs"]
                    ss["w_legs"] = [old[i] if i < len(old) else "" for i in range(len(days))]
                prev = (ss["w_start"] or "").strip() or "start"
                for i, dy in enumerate(days):
                    st.markdown(f"**{t('Dag')} {i + 1} · {UKEDAGER[dy.weekday()].capitalize()} "
                                f"{dy.strftime('%d.%m')}**  "
                                f"<span class='bnc-sub'>— {t('fra')} {prev}</span>", unsafe_allow_html=True)
                    ss["w_legs"][i] = st.text_input(t("Til / overnatting dag") + f" {i + 1}",
                                                    value=ss["w_legs"][i], key=f"_w_leg_{i}",
                                                    placeholder=t("F.eks. Oslo"),
                                                    label_visibility="collapsed")
                    nxt = (ss["w_legs"][i] or "").strip()
                    prev = nxt if nxt else t("(samme sted)")
                st.caption(t("La en dag stå tom hvis du bare vil jobbe området rundt startstedet."))

        bk, nx = st.columns(2)
        if bk.button(t("←  Tilbake"), use_container_width=True):
            ss["pl_step"] = 1
            st.rerun()
        if nx.button(t("Neste: besøksvalg  →"), type="primary", use_container_width=True,
                     disabled=not (ss["w_start"] or "").strip()):
            ss["pl_step"] = 3
            st.rerun()

    # ------------------------------------------------- STEG 3 · BESØKSVALG -----
    else:
        fr = ss["w_from"]
        to = ss["w_to"] or ss["w_from"]
        _multi3 = (to - fr).days > 0
        if _multi3 and ss["w_trip_mode"] == "base" and (ss["w_base"] or "").strip():
            area = f"{ss['w_start'] or 'start'} → {ss['w_base'].strip()} (base, flere dager)"
        elif _multi3 and ss["w_trip_mode"] == "tour":
            area = f"{ss['w_start'] or 'start'} (rundreise)"
        else:
            area = (ss["w_start"] or "start") + (f" → {ss['w_til']}"
                                                 if (ss["w_til"] or "").strip()
                                                 else " (området rundt)")
        st.markdown(f"<div class='psum'><span><span class='pv'>{area}</span></span></div>",
                    unsafe_allow_html=True)

        st.markdown(t("#### Besøksvalg"))
        v1, v2 = st.columns(2)
        ss["w_antall"] = v1.number_input(t("Besøk per dag"), 1, 20, int(ss["w_antall"]), 1,
                                         key="_w_antall",
                                         help=t("Så mange forslag spres langs ruta. Kim tar de "
                                              "han rekker på veien."))
        ss["w_visit"] = v2.number_input(t("Minutter per besøk"), 10, 180, int(ss["w_visit"]), 5,
                                        key="_w_visit", help=t("Kun for anslått kjøretid."))

        if ss["w_all"]:
            bsum = t("Alle bransjer")
        elif ss["w_cat"]:
            bsum = ", ".join(t(c) for c in ss["w_cat"][:3]) + (" …" if len(ss["w_cat"]) > 3 else "")
        else:
            bsum = t("ingen valgt")
        with st.expander(t("Bransjer:") + f" {bsum}"):
            ss["w_all"] = st.checkbox(t("Alle aktuelle bransjer"), value=ss["w_all"])
            # Kun stabil `key`, ingen `default` – da re-monteres ikke widgeten ved hvert
            # klikk, nedtrekket holder seg åpent og flere bransjer kan velges etter hverandre.
            _cur = st.session_state.get("_w_cat")
            if _cur is None:
                st.session_state["_w_cat"] = [c for c in ss["w_cat"] if c in _kat_present]
            else:
                _valid = [c for c in _cur if c in _kat_present]   # rydd ved f.eks. landbytte
                if _valid != _cur:
                    st.session_state["_w_cat"] = _valid
            st.multiselect(t("…eller velg selv"), _kat_present, key="_w_cat",
                           format_func=t, disabled=ss["w_all"])
            ss["w_cat"] = list(st.session_state.get("_w_cat") or [])
            if current_land() == "SE":
                st.caption(t("Tips: Det svenske registeret (SCB) har ingen egen kode for "
                           "bilpleie, så flere bilpleie-/biltvätt-bedrifter ligger under "
                           "«Verksted». Velg også Verksted for å finne flere."))
        with st.expander(t("Flere valg")):
            if current_land() == "NO":
                _orgs = ["AS + ENK", "Kun AS", "Kun ENK"]
                ss["w_org"] = st.selectbox(t("Selskapsform"), _orgs, format_func=t,
                                           index=_orgs.index(ss["w_org"]) if ss["w_org"] in _orgs else 0,
                                           key="_w_org")
            else:
                # Svenske data har ikke AS/ENK slik norske har – ikke filtrer på selskapsform.
                ss["w_org"] = "AS + ENK"
            ss["w_max"] = st.slider(t("Maks avstand mellom besøk (km, uten destinasjon)"), 10, 300,
                                    int(ss["w_max"]), 10, key="_w_max")
            ss["w_det"] = st.slider(t("Maks omvei fra ruta (km)"), 2, 60,
                                    int(ss["w_det"]), key="_w_det")

        bk2, go2 = st.columns([1, 2])
        if bk2.button(t("←  Tilbake"), use_container_width=True):
            ss["pl_step"] = 2
            st.rerun()
        if go2.button(t("Lag ruteforslag"), type="primary", use_container_width=True):
            valgte = _kat_present if ss["w_all"] else ss["w_cat"]
            if not valgte:
                st.warning(t("Velg minst én bransje – eller huk av «Alle aktuelle bransjer»."))
            else:
                days = []
                _d = fr
                while _d <= to and len(days) < 10:
                    days.append(_d)
                    _d += timedelta(days=1)
                multi = len(days) > 1
                with st.spinner("Lager ruteforslag … henter bedrifter og geokoder adresser."):
                    if not multi:
                        vias = [v.strip() for v in ss["w_vias"] if v and v.strip()]
                        if (ss["w_til"] or "").strip():
                            places = [ss["w_start"]] + vias + [ss["w_til"]]
                            status, msg, plans = plan_legs(
                                [{"date": days[0], "places": places, "retur": ss["w_retur"]}],
                                ss["w_hours"], ss["w_visit"], valgte, ss["w_org"], ss["w_det"],
                                int(ss["w_antall"]), area_km=ss["w_max"])
                        else:
                            status, msg, plans = plan_area(days, ss["w_start"], ss["w_hours"],
                                                           ss["w_visit"], valgte, ss["w_org"],
                                                           ss["w_max"], int(ss["w_antall"]))
                    elif ss["w_trip_mode"] == "base" and (ss["w_base"] or "").strip():
                        base = ss["w_base"].strip()
                        start0 = (ss["w_start"] or "").strip() or base
                        legs = []
                        for i, dy in enumerate(days):
                            frm = start0 if i == 0 else base
                            legs.append({"date": dy, "places": [frm, base], "retur": False})
                        status, msg, plans = plan_legs(legs, ss["w_hours"], ss["w_visit"],
                                                       valgte, ss["w_org"], ss["w_det"],
                                                       int(ss["w_antall"]), area_km=ss["w_max"])
                    else:
                        legs, prev = [], (ss["w_start"] or "").strip()
                        for i, dy in enumerate(days):
                            dest = (ss["w_legs"][i] if i < len(ss["w_legs"]) else "").strip()
                            if dest:
                                legs.append({"date": dy, "places": [prev, dest], "retur": False})
                                prev = dest
                        if legs:
                            status, msg, plans = plan_legs(legs, ss["w_hours"], ss["w_visit"],
                                                           valgte, ss["w_org"], ss["w_det"],
                                                           int(ss["w_antall"]), area_km=ss["w_max"])
                        else:
                            status, msg, plans = plan_area(days, ss["w_start"], ss["w_hours"],
                                                           ss["w_visit"], valgte, ss["w_org"],
                                                           ss["w_max"], int(ss["w_antall"]))
                if status == "ok":
                    ss["pl_preview"] = plans
                    ss["pl_review"] = True
                    ss["pv_focus"] = None
                    st.rerun()
                elif status == "tom":
                    st.warning(msg)
                else:
                    st.error(msg)

    upcoming = ([p for p in allplans if p["plan_date"] >= today.isoformat()]
                if ss["pl_step"] == 1 else [])
    if ss["pl_step"] == 1:
        st.divider()
        kh1, kh2 = st.columns([3, 1])
        kh1.markdown(t("#### Kommende ruter"))
        if upcoming and kh2.button(t("Tøm alle"), key="del_all_plans", use_container_width=True,
                                   help=t("Slett alle kommende planlagte ruter")):
            try:
                sb_delete("planned_routes", {"user_email": f"eq.{email}",
                                             "plan_date": f"gte.{today.isoformat()}"})
                planned_rows.clear()
                st.session_state["_toast"] = t("Alle kommende ruter slettet.")
                st.rerun()
            except Exception as exc:
                st.error(f"{exc}")
        if not upcoming:
            st.caption(t("Ingen kommende ruter ennå – planlegg en rute over."))
        if len(upcoming) > 1:
            tot_km_all = round(sum(float((p.get("meta") or {}).get("total_km") or 0)
                                   for p in upcoming), 1)
            n_all = sum(len(p.get("companies") or []) for p in upcoming)
            st.markdown(f"**{t('Hele turen')}** — {len(upcoming)} {t('dager')} · {n_all} {t('forslag')} · "
                        f"≈ {tot_km_all} km")
            leaflet_multi_html(upcoming)
            st.write("")
    for p in upcoming:
        d = p["plan_date"]
        comps = p.get("companies") or []
        try:
            dt = datetime.fromisoformat(d).date()
            tittel = f"{UKEDAGER[dt.weekday()].capitalize()} {dt.strftime('%d.%m.%Y')} — {len(comps)} {t('forslag')}"
        except Exception:
            dt, tittel = None, f"{d} — {len(comps)} {t('forslag')}"
        pstart = p.get("start")
        pmeta = p.get("meta") or {}
        with st.expander(tittel):
            pm = int(pmeta.get("total_min", 0))
            way = pmeta.get("way") or []
            if way:
                arrow = " → ".join(way) + (f" → {way[0]}" if pmeta.get("retur") else "")
                st.caption(f"{arrow}")
            st.caption(f"≈ {pmeta.get('total_km', 0)} km · {pm // 60}t {pm % 60}min " + t("kjøring")
                       + (f" · {len(comps)} {t('forslag')}" if comps else " · " + t("ingen forslag ennå")))
            pdest = pmeta.get("dest")
            rpts = pmeta.get("route_pts")
            if pstart and pstart.get("lat") is not None:
                leaflet_route_html(pstart, comps, dest=pdest, route_pts=rpts)
            g_url = gmaps_route_url(pstart, comps, dest=pdest) if pstart else None
            if g_url:
                st.link_button(t("Åpne ruta i Google Maps"), g_url, use_container_width=True)
            if comps:
                st.caption(t("Forslag til drop-in (valgfritt) — Bytt = annen bedrift · "
                           "✕ fjern fra ruta · Skjul = permanent:"))
            for s in comps:
                org = str(s.get("orgnr"))
                rc = st.columns([4, 1.2, 0.9, 1.3])
                rt = s.get("retning")
                badge = (f"<span class='bnc-badge'>{rt}</span> " if rt else "")
                dkm = s.get("detour_km")
                extra = (f" <span class='bnc-sub'>· omvei {dkm} km (+{s.get('extra_min','?')} min)</span>"
                         if dkm is not None else "")
                rc[0].markdown(
                    f"**{s.get('stop','')}. {s.get('navn','')}** "
                    f"<span class='bnc-sub'>({s.get('poststed','')})</span><br>"
                    f"{badge}<span class='bnc-sub'>{s.get('kategori','')}</span>{extra}",
                    unsafe_allow_html=True)
                if rc[1].button(t("Bytt"), key=f"plr_{d}_{org}",
                                help=t("Bytt ut med en annen bedrift i nærheten"),
                                use_container_width=True):
                    try:
                        nyc = alt_company(pmeta, comps, org)
                        if nyc and dt is not None:
                            newc = [dict(nyc) if str(x.get("orgnr")) == org else x for x in comps]
                            for i, x in enumerate(newc):
                                x["stop"] = i + 1
                            _save_recompute(email, dt, pstart, newc, pmeta)
                            st.session_state["_toast"] = t("Byttet til") + f" {nyc.get('navn','')}."
                        else:
                            st.session_state["_toast"] = t("Fant ingen annen passende bedrift i nærheten.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{exc}")
                if rc[2].button(t("✕"), key=f"plx_{d}_{org}",
                                help=t("Fjern fra denne ruta (kan dukke opp igjen senere)"),
                                use_container_width=True):
                    try:
                        rest = [x for x in comps if str(x.get("orgnr")) != org]
                        for i, x in enumerate(rest):
                            x["stop"] = i + 1
                        if dt is not None:
                            _save_recompute(email, dt, pstart, rest, pmeta)
                        st.session_state["_toast"] = f"{s.get('navn','')} " + t("fjernet fra ruta.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{exc}")
                if rc[3].button(t("Skjul"), key=f"plb_{d}_{org}",
                                help=t("Ikke aktuell – skjul permanent fra alle framtidige forslag"),
                                use_container_width=True):
                    try:
                        exclude_company(email, s)
                        rest = [x for x in comps if str(x.get("orgnr")) != org]
                        for i, x in enumerate(rest):
                            x["stop"] = i + 1
                        if dt is not None:
                            _save_recompute(email, dt, pstart, rest, pmeta)
                        st.session_state["_toast"] = f"{s.get('navn','')} " + t("skjult permanent.")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{exc}")
            if st.button(t("Slett ruten"), key=f"delp_{d}"):
                try:
                    delete_plan(email, d)
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")


# ============================================================= HISTORIKK ======
if nav == "Historikk":
    page_header(t("Historikk"), t("Dine besøk og klimaavtrykk"))
    periode = st.radio(t("Periode"), ["Denne måneden", "Hittil i år", "Alt"], horizontal=True, format_func=t)
    params = {"user_email": f"eq.{email}", "order": "visited_on.desc"}
    if periode == "Denne måneden":
        params["month_key"] = f"eq.{month_key()}"
    elif periode == "Hittil i år":
        params["visited_on"] = f"gte.{date.today().year}-01-01"
    try:
        rows = sb_get("visits", "*", params)
    except Exception as exc:
        rows = []
        st.error(t("Kunne ikke hente:") + f" {exc}")
    tot_km = round(sum(float(r.get("leg_km") or 0) for r in rows), 1)
    tot_co2 = round(sum(float(r.get("co2_g") or 0) for r in rows) / 1000.0, 1)
    m1, m2, m3 = st.columns(3)
    m1.markdown(f"<div class='bnc-stat'><div class='k'>{t('Besøk')}</div>"
                f"<div class='v'>{len(rows)}</div></div>", unsafe_allow_html=True)
    m2.markdown(f"<div class='bnc-stat'><div class='k'>{t('Kjørt (km)')}</div>"
                f"<div class='v'>{tot_km}</div></div>", unsafe_allow_html=True)
    m3.markdown(f"<div class='bnc-stat'><div class='k'>{t('Klimaavtrykk')}</div>"
                f"<div class='v'>{tot_co2} <small>kg CO₂e</small></div></div>", unsafe_allow_html=True)
    st.caption(t("Kjøring summeres fra avstanden til hvert registrerte besøk (anslag for "
               "konsernrapportering). Besøk søkt opp manuelt teller 0 km."))
    st.write("")
    _cls = {"Interessert": "b-ok", "Solgt": "b-ok", "Vil tenke": "b-warn",
            "Ikke til stede": "b-no", "Ikke interessert": "b-no"}
    for r in rows:
        cls = _cls.get(r.get("outcome", ""), "")
        with st.expander(f"{r.get('navn','')}  ·  {t(r.get('outcome',''))}  ·  {r.get('visited_on','')}"):
            st.markdown(f"<span class='bnc-badge {cls}'>{t(r.get('outcome',''))}</span> "
                        f"<span class='bnc-sub'>{r.get('visited_on','')} · {r.get('kommune','')}</span>",
                        unsafe_allow_html=True)
            st.write(r.get("report") or t("_Ingen rapporttekst._"))
            if r.get("contact_email"):
                st.caption(t("Takk-e-post til:") + f" {r.get('contact_email')}")
            he1, he2 = st.columns(2)
            if he1.button(t("Rediger"), key=f"hedit_{r.get('id')}", use_container_width=True):
                edit_visit_dialog(r)
            if he2.button(t("Angre / slett besøk"), key=f"hdel_{r.get('id')}",
                          use_container_width=True):
                try:
                    sb_delete("visits", {"id": f"eq.{r.get('id')}"})
                    user_co2_totals.clear()
                    st.session_state["_toast"] = t("Besøk slettet.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")
    if not rows:
        st.info(t("Ingen besøk i perioden") + f" «{t(periode)}».")


# =========================================================== INNSTILLINGER ====
if nav == "Innstillinger":
    page_header(t("Innstillinger"), t("Profil og standardverdier for planlegging"))
    with st.container(border=True):
        st.markdown(t("**Søkeområde**"))
        land_picker("land_set", label_visibility="collapsed",
                    help=t("Velg hvilket land bedriftssøk og ruteplanlegging bruker."))
        _n_land = len(companies)
        st.caption(f"{_n_land:,} {t('bedrifter i')} {LAND_NAVN.get(current_land())}."
                   .replace(",", " "))
    with st.container(border=True):
        st.markdown(t("**Språk**"))
        _langs = ["no", "sv"]
        _cl = current_lang()
        _sel_lang = st.radio(
            t("**Språk**"), _langs, index=_langs.index(_cl) if _cl in _langs else 0,
            key="_lang_pick", horizontal=True, label_visibility="collapsed",
            format_func=lambda x: {"no": "Norsk", "sv": "Svenska"}[x])
        if _sel_lang != _cl:
            st.session_state["lang"] = _sel_lang
            try:
                st.query_params["lang"] = _sel_lang
            except Exception:
                pass
            # best-effort varig lagring (virker hvis kolonnen «lang» finnes – ellers ignoreres)
            try:
                sb_upsert("salesperson_settings",
                          {"email": email, "lang": _sel_lang,
                           "updated_at": datetime.now(timezone.utc).isoformat()}, "email")
            except Exception:
                pass
            # nullstill menyvalg lagret med gammel språketikett, og bli værende her
            for _nk in ("nav_menu", "bnav_menu", "_last_bnav"):
                st.session_state.pop(_nk, None)
            st.session_state["_goto"] = "Innstillinger"
            st.rerun()
    with st.form("settings"):
        c1, c2 = st.columns(2)
        with c1:
            with st.container(border=True):
                st.markdown(t("**Selgerprofil**"))
                name = st.text_input(t("Navn"), value=_settings.get("name", ""))
                phone = st.text_input(t("Telefon"), value=_settings.get("phone", ""), placeholder=t("+47 …"),
                                      help=t("Settes automatisk inn i takk-e-posten ({telefon})."))
                summary_email = st.text_input(t("E-post for ukesoppsummering"),
                                              value=_settings.get("summary_email", "") or email,
                                              help=t("Hit sendes fredagens ukesoppsummering."))
        with c2:
            with st.container(border=True):
                st.markdown(t("**Standard for planlegging**"))
                start_label = st.text_input(t("Fast startposisjon (adresse)"),
                                            value=_settings.get("start_label", ""),
                                            placeholder=t("Valgfritt – kan overstyres på «Min dag»"))
                cc1, cc2 = st.columns(2)
                visit_min = cc1.number_input(t("Snitt pr. besøk (min)"), 10, 180,
                                             int(_settings.get("avg_visit_minutes") or 60), 5,
                                             help=t("Standardverdi i planleggingen og for "
                                                  "«Beregnet tid»-anslaget."))
                speed = cc2.number_input(t("Snittfart bil (km/t)"), 30, 110,
                                         int(_settings.get("speed_kmh") or 70), 5,
                                         help=t("Brukes til å anslå kjøretid."))
                _co2d = _settings.get("co2_g_per_km")
                co2 = st.number_input(t("Utslipp (g CO₂e/km)"), 0, 400,
                                      int(_co2d) if _co2d not in (None, "") else 0, 1,
                                      help=t("For konsernrapportering. Helelektrisk (f.eks. BMW iX1) "
                                           "= 0. Ladbar hybrid ~60, bensin ~165, diesel ~170."))
                hours = float(_settings.get("hours_per_day") or 8)
        lagre = st.form_submit_button(t("Lagre innstillinger"), type="primary")
    if lagre:
        lat, lon, label = (_settings.get("start_lat"), _settings.get("start_lon"), start_label)
        if start_label:
            _la, _lo, _lb = geocode(start_label)
            if not _la:
                # Basen kan ligge i et annet land enn søkeområdet – prøv det andre også.
                _la, _lo, _lb = geocode(start_label, "NO" if current_land() == "SE" else "SE")
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
            st.success(t("Lagret.") + (f" {t('Startposisjon:')} {label}." if lat else
                                       t(" (Fant ikke adressen – startposisjon ikke satt.)")))
        except Exception as exc:
            st.error(t("Kunne ikke lagre:") + f" {exc}")

    st.divider()
    with st.expander(t("Ekskluderte bedrifter (skjult fra forslag)")):
        try:
            ex = sb_get("excluded", "orgnr,navn", {"user_email": f"eq.{email}", "order": "navn.asc"})
        except Exception:
            ex = []
        if not ex:
            st.caption(t("Ingen bedrifter er ekskludert."))
        else:
            if st.button(f"Angre alle ({len(ex)})", key="unx_all",
                         help=t("Fjern alle fra ekskludert-lista – de kan foreslås igjen")):
                try:
                    sb_delete("excluded", {"user_email": f"eq.{email}"})
                    st.session_state["_toast"] = t("Alle ekskluderte bedrifter er gjenåpnet.")
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")
        for e in ex:
            xc = st.columns([5, 2])
            xc[0].write(f"{e.get('navn','')}  ({e.get('orgnr','')})")
            if xc[1].button(t("Angre"), key=f"unx_{e.get('orgnr')}", use_container_width=True):
                try:
                    unexclude(email, e.get("orgnr"))
                    st.rerun()
                except Exception as exc:
                    st.error(f"{exc}")

    st.divider()
    st.caption(t("Du kommer inn via din personlige lenke. Legg appen til på Hjem-skjerm på "
               "mobilen (del-knappen → «Legg til på Hjem-skjerm») – da åpner snarveien rett inn."))


# =============================================================== BRUKERE =======
if nav == "Brukere" and is_admin:
    import secrets as _secrets
    page_header(t("Brukere"), t("Opprett selgere og gi dem personlig innloggingslenke"))

    with st.expander(t("Klimaavtrykk pr. selger"), expanded=True):
        perc = st.radio(t("Periode"), ["Denne måneden", "Hittil i år", "Alt"],
                        horizontal=True, key="adm_co2_per", format_func=t)
        prm = {"user_email": "not.is.null"}
        if perc == "Denne måneden":
            prm["month_key"] = f"eq.{month_key()}"
        elif perc == "Hittil i år":
            prm["visited_on"] = f"gte.{date.today().year}-01-01"
        try:
            vr = sb_get("visits", "user_email,co2_g,leg_km,visited_on,month_key", prm)
        except Exception as exc:
            vr = []
            st.error(t("Kunne ikke hente:") + f" {exc}")
        names = {}
        try:
            for u in sb_get("salesperson_settings", "email,name"):
                names[u["email"]] = u.get("name") or u["email"]
        except Exception:
            pass
        agg = {}
        for r in vr:
            a = agg.setdefault(r.get("user_email", "?"), {"n": 0, "km": 0.0, "co2": 0.0})
            a["n"] += 1
            a["km"] += float(r.get("leg_km") or 0)
            a["co2"] += float(r.get("co2_g") or 0)
        if not agg:
            st.caption(t("Ingen registrerte besøk i perioden."))
        else:
            a1, a2, a3 = st.columns(3)
            a1.markdown(f"<div class='bnc-stat'><div class='k'>{t('Selgere')}</div>"
                        f"<div class='v'>{len(agg)}</div></div>", unsafe_allow_html=True)
            a2.markdown(f"<div class='bnc-stat'><div class='k'>{t('Kjørt (km)')}</div>"
                        f"<div class='v'>{round(sum(a['km'] for a in agg.values()), 1)}</div></div>",
                        unsafe_allow_html=True)
            a3.markdown(f"<div class='bnc-stat'><div class='k'>{t('Totalt CO₂e')}</div>"
                        f"<div class='v'>{round(sum(a['co2'] for a in agg.values()) / 1000, 1)} "
                        f"<small>kg</small></div></div>", unsafe_allow_html=True)
            st.write("")
            for u, a in sorted(agg.items(), key=lambda x: -x[1]["co2"]):
                st.markdown(f"- **{names.get(u, u)}** — {a['n']} {t('besøk')} · {round(a['km'], 1)} km · "
                            f"**{round(a['co2'] / 1000, 1)} kg CO₂e**")

    def personal_link(u):
        tok = u.get("login_token")
        if not tok:
            return None
        qr = urllib.parse.urlencode({"u": u["email"], "k": tok})
        return f"{APP_BASE_URL}/?{qr}" if APP_BASE_URL else f"?{qr}"

    c1, c2 = st.columns([1, 2])
    with c1:
        with st.container(border=True):
            st.markdown(t("**Ny bruker**"))
            with st.form("ny_bruker"):
                bn = st.text_input(t("Navn"), placeholder=t("Kim Werner Strand"))
                be = st.text_input(t("E-post"), placeholder=t("kim@bncnordic.no")).strip().lower()
                bl = st.radio(t("Språk (e-post + app)"), ["no", "sv"], horizontal=True, key="nb_lang",
                              format_func=lambda x: {"no": "Norsk", "sv": "Svenska"}[x])
                st.caption(t("Selgerens språk styrer appen og språket/avsenderen på utgående e-post."))
                if st.form_submit_button(t("Opprett / ny lenke"), type="primary", use_container_width=True):
                    if be:
                        try:
                            upsert_settings({
                                "email": be, "name": bn, "lang": bl,
                                "login_token": _secrets.token_urlsafe(12),
                                "updated_at": datetime.now(timezone.utc).isoformat()})
                            st.success(t("Bruker klar – lenken vises til høyre."))
                        except Exception as exc:
                            st.error(t("Kunne ikke lagre:") + f" {exc}")
                    else:
                        st.error(t("Skriv inn e-post."))
    with c2:
        if not APP_BASE_URL:
            st.warning(t("Legg **APP_BASE_URL** i Streamlit Secrets (appens adresse) for komplette lenker."))
        try:
            users = sb_get("salesperson_settings", "email,name,login_token,lang", {"order": "email.asc"})
        except Exception:
            try:
                users = sb_get("salesperson_settings", "email,name,login_token", {"order": "email.asc"})
            except Exception as exc:
                users = []
                st.error(t("Kunne ikke hente brukere:") + f" {exc}")
        for u in users:
            with st.container(border=True):
                tag = " · admin" if u["email"] in ADMIN_EMAILS else ""
                st.markdown(f"**{u.get('name') or t('(uten navn)')}** — {u['email']}{tag}")
                link = personal_link(u)
                if link:
                    st.code(link, language=None)
                else:
                    st.caption(t("Ingen lenke ennå – trykk «Ny lenke»."))
                _uc = "sv" if (u.get("lang") or "no") == "sv" else "no"
                _us = st.radio(t("Språk (e-post + app)"), ["no", "sv"],
                               index=1 if _uc == "sv" else 0, key=f"ul_{u['email']}",
                               horizontal=True, format_func=lambda x: {"no": "Norsk", "sv": "Svenska"}[x])
                if _us != _uc:
                    try:
                        upsert_settings({"email": u["email"], "lang": _us,
                                         "updated_at": datetime.now(timezone.utc).isoformat()})
                        st.rerun()
                    except Exception as exc:
                        st.error(t("Kunne ikke lagre:") + f" {exc}")
                bc = st.columns(2)
                if bc[0].button(t("Ny lenke"), key=f"nl_{u['email']}", use_container_width=True):
                    try:
                        sb_upsert("salesperson_settings", {
                            "email": u["email"], "login_token": _secrets.token_urlsafe(12),
                            "updated_at": datetime.now(timezone.utc).isoformat()}, "email")
                        st.rerun()
                    except Exception as exc:
                        st.error(f"{exc}")
                if u["email"] not in ADMIN_EMAILS:
                    if bc[1].button(t("Steng lenke"), key=f"rv_{u['email']}", use_container_width=True):
                        try:
                            sb_upsert("salesperson_settings", {
                                "email": u["email"], "login_token": None,
                                "updated_at": datetime.now(timezone.utc).isoformat()}, "email")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"{exc}")


# ========================================================= E-POSTINNHOLD =======
if nav == "E-postinnhold" and is_admin:
    page_header(t("E-postinnhold"), t("Rediger de automatiske e-postene. Selgerne ser ikke denne siden."))
    _ec_langs = ["no", "sv"]
    ec_lang = st.radio(t("Språk for malene"), _ec_langs, horizontal=True, key="ec_lang",
                       format_func=lambda x: {"no": "Norsk", "sv": "Svenska"}[x])
    if ec_lang == "sv":
        st.caption(t("Svenske e-poster sendes fra info@bncnordic.se via den svenske Brevo-kontoen."))
    for kind in ("thankyou", "summary", "report"):
        d = default_content(kind, ec_lang)
        cur = get_content(kind, ec_lang)
        with st.expander(t(d["label"]), expanded=(kind == "thankyou")):
            with st.form(f"ec_{kind}_{ec_lang}"):
                subj = st.text_input(t("Emne"), value=cur["subject"])
                body = st.text_area(t("Tekst"), value=cur["body"], height=260)
                st.caption(t("Variabler som fylles automatisk:") + f" {d['vars']}")
                if st.form_submit_button(t("Lagre"), type="primary"):
                    try:
                        sb_upsert("email_content", {
                            "kind": kind, "lang": ec_lang, "subject": subj, "body": body,
                            "updated_at": datetime.now(timezone.utc).isoformat()}, "kind,lang")
                        st.success(t("Lagret."))
                    except Exception as exc:
                        st.error(t("Kunne ikke lagre:") + f" {exc}")

    st.divider()
    with st.expander(t("Send testmail (kontroll før lansering)")):
        st.caption(t("Legger en test i kø og sendes av E-postjobben (kjør den i GitHub Actions "
                   "eller vent inntil 1 time)."))
        tt_to = st.text_input(t("Send test til"), value=email, key="tt_to")
        tt_kind = st.selectbox(t("Type"), ["thankyou", "summary", "report"],
                               format_func=lambda k: t(default_content(k, ec_lang)["label"]), key="tt_kind")
        cur = get_content(tt_kind, ec_lang)
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
        st.caption(t("Forhåndsvisning:"))
        st.code(tsub, language=None)
        st.text(tbody)
        if st.button(t("Legg testmail i kø")):
            try:
                enqueue_email({
                    "kind": tt_kind, "to_email": tt_to.strip(), "lang": ec_lang,
                    "subject": "[TEST] " + tsub, "body": tbody,
                    "send_after": date.today().isoformat(), "payload": {"test": True}})
                st.success(t("Testmail lagt i kø til") + f" {tt_to.strip()}. "
                           + t("Kjør «E-postjobb» i GitHub Actions for å sende nå."))
            except Exception as exc:
                st.error(t("Kunne ikke legge i kø:") + f" {exc}")


# ====================================================== MOBIL: TOPP + BUNNMENY =
# Vises bare på mobil (skjult på desktop via CSS). Sidemenyen skjules på mobil.
with st.container(key="bnc_topbar"):
    tb = st.columns([5, 2])
    with tb[0]:
        _logo(LOGO_WHITE, 92)
    if tb[1].button(t("Logg ut"), key="top_out", use_container_width=True):
        st.query_params.clear()
        for _k in list(st.session_state.keys()):
            st.session_state.pop(_k, None)
        st.rerun()

with st.container(key="bnc_bnav"):
    _bpages = ["Min dag", "Drop-in", "Planlegging", "Historikk", "Innstillinger"]
    _blabels = [t(p) for p in _bpages]
    if HAS_OPTION_MENU:
        _bidx = _bpages.index(nav) if nav in _bpages else 0
        _bsel = option_menu(
            None, _blabels, icons=[PAGE_ICONS[p] for p in _bpages],
            orientation="horizontal", default_index=_bidx, key="bnav_menu",
            styles={
                "container": {"background-color": "#ffffff", "padding": "0!important"},
                "nav-link": {"color": "#8892a4", "font-size": "9px", "font-weight": "600",
                             "text-align": "center", "margin": "0", "padding": "5px 1px",
                             "line-height": "1.15", "--hover-color": "#eef2f8"},
                "nav-link-selected": {"background-color": "#eef2f8", "color": "#1b2a4a"},
                "icon": {"font-size": "17px", "display": "block", "margin": "0 0 2px 0"},
            })
        _bselpage = _bpages[_blabels.index(_bsel)] if _bsel in _blabels else nav
        if _bsel != st.session_state.get("_last_bnav"):
            st.session_state["_last_bnav"] = _bsel
            if _bselpage != nav:
                st.session_state["_goto"] = _bselpage
                st.rerun()
    else:
        _bc = st.columns(len(_bpages))
        for _i, _pg in enumerate(_bpages):
            if _bc[_i].button(t(_pg), key=f"bn_{_pg}", use_container_width=True,
                              type=("primary" if nav == _pg else "secondary")):
                st.session_state["_goto"] = _pg
                st.rerun()
