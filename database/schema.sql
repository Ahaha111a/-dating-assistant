-- Выполнить один раз в Supabase SQL Editor.
-- История сообщений в Supabase не хранится.
create table if not exists public.operator_settings (
    key text primary key,
    value text not null
);
create table if not exists public.operator_allowed_users (
    telegram_user_id bigint primary key,
    username text not null default '',
    added_at timestamptz not null default now()
);
alter table public.operator_settings enable row level security;
alter table public.operator_allowed_users enable row level security;
-- Не создавайте публичные политики. Доступ только с серверным service_role key.
insert into public.operator_settings(key, value)
values ('enabled', 'false')
on conflict (key) do nothing;
