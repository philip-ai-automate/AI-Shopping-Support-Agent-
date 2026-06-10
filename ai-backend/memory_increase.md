# Semantic Memory Upgrade — Design & Implementation Plan

## Goal
Extend AI chat memory using pgvector semantic retrieval so the AI can recall
relevant past exchanges beyond the current 4-message sliding window, at zero
additional API cost.

---

## Why Zero Extra Cost
`main.py` already calls `_embed_query(req.message)` inside
`search_documents_with_meta()` to power product search. That embedding is
computed, used once, then discarded. By generating it BEFORE the search call
and passing it into both:
  - `search_documents_with_meta()` (product search — already happening)
  - `add_message()` (store on the chat_messages row)
we get semantic memory for free. The embedding API call was already paid for.

---

## Architecture

### Memory layers (all three kept):
1. **Last N messages** (keep_last_n=4) — immediate conversational continuity
2. **Summary** (chat_summaries table) — compact overall context, already built
3. **Semantic retrieval** (NEW) — finds relevant older messages by vector similarity

### Flow per chat request (new):
```
1. generate embedding for req.message          ← ONE call, reused for steps 2+3
2. get_semantic_history(session_id, tenant_id,
       query_embedding, keep_last_n=4,
       semantic_top_k=4)                        ← replaces get_history_with_summary()
3. add_message(..., embedding=msg_embedding)    ← store embedding on user message
4. search_documents_with_meta(...,
       precomputed_embedding=msg_embedding)     ← reuse embedding, no 2nd call
5. ask_llm(history=history)                    ← history now has semantic context
6. add_message(assistant reply, embedding=None) ← assistant reply stored, no embed
```

---

## Database Change
```sql
ALTER TABLE chat_messages
  ADD COLUMN IF NOT EXISTS embedding vector(1536);

CREATE INDEX IF NOT EXISTS idx_chat_messages_emb
  ON chat_messages
  USING ivfflat (embedding vector_cosine_ops)
  WITH (lists = 50);
```
Run once. Existing rows get embedding=NULL — they stay in last-N retrieval
but are excluded from semantic search until re-processed (acceptable).

---

## File 1: memory_store.py

### Change 1 — modify add_message() signature
```python
def add_message(session_id: str, tenant_id: int, role: str,
                content: str, embedding: list = None):
```
When embedding is provided, store it:
```sql
INSERT INTO chat_messages (session_id, tenant_id, role, content, embedding)
VALUES (%s, %s, %s, %s, %s::vector)
```
When None:
```sql
INSERT INTO chat_messages (session_id, tenant_id, role, content)
VALUES (%s, %s, %s, %s)
```

### Change 2 — new function get_semantic_history()
```python
def get_semantic_history(session_id: str, tenant_id: int,
                         query_embedding: list,
                         keep_last_n: int = 4,
                         semantic_top_k: int = 4) -> list:
    """
    Returns history combining:
      - Last keep_last_n messages (always, for continuity)
      - Top semantic_top_k relevant older messages + their replies
    Deduplicated and sorted chronologically (by id ASC).
    Prepends summary if one exists.
    """
```

#### Implementation steps inside get_semantic_history():
```
1. Fetch summary from chat_summaries (same as get_history_with_summary)
2. Fetch last keep_last_n messages → get their IDs
3. Vector search on chat_messages WHERE:
     session_id = %s AND tenant_id = %s
     AND embedding IS NOT NULL
     AND id NOT IN (last_n_ids)
     AND role = 'user'
   ORDER BY embedding <=> %s::vector
   LIMIT semantic_top_k
4. For each semantically matched user message (id=X), also fetch the
   immediately following assistant message (id = min id > X where role='assistant')
5. Merge: semantic pairs + last_n messages
6. Deduplicate by id
7. Sort by id ASC (chronological)
8. Build OpenAI messages list:
     [summary system msg (if exists)] + [{"role":r,"content":c} for each]
9. Return list
```

