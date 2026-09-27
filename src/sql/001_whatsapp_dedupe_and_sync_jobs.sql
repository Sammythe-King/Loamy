-- Persistent state that used to live in RAM on the Render instance.
-- Run once in the Supabase SQL editor. Safe to re-run.

create table if not exists public.whatsapp_processed_messages (
  message_id text primary key,
  phone_number text,
  created_at timestamptz not null default now()
);
create index if not exists whatsapp_processed_messages_created_at_idx
  on public.whatsapp_processed_messages (created_at);
alter table public.whatsapp_processed_messages enable row level security;

create table if not exists public.sync_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id text not null unique,
  status text not null check (status in ('processing', 'completed', 'failed')),
  error_message text,
  updated_at timestamptz not null default now()
);
alter table public.sync_jobs enable row level security;
