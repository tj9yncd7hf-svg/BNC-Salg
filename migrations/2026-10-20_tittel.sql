-- Tittel/stilling pr. selger – fylles inn i takk-e-posten via {tittel}.
-- Kjør i Supabase SQL Editor. Trygg å kjøre flere ganger.
alter table public.salesperson_settings add column if not exists title text default '';
