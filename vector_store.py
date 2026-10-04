"""
Vector store for Loamy: Gemini embeddings + Supabase pgvector.

Replaces the local chromadb.PersistentClient + in-process ONNX model, which
used ~350MB+ RAM and OOM-killed the 512MB Render instance. Nothing here loads
a model: embeddings are a network call to Gemini, storage is Postgres.

Two layers:
  1. get_embedding / add_vector / search_vectors - the explicit RAG API.
  2. SupabaseCollection - a drop-in stand-in for a Chroma Collection
     (get/add/upsert/update/delete/query/count) so the ~100 existing call sites
     in main.py keep working unchanged and return the exact same shapes.

Requires scripts/sql/004_vector_embeddings.sql to have been run in Supabase.
"""

import os
import time
import uuid

from database import supabase

TABLE = "vector_embeddings"
EMBEDDING_DIM = 768
# text-embedding-004 was retired by Google; gemini-embedding-001 is its
# replacement and can be truncated to 768 dims to match the pgvector column.
EMBEDDING_MODEL = os.getenv("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001")
PAGE_SIZE = 1000
MAX_EMBED_CHARS = 8000

# Only these collections are searched semantically (goals_collection.query in
# the chat endpoint). Everything else is key/value storage and skips the
# Gemini call entirely, keeping writes fast and free.
SEMANTIC_COLLECTIONS = {
    c.strip()
    for c in os.getenv("VECTOR_SEMANTIC_COLLECTIONS", "user_goals").split(",")
    if c.strip()
}


# ============================================
# Embeddings (Gemini API)
# ============================================
_genai_client = None


def _api_key() -> str:
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise ValueError("Missing GEMINI_API_KEY for embeddings.")
    return key


def _embed_batch(texts: list[str], task_type: str) -> list[list[float]]:
    global _genai_client
    clipped = [(t or " ")[:MAX_EMBED_CHARS] for t in texts]

    try:
        from google import genai as google_genai
        from google.genai import types as genai_types
    except ImportError:
        google_genai = None

    if google_genai is not None:
        if _genai_client is None:
            _genai_client = google_genai.Client(api_key=_api_key())
        result = _genai_client.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=clipped,
            config=genai_types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=EMBEDDING_DIM,
            ),
        )
        return [list(e.values) for e in result.embeddings]

    # Fallback to the legacy SDK main.py already depends on.
    import google.generativeai as legacy_genai

    legacy_genai.configure(api_key=_api_key())
    result = legacy_genai.embed_content(
        model=f"models/{EMBEDDING_MODEL}",
        content=clipped,
        task_type=task_type.lower(),
        output_dimensionality=EMBEDDING_DIM,
    )
    return [list(v) for v in result["embedding"]]


