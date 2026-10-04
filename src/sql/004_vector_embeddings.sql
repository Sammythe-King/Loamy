-- 004_vector_embeddings.sql
-- Replaces the local ChromaDB vault with Supabase pgvector.
-- Run once in the Supabase SQL Editor. Safe to re-run (idempotent).
--
-- Design notes:
--   * One table backs every former Chroma collection. `collection` holds the
--     old collection name (e.g. 'user_goals', 'review_queue') and `doc_id` the
--     old Chroma id, so (collection, doc_id) is the logical primary key.
--   * user_id is TEXT, not a UUID FK to auth.users: Loamy ids look like
--     'usr_iyallasamuel43' and live in public.users, not Supabase Auth.
--   * embedding is NULLable: only collections that are semantically searched
--     (currently user_goals) get embeddings, so key/value collections such as
--     sync_state or notifications cost zero Gemini calls.
--   * 768 dims = gemini-embedding-001 with output_dimensionality=768.

create extension if not exists vector;

create table if not exists public.vector_embeddings (
    id          bigint generated always as identity primary key,
    collection  text        not null,
    doc_id      text        not null,
    user_id     text,
    content     text        not null default '',
    metadata    jsonb       not null default '{}'::jsonb,
    embedding   vector(768),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint vector_embeddings_collection_doc_id_key unique (collection, doc_id)
);

create index if not exists vector_embeddings_collection_user_idx
    on public.vector_embeddings (collection, user_id);

create index if not exists vector_embeddings_metadata_gin_idx
    on public.vector_embeddings using gin (metadata jsonb_path_ops);

create index if not exists vector_embeddings_embedding_hnsw_idx
    on public.vector_embeddings using hnsw (embedding vector_cosine_ops);

create or replace function public.vector_embeddings_touch_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

drop trigger if exists vector_embeddings_touch_updated_at on public.vector_embeddings;
create trigger vector_embeddings_touch_updated_at
    before update on public.vector_embeddings
    for each row execute function public.vector_embeddings_touch_updated_at();

-- Backend uses the service-role key (bypasses RLS). RLS on with no policies
-- means the anon/publishable key can never read anyone's vectors.
alter table public.vector_embeddings enable row level security;

-- Cosine top-k search. p_user_id / p_collection are optional filters so the
-- same function serves per-user RAG and collection-wide lookups.
create or replace function public.match_documents(
    query_embedding vector(768),
    match_threshold float default 0.0,
    match_count     int   default 5,
    p_user_id       text  default null,
    p_collection    text  default null
)
returns table (
    doc_id      text,
    collection  text,
    user_id     text,
    content     text,
    metadata    jsonb,
    similarity  float
)
language sql
stable
as $$
    select
        v.doc_id,
        v.collection,
        v.user_id,
        v.content,
        v.metadata,
        1 - (v.embedding <=> query_embedding) as similarity
    from public.vector_embeddings v
    where v.embedding is not null
      and (p_user_id is null or v.user_id = p_user_id)
      and (p_collection is null or v.collection = p_collection)
      and 1 - (v.embedding <=> query_embedding) >= match_threshold
    order by v.embedding <=> query_embedding
    limit greatest(match_count, 1);
$$;

revoke all on function public.match_documents(vector, float, int, text, text) from public, anon, authenticated;
grant execute on function public.match_documents(vector, float, int, text, text) to service_role;
