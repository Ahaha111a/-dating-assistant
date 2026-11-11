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


-- Distributed singleton lease: blocks simultaneous bot polling across replicas.
create table if not exists public.operator_runtime_lock (
    lock_name text primary key,
    owner_id text not null,
    lease_until timestamptz not null,
    updated_at timestamptz not null default now()
);
alter table public.operator_runtime_lock enable row level security;
