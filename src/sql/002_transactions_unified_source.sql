-- Unified ledger: bank alerts, manual cash entries and scanned receipts all
-- live in public.transactions, distinguished by `source`.
alter table public.transactions add column if not exists source text;
alter table public.transactions add column if not exists description text;

-- Rows written before this migration were receipts / manual entries.
update public.transactions
set source = coalesce(metadata->>'source', 'receipt')
where source is null;

alter table public.transactions alter column source set default 'receipt';

alter table public.transactions drop constraint if exists transactions_source_check;
alter table public.transactions
  add constraint transactions_source_check
  check (source in ('bank_alert', 'manual_cash', 'receipt'));

create index if not exists transactions_user_source_idx
  on public.transactions (user_id, source);
