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

-- Служебные временные отметки активности контактов.
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
    ('enabled', 'false')
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




-- Compact per-contact memory: no raw chat history is copied into this table.
create table if not exists public.operator_contact_memory (
    telegram_user_id bigint primary key,
    summary text not null default '',
    interaction_count integer not null default 0,
    updated_at timestamptz not null default now()
);
alter table public.operator_contact_memory enable row level security;

notify pgrst, 'reload schema';


-- Durable incoming-message queue. Only Telegram IDs/status are stored; message text and media remain in Telegram.
create table if not exists public.operator_pending_messages (
    id bigserial primary key,
    telegram_user_id bigint not null,
    telegram_message_id bigint not null,
    status text not null default 'pending' check (status in ('pending','processed','ignored','unavailable')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    unique (telegram_user_id, telegram_message_id)
);
create index if not exists operator_pending_messages_pending_idx
    on public.operator_pending_messages(status, created_at)
    where status = 'pending';
alter table public.operator_pending_messages enable row level security;

-- Per-contact counters used by the private admin panel.
create table if not exists public.operator_contact_stats (
    telegram_user_id bigint not null,
    event_type text not null,
    count bigint not null default 0,
    updated_at timestamptz not null default now(),
    primary key (telegram_user_id, event_type)
);
alter table public.operator_contact_stats enable row level security;

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
    if p_telegram_user_id is not null then
        insert into public.operator_contact_stats(telegram_user_id, event_type, count, updated_at)
        values (p_telegram_user_id, p_event_type, 1, now())
        on conflict (telegram_user_id, event_type) do update
          set count = public.operator_contact_stats.count + 1,
              updated_at = now();
    end if;
    return new_count;
end;
$$;
revoke all on function public.increment_operator_stat(text, bigint) from public, anon, authenticated;
grant execute on function public.increment_operator_stat(text, bigint) to service_role;
notify pgrst, 'reload schema';


-- Explicit admin blacklist. A blocked contact can never pass the access gate.
create table if not exists public.operator_blocked_users (
    telegram_user_id bigint primary key,
    username text not null default '',
    reason text not null default 'admin_block',
    created_at timestamptz not null default now()
);
alter table public.operator_blocked_users enable row level security;
notify pgrst, 'reload schema';


-- Version history for admin-edited system prompts.
create table if not exists public.operator_prompt_versions (
    id bigserial primary key,
    prompt text not null,
    created_at timestamptz not null default now()
);
alter table public.operator_prompt_versions enable row level security;
notify pgrst, 'reload schema';


-- Explicit server-role privileges; RLS remains enabled and no public policies are added.
grant all on table public.operator_settings to service_role;
grant all on table public.operator_allowed_users to service_role;
grant all on table public.operator_contact_state to service_role;
grant all on table public.operator_runtime_lock to service_role;
grant all on table public.operator_stats to service_role;
grant all on table public.operator_contact_memory to service_role;
grant all on table public.operator_pending_messages to service_role;
grant all on table public.operator_contact_stats to service_role;
grant all on table public.operator_blocked_users to service_role;
grant all on table public.operator_prompt_versions to service_role;
grant usage, select on all sequences in schema public to service_role;
notify pgrst, 'reload schema';

-- Futures trader research module. Paper trades only; no exchange credentials or live orders.
create table if not exists public.operator_trader_trades (
    id bigserial primary key,
    symbol text not null,
    side text not null check (side in ('LONG','SHORT')),
    entry_price double precision not null,
    quantity double precision not null,
    stop_price double precision not null,
    take_price double precision not null,
    risk_usd double precision not null,
    entry_fee double precision not null default 0,
    entry_time timestamptz not null default now(),
    exit_price double precision,
    exit_time timestamptz,
    gross_pnl double precision,
    fees double precision,
    net_pnl double precision,
    close_reason text,
    strategy text not null,
    status text not null default 'open' check (status in ('open','closed'))
);
create index if not exists operator_trader_trades_status_idx
    on public.operator_trader_trades(status, id desc);
alter table public.operator_trader_trades enable row level security;
grant all on table public.operator_trader_trades to service_role;
grant usage, select on sequence public.operator_trader_trades_id_seq to service_role;
insert into public.operator_settings(key, value) values
    ('trader_demo_enabled','false'),
    ('trader_paper_balance','10000')
on conflict (key) do nothing;
notify pgrst, 'reload schema';
