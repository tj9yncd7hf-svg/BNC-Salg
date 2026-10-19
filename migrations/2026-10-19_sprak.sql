-- Språkstøtte (NO/SV) for BNC Selger.
-- Kjør HELE fila i Supabase SQL Editor. Trygg å kjøre flere ganger.

-- 1) Per-selger språk. Styrer appen + språket/avsenderen på utgående e-post.
alter table public.salesperson_settings add column if not exists lang text default 'no';

-- 2) E-postkøen bærer språket, så e-postjobben velger riktig avsender
--    (svensk = info@bncnordic.se via den svenske Brevo-kontoen).
alter table public.email_queue add column if not exists lang text default 'no';

-- 3) E-postmaler pr. språk: utvid nøkkelen fra (kind) til (kind, lang),
--    slik at det finnes egne norske og svenske maler.
alter table public.email_content add column if not exists lang text default 'no';
update public.email_content set lang = 'no' where lang is null;

do $$
begin
    -- bytt primærnøkkel bare hvis den fortsatt er kun (kind)
    if exists (
        select 1 from pg_constraint
        where conrelid = 'public.email_content'::regclass and contype = 'p'
          and array_length(conkey, 1) = 1
    ) then
        alter table public.email_content drop constraint email_content_pkey;
        alter table public.email_content add primary key (kind, lang);
    end if;
end $$;
