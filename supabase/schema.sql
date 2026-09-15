-- CKR publication register schema for Supabase/PostgreSQL.
-- Run this once in the Supabase SQL editor.

create schema if not exists private;
revoke all on schema private from public;

create table if not exists public.publications (
  id text primary key,
  pmid text,
  title text not null,
  authors text[] not null default '{}',
  year text not null,
  journal text not null default '',
  abbreviation text not null default '',
  volume text not null default '',
  issue text not null default '',
  start_page text not null default '',
  end_page text not null default '',
  doi text not null default '',
  pmcid text not null default '',
  issn text not null default '',
  language text not null default '',
  affiliations text[] not null default '{}',
  publication_types text[] not null default '{}',
  publication_date text not null default '',
  electronic_dates text[] not null default '{}',
  corrections jsonb not null default '[]'::jsonb,
  projects text[] not null default '{}',
  programmes text[] not null default '{}',
  project_evidence jsonb not null default '{}'::jsonb,
  programme_evidence jsonb not null default '{}'::jsonb,
  projects_source text not null default 'suggested' check (projects_source in ('suggested', 'manual')),
  programmes_source text not null default 'suggested' check (programmes_source in ('suggested', 'manual')),
  source_keywords text[] not null default '{}',
  affiliation_review boolean not null default false,
  record_source text not null default 'PubMed',
  collection_checked_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  updated_by uuid references auth.users(id) on delete set null,
  constraint publications_pmid_numeric check (pmid is null or pmid = '' or pmid ~ '^[0-9]+$'),
  constraint publications_year_four_digits check (year ~ '^[0-9]{4}$')
);

create unique index if not exists publications_pmid_unique
  on public.publications (pmid) where pmid is not null and pmid <> '';
create index if not exists publications_year_idx on public.publications (year desc);
create index if not exists publications_projects_idx on public.publications using gin (projects);
create index if not exists publications_programmes_idx on public.publications using gin (programmes);

create table if not exists private.publication_editors (
  user_id uuid primary key references auth.users(id) on delete cascade,
  label text,
  created_at timestamptz not null default now()
);

create table if not exists public.publication_history (
  history_id bigint generated always as identity primary key,
  publication_id text not null,
  operation text not null check (operation in ('INSERT', 'UPDATE', 'DELETE')),
  changed_at timestamptz not null default now(),
  changed_by uuid references auth.users(id) on delete set null,
  old_record jsonb,
  new_record jsonb
);
create index if not exists publication_history_publication_idx
  on public.publication_history (publication_id, changed_at desc);

create or replace function public.is_publication_editor()
returns boolean
language sql
stable
security invoker
set search_path = ''
as $$
  select (select auth.uid()) is not null and exists (
    select 1
    from private.publication_editors
    where user_id = (select auth.uid())
  );
$$;

create or replace function private.set_publication_audit_fields()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
begin
  new.updated_at := now();
  new.updated_by := (select auth.uid());
  if tg_op = 'INSERT' and new.created_at is null then
    new.created_at := now();
  end if;
  return new;
end;
$$;

create or replace function private.record_publication_history()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.publication_history (
    publication_id, operation, changed_by, old_record, new_record
  ) values (
    coalesce(new.id, old.id),
    tg_op,
    (select auth.uid()),
    case when tg_op in ('UPDATE', 'DELETE') then to_jsonb(old) else null end,
    case when tg_op in ('INSERT', 'UPDATE') then to_jsonb(new) else null end
  );
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end;
$$;

drop trigger if exists publications_set_audit_fields on public.publications;
create trigger publications_set_audit_fields
before insert or update on public.publications
for each row execute function private.set_publication_audit_fields();

drop trigger if exists publications_record_history on public.publications;
create trigger publications_record_history
after insert or update or delete on public.publications
for each row execute function private.record_publication_history();

alter table public.publications enable row level security;
alter table private.publication_editors enable row level security;
alter table public.publication_history enable row level security;

revoke all on public.publications from anon, authenticated;
revoke all on private.publication_editors from anon, authenticated;
revoke all on public.publication_history from anon, authenticated;
grant select on public.publications to anon, authenticated;
grant insert, update, delete on public.publications to authenticated;
grant usage on schema private to authenticated;
grant select on private.publication_editors to authenticated;
grant select on public.publication_history to authenticated;
revoke all on function public.is_publication_editor() from public;
revoke all on function private.set_publication_audit_fields() from public;
revoke all on function private.record_publication_history() from public;
grant execute on function public.is_publication_editor() to authenticated;

drop policy if exists "Publications are publicly readable" on public.publications;
create policy "Publications are publicly readable"
on public.publications for select
to anon, authenticated
using (true);

drop policy if exists "Editors can insert publications" on public.publications;
create policy "Editors can insert publications"
on public.publications for insert
to authenticated
with check ((select public.is_publication_editor()));

drop policy if exists "Editors can update publications" on public.publications;
create policy "Editors can update publications"
on public.publications for update
to authenticated
using ((select public.is_publication_editor()))
with check ((select public.is_publication_editor()));

drop policy if exists "Editors can delete publications" on public.publications;
create policy "Editors can delete publications"
on public.publications for delete
to authenticated
using ((select public.is_publication_editor()));

drop policy if exists "Editors can see their editor record" on private.publication_editors;
create policy "Editors can see their editor record"
on private.publication_editors for select
to authenticated
using (user_id = (select auth.uid()));

drop policy if exists "Editors can view publication history" on public.publication_history;
create policy "Editors can view publication history"
on public.publication_history for select
to authenticated
using ((select public.is_publication_editor()));

-- Supabase may create this helper when automatic RLS is enabled. It is an
-- administrative trigger function and should not be callable through the API.
do $do$
begin
  if to_regprocedure('public.rls_auto_enable()') is not null then
    execute 'revoke execute on function public.rls_auto_enable() from public, anon, authenticated';
  end if;
end;
$do$;

-- After creating an editor in Authentication > Users, approve that account with:
-- insert into private.publication_editors (user_id, label)
-- select id, email from auth.users where email = 'editor@example.org'
-- on conflict (user_id) do update set label = excluded.label;
