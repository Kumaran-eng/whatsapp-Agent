"""
Customer Memory Layer — Semantic + Episodic Memory
===================================================
Semantic Memory  → Stores customer preferences as embedded vectors (Gemini embeddings)
Episodic Memory  → Stores conversation summaries for recalling past sessions
"""

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from google import genai

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "messages.db"

EMBEDDING_MODEL = "text-embedding-004"


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _memory_text_items(value) -> list[str]:
    """Convert stored or generated memory facts to safe, readable strings."""
    if value is None:
        return []
    values = value if isinstance(value, (list, tuple, set)) else [value]
    items = []
    for item in values:
        if item is None:
            continue
        if isinstance(item, str):
            text = item.strip()
        else:
            text = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if text:
            items.append(text)
    return items


def initialize_memory_tables():
    """Create the memory tables if they don't exist."""
    db = _connect()
    try:
        # Semantic Memory — customer preferences with embeddings
        db.execute("""
            CREATE TABLE IF NOT EXISTS semantic_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                preference TEXT NOT NULL,
                embedding TEXT,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_sem_phone ON semantic_preferences(phone)")

        # Episodic Memory — conversation summaries
        db.execute("""
            CREATE TABLE IF NOT EXISTS episodic_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                summary TEXT NOT NULL,
                key_facts TEXT,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_epi_phone ON episodic_summaries(phone)")
        db.commit()
    finally:
        db.close()


# =============================================
# EMBEDDING HELPERS (Pure Python, no numpy)
# =============================================

def _get_genai_client():
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is missing")
    return genai.Client(api_key=api_key)


def _get_embedding(text: str) -> list[float]:
    """Get embedding vector from Gemini API."""
    client = _get_genai_client()
    result = client.models.embed_content(
        model=EMBEDDING_MODEL,
        contents=[text],
    )
    return list(result.embeddings[0].values)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure Python cosine similarity — no numpy needed."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# =============================================
# SEMANTIC MEMORY — Customer Preferences
# =============================================

def store_preference(phone: str, preference: str):
    """Store a customer preference with its embedding vector."""
    try:
        embedding = _get_embedding(preference)
        embedding_json = json.dumps(embedding)
    except Exception as e:
        print(f"MEMORY: Embedding failed for preference, storing without vector: {e}", flush=True)
        embedding_json = None

    db = _connect()
    try:
        # Check for duplicate preferences (same text)
        existing = db.execute(
            "SELECT id FROM semantic_preferences WHERE phone=? AND preference=?",
            (phone, preference),
        ).fetchone()
        if existing:
            return  # Already stored

        db.execute(
            "INSERT INTO semantic_preferences(phone, preference, embedding, created_at) VALUES (?, ?, ?, ?)",
            (phone, preference, embedding_json, _now()),
        )
        db.commit()
        print(f"MEMORY: Stored preference for {phone}: {preference}", flush=True)
    finally:
        db.close()


def find_relevant_preferences(phone: str, query: str, top_k: int = 3) -> list[str]:
    """Find the most relevant preferences for a customer based on semantic similarity."""
    db = _connect()
    try:
        rows = db.execute(
            "SELECT preference, embedding FROM semantic_preferences WHERE phone=?",
            (phone,),
        ).fetchall()

        if not rows:
            return []

        # If no embeddings or few preferences, just return all
        prefs_without_embeddings = [r["preference"] for r in rows if not r["embedding"]]
        prefs_with_embeddings = [(r["preference"], r["embedding"]) for r in rows if r["embedding"]]

        if not prefs_with_embeddings:
            return prefs_without_embeddings[:top_k]

        try:
            query_embedding = _get_embedding(query)
        except Exception:
            # If embedding fails, return all preferences
            return [r["preference"] for r in rows][:top_k]

        # Score each preference by similarity
        scored = []
        for pref_text, emb_json in prefs_with_embeddings:
            emb = json.loads(emb_json)
            score = _cosine_similarity(query_embedding, emb)
            scored.append((score, pref_text))

        scored.sort(reverse=True)
        results = [text for _, text in scored[:top_k]]
        results.extend(prefs_without_embeddings[:max(0, top_k - len(results))])
        return results[:top_k]
    finally:
        db.close()


def get_all_preferences(phone: str) -> list[str]:
    """Get all stored preferences for a customer."""
    db = _connect()
    try:
        rows = db.execute(
            "SELECT preference FROM semantic_preferences WHERE phone=? ORDER BY created_at DESC",
            (phone,),
        ).fetchall()
        return [r["preference"] for r in rows]
    finally:
        db.close()


# =============================================
# EPISODIC MEMORY — Conversation Summaries
# =============================================

def store_episode(phone: str, summary: str, key_facts: list[str] = None):
    """Store a conversation episode summary."""
    db = _connect()
    try:
        facts = _memory_text_items(key_facts)
        facts_json = json.dumps(facts, ensure_ascii=False) if facts else None
        db.execute(
            "INSERT INTO episodic_summaries(phone, summary, key_facts, created_at) VALUES (?, ?, ?, ?)",
            (phone, summary, facts_json, _now()),
        )
        db.commit()
        print(f"MEMORY: Stored episode for {phone}: {summary[:80]}...", flush=True)
    finally:
        db.close()


def get_recent_episodes(phone: str, limit: int = 5) -> list[dict]:
    """Get the most recent conversation summaries for a customer."""
    db = _connect()
    try:
        rows = db.execute(
            "SELECT summary, key_facts, created_at FROM episodic_summaries WHERE phone=? ORDER BY id DESC LIMIT ?",
            (phone, limit),
        ).fetchall()
        episodes = []
        for r in reversed(rows):  # Chronological order
            ep = {"summary": r["summary"], "date": r["created_at"]}
            if r["key_facts"]:
                try:
                    ep["key_facts"] = _memory_text_items(json.loads(r["key_facts"]))
                except (json.JSONDecodeError, TypeError):
                    pass
            episodes.append(ep)
        return episodes
    finally:
        db.close()


# =============================================
# GENERATE EPISODE SUMMARY (using Gemini)
# =============================================

async def generate_episode_summary(messages: list[dict]) -> dict:
    """Use Gemini to generate a summary of the conversation and extract preferences."""
    client = _get_genai_client()

    conversation_text = "\n".join(
        f"{'Customer' if m['role'] == 'user' else 'Agent'}: {m['content']}"
        for m in messages[-10:]  # Last 10 messages
    )

    prompt = f"""Analyze this customer support conversation and return a JSON object with:
1. "summary": A 1-2 sentence summary of what happened
2. "key_facts": An array of key facts (order IDs, issues, outcomes)
3. "preferences": An array of customer preferences detected (things they want every time, communication style preferences, product preferences). Return empty array if none detected.

Conversation:
{conversation_text}

Return ONLY valid JSON, no markdown:"""

    try:
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=[prompt],
        )
        text = response.text.strip()
        # Clean up markdown formatting if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()
        return json.loads(text)
    except Exception as e:
        print(f"MEMORY: Episode summary generation failed: {e}", flush=True)
        return {"summary": "", "key_facts": [], "preferences": []}


