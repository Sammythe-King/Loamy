-- Conversation state per user, so short replies like "yes" / "sure" can be
-- resolved against what Loamy last offered instead of re-running a summary.
-- Safe to run more than once.

create table if not exists public.chat_sessions (
  user_id          text primary key,
  current_state    text not null default 'IDLE'
                   check (current_state in (
                     'IDLE',
                     'AWAITING_CATEGORIZATION_APPROVAL',
                     'CATEGORIZATION_IN_PROGRESS'
                   )),
  pending_context  jsonb not null default '{}'::jsonb,
  updated_at       timestamptz not null default now()
);

-- Only the backend (service role) touches this table.
alter table public.chat_sessions enable row level security;