def get_embeddings(texts: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    """Embed many texts in one round trip, with a short retry for transient errors."""
    if not texts:
        return []
    last_error = None
    for attempt in range(3):
        try:
            return _embed_batch(texts, task_type)
        except Exception as e:
            last_error = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Gemini embedding failed: {last_error}")


def get_embedding(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    return get_embeddings([text], task_type)[0]


def _to_pgvector(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.7f}" for v in values) + "]"


# ============================================
# Explicit RAG API
# ============================================
def add_vector(user_id: str, content: str, metadata: dict | None = None,
               collection: str = "documents", doc_id: str | None = None) -> dict:
    """Embed content with Gemini and upsert it into Supabase."""
    try:
        meta = dict(metadata or {})
        meta.setdefault("user_id", user_id)
        row = {
            "collection": collection,
            "doc_id": doc_id or meta.get("id") or str(uuid.uuid4()),
            "user_id": user_id,
            "content": content or "",
            "metadata": meta,
            "embedding": _to_pgvector(get_embedding(content)),
        }
        supabase.table(TABLE).upsert(row, on_conflict="collection,doc_id").execute()
        return {"status": "success", "data": {"doc_id": row["doc_id"]}, "error": None}
    except Exception as e:
        print(f"[VectorStore] add_vector failed: {e}")
        return {"status": "error", "data": None, "error": str(e)}


def search_vectors(user_id: str | None, query: str, limit: int = 5,
                   collection: str | None = None, threshold: float = 0.0) -> list[dict]:
    """Top-k cosine matches for query, scoped to user_id (and optionally a collection)."""
    try:
        embedding = get_embedding(query, task_type="RETRIEVAL_QUERY")
        res = supabase.rpc("match_documents", {
            "query_embedding": _to_pgvector(embedding),
            "match_threshold": threshold,
            "match_count": limit,
            "p_user_id": user_id,
            "p_collection": collection,
        }).execute()
        return [
            {
                "id": r["doc_id"],
                "content": r["content"],
                "metadata": r.get("metadata") or {},
                "similarity": r.get("similarity"),
            }
            for r in (res.data or [])
        ]
    except Exception as e:
        print(f"[VectorStore] search_vectors failed: {e}")
        return []


# ============================================
# Chroma-compatible collection
# ============================================
_SUPPORTED_OPS = {"$eq", "$ne", "$in", "$nin"}


def _split_where(where: dict | None) -> list[tuple[str, str, object]]:
    """Flatten a Chroma where-filter into (key, op, value) clauses (AND only)."""
    if not where:
        return []
    clauses = []
    for key, value in where.items():
        if key == "$and":
            for sub in value:
                clauses.extend(_split_where(sub))
        elif key.startswith("$"):
            raise ValueError(f"Unsupported where operator: {key}")
        elif isinstance(value, dict):
            for op, v in value.items():
                if op not in _SUPPORTED_OPS:
                    raise ValueError(f"Unsupported where operator: {op}")
                clauses.append((key, op, v))
        else:
            clauses.append((key, "$eq", value))
    return clauses


def _apply_where(query, clauses):
    for key, op, value in clauses:
        if key == "user_id" and op == "$eq":
            query = query.eq("user_id", str(value))
        elif op == "$eq":
            query = query.contains("metadata", {key: value})
        elif op == "$ne":
            query = query.not_.contains("metadata", {key: value})
        elif op == "$in":
            query = query.in_(f"metadata->>{key}", [_as_text(v) for v in value])
        elif op == "$nin":
            values = ",".join(f'"{_as_text(v)}"' for v in value)
            query = query.filter(f"metadata->>{key}", "not.in", f"({values})")
    return query


def _as_text(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


class SupabaseCollection:
    """Implements the subset of chromadb.Collection used by Loamy."""

    def __init__(self, name: str):
        self.name = name
        self.semantic = name in SEMANTIC_COLLECTIONS

    # --- reads -----------------------------------------------------------
    def _select(self, ids=None, where=None, limit=None, offset=None) -> list[dict]:
        clauses = _split_where(where)
        rows: list[dict] = []

        if ids is not None:
            ids = [str(i) for i in ids]
            if not ids:
                return []
            for chunk in _chunks(ids):
                q = (supabase.table(TABLE).select("doc_id,content,metadata")
                     .eq("collection", self.name).in_("doc_id", chunk))
                rows.extend(_apply_where(q, clauses).execute().data or [])
            order = {doc_id: i for i, doc_id in enumerate(ids)}
            rows.sort(key=lambda r: order.get(r["doc_id"], 0))
        else:
            start = offset or 0
            remaining = limit
            while True:
                page = PAGE_SIZE if remaining is None else min(PAGE_SIZE, remaining)
                q = (supabase.table(TABLE).select("doc_id,content,metadata")
                     .eq("collection", self.name).order("id"))
                batch = _apply_where(q, clauses).range(start, start + page - 1).execute().data or []
                rows.extend(batch)
                if remaining is not None:
                    remaining -= len(batch)
                if len(batch) < page or (remaining is not None and remaining <= 0):
                    break
                start += page

        if offset and ids is not None:
            rows = rows[offset:]
        if limit is not None and ids is not None:
            rows = rows[:limit]
        return rows

    @staticmethod
    def _shape(rows: list[dict]) -> dict:
        return {
            "ids": [r["doc_id"] for r in rows],
            "documents": [r.get("content") or "" for r in rows],
            "metadatas": [r.get("metadata") or {} for r in rows],
            "embeddings": None,
        }

    def get(self, ids=None, where=None, limit=None, offset=None, include=None, **_ignored) -> dict:
        if isinstance(ids, str):
            ids = [ids]
        return self._shape(self._select(ids=ids, where=where, limit=limit, offset=offset))

    def peek(self, limit: int = 10) -> dict:
        return self.get(limit=limit)

    def count(self) -> int:
        res = (supabase.table(TABLE).select("id", count="exact", head=True)
               .eq("collection", self.name).execute())
        return res.count or 0

    # --- writes ----------------------------------------------------------
    def _rows(self, ids, documents=None, metadatas=None, embeddings=None) -> list[dict]:
        if isinstance(ids, str):
            ids = [ids]
        n = len(ids)
        documents = list(documents) if documents is not None else [""] * n
        metadatas = list(metadatas) if metadatas is not None else [{}] * n

        vectors = None
        if embeddings is not None:
            vectors = [list(e) for e in embeddings]
        elif self.semantic:
            try:
                vectors = get_embeddings([d or "" for d in documents])
            except Exception as e:
                # Never lose a write because the embedding API hiccupped; the
                # row is stored and simply won't appear in semantic search.
                print(f"[VectorStore] Embedding skipped for '{self.name}': {e}")

        rows = []
        for i in range(n):
            meta = dict(metadatas[i] or {})
            row = {
                "collection": self.name,
                "doc_id": str(ids[i]),
                "user_id": str(meta["user_id"]) if meta.get("user_id") is not None else None,
                "content": documents[i] or "",
                "metadata": meta,
            }
            if vectors is not None:
                row["embedding"] = _to_pgvector(vectors[i])
            rows.append(row)
        return rows

    def add(self, ids, documents=None, metadatas=None, embeddings=None, **_ignored) -> None:
        # Chroma's add() ignores ids that already exist; mirror that.
        for chunk in _chunks(self._rows(ids, documents, metadatas, embeddings)):
            supabase.table(TABLE).upsert(
                chunk, on_conflict="collection,doc_id", ignore_duplicates=True
            ).execute()

    def upsert(self, ids, documents=None, metadatas=None, embeddings=None, **_ignored) -> None:
        for chunk in _chunks(self._rows(ids, documents, metadatas, embeddings)):
            supabase.table(TABLE).upsert(chunk, on_conflict="collection,doc_id").execute()

    def update(self, ids, documents=None, metadatas=None, embeddings=None, **_ignored) -> None:
        if isinstance(ids, str):
            ids = [ids]
        existing = {r["doc_id"]: r for r in self._select(ids=ids)}

        for i, doc_id in enumerate(ids):
            current = existing.get(str(doc_id))
            if current is None:
                continue  # Chroma silently skips unknown ids on update.

            patch: dict = {}
            if metadatas is not None:
                # Chroma merges metadata keys on update; None deletes a key.
                merged = dict(current.get("metadata") or {})
                for k, v in (metadatas[i] or {}).items():
                    if v is None:
                        merged.pop(k, None)
                    else:
                        merged[k] = v
                patch["metadata"] = merged
                if merged.get("user_id") is not None:
                    patch["user_id"] = str(merged["user_id"])
            if documents is not None:
                patch["content"] = documents[i] or ""
                if embeddings is None and self.semantic:
                    try:
                        patch["embedding"] = _to_pgvector(get_embedding(patch["content"]))
                    except Exception as e:
                        print(f"[VectorStore] Re-embedding skipped for '{self.name}': {e}")
            if embeddings is not None:
                patch["embedding"] = _to_pgvector(list(embeddings[i]))

            if patch:
                (supabase.table(TABLE).update(patch)
                 .eq("collection", self.name).eq("doc_id", str(doc_id)).execute())

    def delete(self, ids=None, where=None, **_ignored) -> None:
        if ids is None and not where:
            return  # Refuse to wipe a whole collection by accident.
        if isinstance(ids, str):
            ids = [ids]
        clauses = _split_where(where)
        if ids is not None:
            for chunk in _chunks([str(i) for i in ids]):
                q = supabase.table(TABLE).delete().eq("collection", self.name).in_("doc_id", chunk)
                _apply_where(q, clauses).execute()
        else:
            q = supabase.table(TABLE).delete().eq("collection", self.name)
            _apply_where(q, clauses).execute()

    # --- semantic search -------------------------------------------------
    def query(self, query_texts=None, n_results: int = 10, where=None, **_ignored) -> dict:
        texts = [query_texts] if isinstance(query_texts, str) else list(query_texts or [])
        clauses = _split_where(where)
        user_id = next((str(v) for k, op, v in clauses if k == "user_id" and op == "$eq"), None)
        extra = [(k, op, v) for k, op, v in clauses if not (k == "user_id" and op == "$eq")]

        out = {"ids": [], "documents": [], "metadatas": [], "distances": []}
        for text in texts:
            # Over-fetch when extra metadata filters will be applied in Python.
            matches = search_vectors(
                user_id, text,
                limit=n_results * 4 if extra else n_results,
                collection=self.name,
            )
            if extra:
                matches = [m for m in matches if _matches(m["metadata"], extra)]
            matches = matches[:n_results]
            out["ids"].append([m["id"] for m in matches])
            out["documents"].append([m["content"] for m in matches])
            out["metadatas"].append([m["metadata"] for m in matches])
            out["distances"].append([1 - (m["similarity"] or 0) for m in matches])
        return out


def _matches(meta: dict, clauses) -> bool:
    for key, op, value in clauses:
        actual = meta.get(key)
        if op == "$eq" and actual != value:
            return False
        if op == "$ne" and actual == value:
            return False
        if op == "$in" and actual not in value:
            return False
        if op == "$nin" and actual in value:
            return False
    return True


_collections: dict[str, SupabaseCollection] = {}


def get_collection(name: str) -> SupabaseCollection:
    if name not in _collections:
        _collections[name] = SupabaseCollection(name)
    return _collections[name]


def health() -> dict:
    """Cheap connectivity probe used by /health/vector-store."""
    try:
        supabase.table(TABLE).select("id", head=True).limit(1).execute()
        return {"status": "ready", "backend": "supabase-pgvector",
                "embedding_model": EMBEDDING_MODEL, "error": None}
    except Exception as e:
        return {"status": "error", "backend": "supabase-pgvector",
                "embedding_model": EMBEDDING_MODEL, "error": str(e)}