---

## File 2: search.py

### Change — accept precomputed embedding in search_documents_with_meta()
```python
def search_documents_with_meta(
    query: str, tenant_id: int,
    semantic_config: Optional[str] = None,
    precomputed_embedding: Optional[List[float]] = None   # NEW
) -> tuple:
```
Inside the function, replace:
```python
q_vec = _embed_query(query)
```
with:
```python
q_vec = precomputed_embedding if precomputed_embedding else _embed_query(query)
```
No other changes needed.

---

## File 3: main.py

### Change — generate embedding early, wire everything up
Replace this block:
```python
ensure_session(session_id, tenant_id)
try:
    maybe_summarize_session(...)
except Exception:
    pass
history = get_history_with_summary(session_id, tenant_id, keep_last_n=...)
add_message(session_id, tenant_id, "user", req.message)
context_chunks, raw_docs = search_documents_with_meta(req.message, tenant_id)
```

With:
```python
ensure_session(session_id, tenant_id)
try:
    maybe_summarize_session(...)
except Exception:
    pass

# Generate embedding ONCE — reused for semantic history + product search
from search import _embed_query as _emb
msg_embedding = None
try:
    msg_embedding = _emb(req.message)
except Exception as _emb_err:
    print(f"⚠️ embedding failed (semantic memory disabled this turn): {_emb_err}")

history = get_semantic_history(
    session_id, tenant_id,
    query_embedding=msg_embedding or [],
    keep_last_n=int(os.getenv("HISTORY_KEEP_LAST_N", "4")),
    semantic_top_k=int(os.getenv("SEMANTIC_HISTORY_TOP_K", "4")),
) if msg_embedding else get_history_with_summary(
    session_id, tenant_id,
    keep_last_n=int(os.getenv("HISTORY_KEEP_LAST_N", "4")),
)

add_message(session_id, tenant_id, "user", req.message,
            embedding=msg_embedding)

context_chunks, raw_docs = search_documents_with_meta(
    req.message, tenant_id,
    precomputed_embedding=msg_embedding,
)
```

Also update the import in main.py:
```python
from memory_store import (
    init_memory_tables,
    ensure_session,
    add_message,
    maybe_summarize_session,
    get_history_with_summary,
    get_semantic_history,          # NEW
)
```

---

## Environment Variables (optional tuning)
```
HISTORY_KEEP_LAST_N=4       # already exists
SEMANTIC_HISTORY_TOP_K=4    # NEW — how many semantic pairs to retrieve
```

---

## Implementation Order
1. DB migration (SQL — irreversible, do first)
2. memory_store.py (self-contained)
3. search.py (tiny change, independent)
4. main.py (wires everything, last)
5. Restart phixtra-ai-backend.service
6. Test with session that has existing history

---

## Status
- [x] DB migration — embedding vector(1536) + ivfflat index on chat_messages
- [x] memory_store.py — add_message() accepts embedding; get_semantic_history() added
- [x] search.py — accepts precomputed_embedding to avoid double embed call
- [x] main.py — generates embedding once, passes to memory + search
- [x] Service restart — port conflict fixed (fuser -k 8000/tcp), service active
- [x] chat_messages_id_seq fixed (was out of sync from MySQL migration, reset to 541)
- [x] Test passed — semantic recall working (Turn 6 recalled iPhone 14 after 3 unrelated turns)

---

## Key Decisions
- Assistant replies are stored WITHOUT embeddings (saves cost, user messages
  are sufficient for relevance retrieval)
- Fallback to get_history_with_summary() if embedding fails (resilient)
- Summary is kept alongside semantic retrieval (complementary, not replaced)
- semantic_top_k=4 default: retrieves 4 relevant user+reply pairs = up to 8
  extra messages in history. Combined with last 4 = up to 12 messages of
  context, but only the most relevant ones.
- ivfflat index with lists=50 is appropriate for expected scale
  (thousands of messages per session over time)