# =============================================
# BUILD MEMORY CONTEXT FOR AGENT
# =============================================

def build_full_memory_context(phone: str, current_query: str = "") -> str:
    """Build complete memory context string to inject into the agent prompt."""
    parts = []

    # --- Customer Memory (key-value from customer_memory table) ---
    db = _connect()
    try:
        row = db.execute(
            "SELECT last_order_id, customer_name, notes FROM customer_memory WHERE phone=?",
            (phone,),
        ).fetchone()
        if row:
            if row["last_order_id"]:
                parts.append(f"Last known order ID: {row['last_order_id']}")
            if row["customer_name"]:
                parts.append(f"Customer name: {row['customer_name']}")
    except Exception:
        pass  # Table might not exist yet
    finally:
        db.close()

    # --- Semantic Memory (Preferences) ---
    if current_query:
        relevant_prefs = find_relevant_preferences(phone, current_query, top_k=5)
    else:
        relevant_prefs = get_all_preferences(phone)[:5]

    if relevant_prefs:
        parts.append("\nCUSTOMER PREFERENCES (remembered from past conversations):")
        for i, pref in enumerate(relevant_prefs, 1):
            parts.append(f"  {i}. {pref}")

    # --- Episodic Memory (Past Conversation Summaries) ---
    episodes = get_recent_episodes(phone, limit=5)
    if episodes:
        parts.append("\nPAST CONVERSATION HISTORY:")
        for ep in episodes:
            date_str = ep.get("date", "")
            summary = ep.get("summary", "")
            if summary:
                parts.append(f"  [{date_str}] {summary}")
            facts = ep.get("key_facts", [])
            if facts:
                parts.append(f"    Key facts: {', '.join(_memory_text_items(facts))}")

    if parts:
        return "CUSTOMER MEMORY:\n" + "\n".join(parts)
    return ""
