-- Persistent Gemini rate limits and usage log.
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- gemini_rate_limits : one row per (scope, key, fixed UTC window) holding a call counter.
-- gemini_usage_logs  : one row per Gemini request, with token counts, for cost analysis.
-- gemini_admit()     : atomically reserves one call against all four limits, or refuses.
--
-- Only the backend (service role) touches these tables. RLS is enabled with no
-- policies, so the anon and authenticated roles can read and write nothing.

create table if not exists public.gemini_rate_limits (
  scope        text        not null,   -- 'global_day' | 'feature_hour' | 'user_feature_hour' | 'user_hour'
  key          text        not null,   -- 'all' | feature | feature:user_id | user_id
  window_start timestamptz not null,   -- start of the UTC hour or day bucket
  calls        integer     not null default 0,
  updated_at   timestamptz not null default now(),
  primary key (scope, key, window_start)
);

create index if not exists gemini_rate_limits_window_idx
  on public.gemini_rate_limits (window_start);

create table if not exists public.gemini_usage_logs (
  id            bigint generated always as identity primary key,
  created_at    timestamptz not null default now(),
  feature       text        not null,
  user_id       text,
  model         text,
  status        text        not null,  -- 'ok' | 'failed'
  prompt_tokens integer     not null default 0,
  output_tokens integer     not null default 0,
  thought_tokens integer    not null default 0,
  latency_ms    integer,
  error         text
);

create index if not exists gemini_usage_logs_created_idx
  on public.gemini_usage_logs (created_at desc);
create index if not exists gemini_usage_logs_feature_idx
  on public.gemini_usage_logs (feature, created_at desc);
create index if not exists gemini_usage_logs_user_idx
  on public.gemini_usage_logs (user_id, created_at desc);

alter table public.gemini_rate_limits enable row level security;
alter table public.gemini_usage_logs  enable row level security;

-- Reserve one Gemini call. Returns 'ok', or the reason it was refused:
--   global_daily_limit | feature_hourly_limit | user_hourly_limit | user_total_hourly_limit
-- A refused call leaves no trace: the inner block rolls back every increment it made,
-- so blocked attempts never eat into the budget.
create or replace function public.gemini_admit(
  p_feature              text,
  p_user_id              text,
  p_global_daily         integer,
  p_feature_hourly       integer,
  p_user_feature_hourly  integer,
  p_user_total_hourly    integer
) returns text
language plpgsql
security definer
set search_path = public
as $$
declare
  v_hour  timestamptz := date_trunc('hour', now() at time zone 'UTC') at time zone 'UTC';
  v_day   timestamptz := date_trunc('day',  now() at time zone 'UTC') at time zone 'UTC';
  v_calls integer;
begin
  begin
    insert into gemini_rate_limits (scope, key, window_start, calls)
    values ('global_day', 'all', v_day, 1)
    on conflict (scope, key, window_start)
    do update set calls = gemini_rate_limits.calls + 1, updated_at = now()
    returning calls into v_calls;
    if v_calls > p_global_daily then raise exception 'global_daily_limit'; end if;

    insert into gemini_rate_limits (scope, key, window_start, calls)
    values ('feature_hour', p_feature, v_hour, 1)
    on conflict (scope, key, window_start)
    do update set calls = gemini_rate_limits.calls + 1, updated_at = now()
    returning calls into v_calls;
    if v_calls > p_feature_hourly then raise exception 'feature_hourly_limit'; end if;

    if p_user_id is not null and p_user_id <> '' then
      insert into gemini_rate_limits (scope, key, window_start, calls)
      values ('user_feature_hour', p_feature || ':' || p_user_id, v_hour, 1)
      on conflict (scope, key, window_start)
      do update set calls = gemini_rate_limits.calls + 1, updated_at = now()
      returning calls into v_calls;
      if v_calls > p_user_feature_hourly then raise exception 'user_hourly_limit'; end if;

      insert into gemini_rate_limits (scope, key, window_start, calls)
      values ('user_hour', p_user_id, v_hour, 1)
      on conflict (scope, key, window_start)
      do update set calls = gemini_rate_limits.calls + 1, updated_at = now()
      returning calls into v_calls;
      if v_calls > p_user_total_hourly then raise exception 'user_total_hourly_limit'; end if;
    end if;
  exception when raise_exception then
    return sqlerrm;
  end;

  -- Housekeeping on ~1% of calls so the tables never grow without bound.
  if random() < 0.01 then
    delete from gemini_rate_limits where window_start < now() - interval '2 days';
    delete from gemini_usage_logs  where created_at   < now() - interval '90 days';
  end if;

  return 'ok';
end;
$$;

revoke all on function public.gemini_admit(text, text, integer, integer, integer, integer) from public, anon, authenticated;
grant execute on function public.gemini_admit(text, text, integer, integer, integer, integer) to service_role;
