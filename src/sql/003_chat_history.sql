-- Omnichannel chat memory: one row per message, shared by WhatsApp and the web app.
-- Run once in the Supabase SQL editor. Safe to re-run.

create extension if not exists pgcrypto;

create table if not exists public.chat_history (
  id          uuid primary key default gen_random_uuid(),
  user_id     text not null,
  sender      text not null check (sender in ('user', 'assistant')),
  message     text not null,
  channel     text not null default 'app' check (channel in ('whatsapp', 'app')),
  created_at  timestamptz not null default now()
);

create index if not exists chat_history_user_created_idx
  on public.chat_history (user_id, created_at desc);

-- The backend uses the service-role key, which bypasses RLS. Enabling RLS with
-- no public policies keeps the anon key from reading anyone's history.
alter table public.chat_history enable row level security;

-- Optional: lets Supabase Realtime broadcast inserts if you later subscribe
-- from the browser instead of polling.
do $$
begin
  alter publication supabase_realtime add table public.chat_history;
exception when others then null;
end $$;
