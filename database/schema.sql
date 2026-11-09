-- Выполнить один раз в Supabase SQL Editor.
-- Сообщения и история диалогов здесь НЕ хранятся.
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

-- Не создавай публичные политики. Приложение обращается к таблицам
-- только с серверным SUPABASE_SERVICE_ROLE_KEY.
insert into public.operator_settings(key, value)
values ('enabled', 'false')
on conflict (key) do nothing;
