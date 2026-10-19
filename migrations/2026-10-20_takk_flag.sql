-- Markerer om «takk for praten»-e-post faktisk ble bedt om ved registrering.
-- Da kan Historikk skille mellom «kunden får takk-mail» og «e-post kun registrert
-- for oppfølging». Kjør i Supabase SQL Editor. Trygg å kjøre flere ganger.
alter table public.visits add column if not exists thanks_sent boolean default false;
