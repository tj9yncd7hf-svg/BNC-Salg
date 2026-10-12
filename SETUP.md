# BNC Selger – besøksrute-app

En egen app (ikke en del av Ditec-appen) for en BNC-selger på veien. Kim setter
startposisjon + timer på veien, og appen foreslår en kjørerute med bedrifter fra
Brønnøysund (AS, utvalgte bransjer, ikke konkurs). Besøk hukes av, rapport sendes
til backoffice, og «takk»-e-post + ukesoppsummering går automatisk.

## Funksjoner
- **Min dag:** startposisjon + timer + snitt-tid pr. besøk → foreslått rute (kart + liste) innenfor tiden.
- **Besøk:** registrer besøk → faller ut av forslag resten av **inneværende måned**; rapport sendes til `post@bncnordic.no`.
- **Takk for besøket:** auto-e-post til kunden **2 virkedager** etter besøket (hopper over helg: tirsdag→torsdag, fredag→tirsdag).
- **Ukesoppsummering:** e-post til selger med ukas besøk (fredag).
- **Historikk:** egne besøk denne måneden.

## Teknisk
- **Streamlit** (mobilvennlig i nettleser – «Legg til på Hjem-skjerm» gir app-ikon)
- **Supabase** (besøk, innstillinger, e-postkø) – bedriftslista ligger som datafil (`data/companies.json`, 5 916 bedrifter geokodet via Kartverket)
- **Ruting:** luftlinje × omvei-faktor / snittfart (enkel, rask – kan byttes til ekte kjøretid senere i `routing.py`)
- **E-post:** SMTP via GitHub Actions-jobber (`scripts/email_worker.py` + `weekly_summary.py`)

## Oppsett (engangs)

### 1. Nytt GitHub-repo
Lag et nytt, privat repo (f.eks. `bnc-selger`) og legg inn alle filene i denne pakka.

### 2. Supabase
- Opprett (eller gjenbruk) et Supabase-prosjekt.
- SQL Editor → lim inn **hele** `migrations/schema.sql` → **Run without RLS**.

### 3. Streamlit Community Cloud
- Ny app → pek på repoet og `app.py`.
- Under **Secrets**, legg inn:
  ```
  SUPABASE_URL = "https://xxxx.supabase.co"
  SUPABASE_SECRET_KEY = "…service-nøkkel…"
  BACKOFFICE_EMAIL = "post@bncnordic.no"
  ```

### 4. GitHub Secrets (for e-postjobbene)
I repoets **Settings → Secrets and variables → Actions**:
```
SUPABASE_URL, SUPABASE_SECRET_KEY
SMTP_HOST, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_USE_TLS
MAIL_FROM_EMAIL, MAIL_FROM_NAME
```
Workflowene kjører da automatisk: **E-postjobb** (hver time) og **Ukesoppsummering** (fredag). Begge kan kjøres manuelt via Actions-fanen.

## Slik bruker Kim appen
1. Skriv inn e-posten din øverst (identifiserer brukeren).
2. **Innstillinger:** sett navn, fast startposisjon, timer/dag, snitt pr. besøk.
3. **Min dag:** juster startposisjon/timer → **Foreslå rute** → se kart + liste.
4. Tapp **Registrer besøk** på et stopp → velg utfall, skriv rapport, sett kundens e-post → **Lagre**.
5. Rapport går til backoffice, takk-e-post legges i kø (2 virkedager etter), og bedriften vises ikke igjen denne måneden.

## Data
`data/companies.json` er bygget fra Brønnøysund-eksporten (bransjekoder 46.710,
46.720, 47.810, 75.320, 95.310), filtrert på **AS** + **ikke konkurs/avvikling**,
og geokodet med Kartverket. 5 916 bedrifter med koordinater. For å oppdatere
listen senere: kjør en ny Brønnøysund-eksport og geokod på nytt.

## Videre (mulige steg 2)
- Ekte kjøretid (OSRM/openrouteservice) i `routing.py`.
- Innlogging/passord (nå identifiseres selger kun på e-post).
- «15 besøk»-trigger for oppsummering i tillegg til fredag.
- Retning/område-styring av ruten.
