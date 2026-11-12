-- Telegram AI Operator: schema + migration
-- Выполнить целиком в Supabase SQL Editor.
-- Тексты переписок и содержимое сообщений здесь НЕ сохраняются.

create table if not exists public.operator_settings (
    key text primary key,
    value text not null
);

create table if not exists public.operator_allowed_users (
    telegram_user_id bigint primary key,
    username text not null default '',
    added_at timestamptz not null default now()
);

-- Миграция для уже существующей таблицы:
alter table public.operator_allowed_users
    add column if not exists consented boolean not null default false;

alter table public.operator_allowed_users
    add column if not exists consented_at timestamptz;

-- Служебные временные отметки для устойчивого планировщика.
-- Никакие тексты сообщений не записываются.
create table if not exists public.operator_contact_state (
    telegram_user_id bigint primary key,
    last_incoming_at timestamptz,
    last_initiative_at timestamptz,
    updated_at timestamptz not null default now()
);

create index if not exists operator_contact_state_last_incoming_idx
    on public.operator_contact_state (last_incoming_at);

alter table public.operator_settings enable row level security;
alter table public.operator_allowed_users enable row level security;
alter table public.operator_contact_state enable row level security;

-- Не создавайте публичные политики. Доступ к этим таблицам только
-- с серверной переменной SUPABASE_SERVICE_ROLE_KEY.
insert into public.operator_settings(key, value)
values
    ('enabled', 'false'),
    ('initiative_enabled', 'true'),
    ('initiative_hours', '6')
on conflict (key) do nothing;


-- Singleton lease via atomic PostgreSQL RPC (no PostgREST update().select()).
create table if not exists public.operator_runtime_lock (
    lock_name text primary key,
    owner_id text not null,
    lease_until timestamptz not null,
    updated_at timestamptz not null default now()
);
alter table public.operator_runtime_lock enable row level security;

create or replace function public.claim_operator_runtime_lock(
    p_owner_id text,
    p_lease_seconds integer default 120
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    affected integer;
begin
    insert into public.operator_runtime_lock(lock_name, owner_id, lease_until, updated_at)
    values ('telegram-ai-operator', p_owner_id, now() + make_interval(secs => greatest(p_lease_seconds, 30)), now())
    on conflict (lock_name) do update
       set owner_id = excluded.owner_id,
           lease_until = excluded.lease_until,
           updated_at = now()
     where public.operator_runtime_lock.lease_until < now()
        or public.operator_runtime_lock.owner_id = excluded.owner_id;
    get diagnostics affected = row_count;
    return affected = 1;
end;
$$;

create or replace function public.renew_operator_runtime_lock(
    p_owner_id text,
    p_lease_seconds integer default 120
) returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    affected integer;
begin
    update public.operator_runtime_lock
       set lease_until = now() + make_interval(secs => greatest(p_lease_seconds, 30)),
           updated_at = now()
     where lock_name = 'telegram-ai-operator'
       and owner_id = p_owner_id
       and lease_until > now();
    get diagnostics affected = row_count;
    return affected = 1;
end;
$$;

create or replace function public.release_operator_runtime_lock(p_owner_id text)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
    affected integer;
begin
    delete from public.operator_runtime_lock
     where lock_name = 'telegram-ai-operator' and owner_id = p_owner_id;
    get diagnostics affected = row_count;
    return affected = 1;
end;
$$;

revoke all on function public.claim_operator_runtime_lock(text, integer) from public, anon, authenticated;
revoke all on function public.renew_operator_runtime_lock(text, integer) from public, anon, authenticated;
revoke all on function public.release_operator_runtime_lock(text) from public, anon, authenticated;
grant execute on function public.claim_operator_runtime_lock(text, integer) to service_role;
grant execute on function public.renew_operator_runtime_lock(text, integer) to service_role;
grant execute on function public.release_operator_runtime_lock(text) to service_role;
notify pgrst, 'reload schema';


-- Aggregate operational statistics (message contents are never stored).
create table if not exists public.operator_stats (
    event_type text primary key,
    count bigint not null default 0,
    updated_at timestamptz not null default now()
);
alter table public.operator_stats enable row level security;

create or replace function public.increment_operator_stat(
    p_event_type text,
    p_telegram_user_id bigint default null
) returns bigint
language plpgsql
security definer
set search_path = public
as $$
declare new_count bigint;
begin
    insert into public.operator_stats(event_type, count, updated_at)
    values (p_event_type, 1, now())
    on conflict (event_type) do update
      set count = public.operator_stats.count + 1,
          updated_at = now()
    returning count into new_count;
    return new_count;
end;
$$;
revoke all on function public.increment_operator_stat(text, bigint) from public, anon, authenticated;
grant execute on function public.increment_operator_stat(text, bigint) to service_role;
notify pgrst, 'reload schema';


-- Compact per-contact memory: no raw chat history is copied into this table.
create table if not exists public.operator_contact_memory (
    telegram_user_id bigint primary key,
    summary text not null default '',
    interaction_count integer not null default 0,
    updated_at timestamptz not null default now()
);
alter table public.operator_contact_memory enable row level security;

notify pgrst, 'reload schema';
