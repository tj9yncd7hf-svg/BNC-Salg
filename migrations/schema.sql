-- BNC Selger-app – databaseoppsett (Supabase/Postgres).
-- Kjør HELE fila i Supabase SQL Editor. GRANT-ene er nødvendige (ellers 403),
-- og «Run without RLS» (appen bruker service-nøkkelen + grants, ikke RLS).
--
-- Bedriftslista (geokodet fra Brønnøysund) ligger som datafil i appen
-- (data/companies.json) – kun besøk/innstillinger/e-postkø lagres her.

-- Innstillinger pr. selger (nøkkel = e-post).
create table if not exists public.salesperson_settings (
    email             text primary key,
    name              text default '',
    phone             text default '',          -- vises i takk-e-posten ({telefon})
    summary_email     text default '',          -- hvor ukesoppsummeringen sendes
    start_label       text default '',          -- adresse/tekst for startpunkt
    start_lat         double precision,
    start_lon         double precision,
    hours_per_day     double precision default 7,
    avg_visit_minutes double precision default 60,
    speed_kmh         double precision default 70,
    updated_at        timestamptz default now()
);

-- Registrerte besøk. month_key (YYYY-MM) brukes til å utelate besøkte bedrifter
-- fra forslag i inneværende måned.
create table if not exists public.visits (
    id            uuid primary key default gen_random_uuid(),
    user_email    text not null,
    orgnr         text not null,
    navn          text default '',
    kommune       text default '',
    visited_on    date not null default current_date,
    month_key     text not null,                 -- f.eks. '2026-10'
    report        text default '',
    outcome       text default '',               -- fritt: interessert / ikke hjemme / solgt ...
    contact_email text default '',               -- hvor «takk for besøket» skal sendes
    created_at    timestamptz default now()
);
create index if not exists visits_user_month on public.visits (user_email, month_key);
create index if not exists visits_orgnr on public.visits (orgnr);

-- E-postkø: «takk for besøket» (2 virkedager etter) + ukesoppsummering + rapport.
create table if not exists public.email_queue (
    id          uuid primary key default gen_random_uuid(),
    kind        text not null,                   -- 'thankyou' | 'summary' | 'report'
    to_email    text not null,
    subject     text default '',
    body        text default '',
    send_after  date not null default current_date,
    status      text default 'pending',          -- pending | sent | failed
    error       text default '',
    payload     jsonb default '{}'::jsonb,
    created_at  timestamptz default now(),
    sent_at     timestamptz
);
create index if not exists email_queue_due on public.email_queue (status, send_after);

-- E-postmaler pr. selger (for «takk for besøket»). Kim kan lage flere og
-- velge standard; valgt mal kopieres inn i e-postkøen ved registrering av besøk.
create table if not exists public.email_templates (
    id          uuid primary key default gen_random_uuid(),
    user_email  text not null,
    name        text not null,
    subject     text default '',
    body        text default '',
    is_default  boolean default false,
    updated_at  timestamptz default now()
);
create index if not exists email_templates_user on public.email_templates (user_email);

grant all on table public.salesperson_settings to anon, authenticated, service_role;
grant all on table public.visits               to anon, authenticated, service_role;
grant all on table public.email_queue          to anon, authenticated, service_role;
grant all on table public.email_templates      to anon, authenticated, service_role;
