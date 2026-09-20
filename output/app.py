"""
RAG Output Pipeline - FastAPI Backend
Exposes POST /query that runs three parallel retrieval streams
(Documents, Images, Excel/SQL), consolidates via a final LLM call,
and returns the structured result to the frontend.
"""

import os
import re
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
from pydantic import BaseModel
from sqlalchemy import create_engine, text

# pyrefly: ignore [missing-import]
import chromadb
# pyrefly: ignore [missing-import]
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from prompts import (
    get_intent_extraction_prompt,
    get_sql_generation_prompt_fallback,
    get_final_consolidation_prompt,
    INTENT_JSON_SCHEMA,
)
from auth import verify_token

# --- Rate limiter setup ---
# pyrefly: ignore [missing-import]
from slowapi import Limiter, _rate_limit_exceeded_handler
# pyrefly: ignore [missing-import]
from slowapi.errors import RateLimitExceeded


def _get_real_ip(request: Request) -> str:
    """Use Cloudflare's real-IP header for tunnel traffic, else direct client IP."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    return request.client.host if request.client else "unknown"


limiter = Limiter(key_func=_get_real_ip)

# =================================================================
# CONFIG - loaded from .env (no more hardcoded secrets)
# =================================================================
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:18k")
# Optional: a faster/smaller model specifically for structured JSON extraction
# (thinking models like qwen3.5 are very slow at JSON; gemma4 is much faster)
OLLAMA_MODEL_FAST = os.getenv("OLLAMA_MODEL_FAST", OLLAMA_MODEL)
OLLAMA_TIMEOUT = 120  # generous timeout for local LLM
OLLAMA_TIMEOUT_THINK = 300  # thinking mode needs much more time on local hardware
OLLAMA_TIMEOUT_INTENT = 120  # intent extraction timeout

# Build MySQL URL from env vars
_mysql_user = os.getenv("MYSQL_USER", "rag_reader")
_mysql_pass = os.getenv("MYSQL_PASSWORD", "")
_mysql_host = os.getenv("MYSQL_HOST", "localhost")
_mysql_port = os.getenv("MYSQL_PORT", "3306")
_mysql_db   = os.getenv("MYSQL_DB", "excel_imports")
MYSQL_URL = f"mysql+pymysql://{_mysql_user}:{_mysql_pass}@{_mysql_host}:{_mysql_port}/{_mysql_db}"

# ChromaDB lives in the input/ folder - shared with the ingest pipeline
CHROMA_DIR = os.path.join(os.path.dirname(__file__), "..", "input", "chroma_data")

# History file for response persistence
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "history.json")
_history_lock = threading.Lock()

# =================================================================
# APP INIT (docs disabled for production)
# =================================================================
app = FastAPI(
    title="RAG Output Pipeline",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- CORS - locked down to allowed origins ---
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8002").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- Trusted Host middleware ---
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["localhost", "127.0.0.1", "*.trycloudflare.com"],
)


# --- Security response headers ---
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# --- Mount input profiles directory for crosscheck image thumbnails ---
_PROFILES_DIR = os.path.join(os.path.dirname(__file__), "..", "input", "profiles")
os.makedirs(_PROFILES_DIR, exist_ok=True)
app.mount("/static_profiles", StaticFiles(directory=_PROFILES_DIR), name="static_profiles")
# --- Preload LLM model on server startup ---
@app.on_event("startup")
async def _preload_model():
    """Load the LLM into VRAM as soon as the server starts.
    By the time the user types a query, the model is warm and ready."""
    import threading

    def _warmup():
        try:
            print(f"[Startup] Preloading model '{OLLAMA_MODEL_FAST}' into VRAM...")
            requests.post(
                f"{OLLAMA_URL}/api/generate",
                json={
                    "model": OLLAMA_MODEL_FAST,
                    "prompt": "",  # empty prompt, just load the model
                    "keep_alive": -1,
                },
                timeout=120,
            )
            print(f"[Startup] Model '{OLLAMA_MODEL_FAST}' is loaded and ready.")
        except Exception as e:
            print(f"[Startup] Model preload failed (non-fatal): {e}")

    # Run in background thread so it doesn't block server startup
    threading.Thread(target=_warmup, daemon=True).start()

# Serve frontend static files from the dedicated static/ subfolder only
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/")
async def serve_index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))


# Mount only the static/ subfolder
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# =================================================================
# HELPERS
# =================================================================

# Singleton ChromaDB client + embedding with retry logic.
# The input server (port 8001) may hold a transient SQLite lock on
# the same chroma_data directory. Retries handle this gracefully.

_chroma_client = None
_embedding_fn = None
CHROMA_MAX_RETRIES = 5
CHROMA_RETRY_DELAY = 1.0  # seconds, doubles each retry


def _get_chroma_client():
    global _chroma_client
    if _chroma_client is not None:
        return _chroma_client

    last_err = None
    for attempt in range(CHROMA_MAX_RETRIES):
        try:
            _chroma_client = chromadb.PersistentClient(
                path=os.path.abspath(CHROMA_DIR)
            )
            return _chroma_client
        except Exception as e:
            last_err = e
            wait = CHROMA_RETRY_DELAY * (2 ** attempt)
            print(f"[ChromaDB] Connection attempt {attempt+1}/{CHROMA_MAX_RETRIES} failed: {e}. Retrying in {wait:.1f}s...")
            time.sleep(wait)

    raise RuntimeError(f"ChromaDB connection failed after {CHROMA_MAX_RETRIES} retries: {last_err}")


def _get_embedding_fn():
    global _embedding_fn
    if _embedding_fn is None:
        _embedding_fn = DefaultEmbeddingFunction()
    return _embedding_fn


def _get_collection(name: str):
    """Get a ChromaDB collection with retry logic for transient lock errors."""
    last_err = None
    for attempt in range(CHROMA_MAX_RETRIES):
        try:
            client = _get_chroma_client()
            return client.get_or_create_collection(
                name=name, embedding_function=_get_embedding_fn()
            )
        except Exception as e:
            last_err = e
            # Reset client on connection errors so next retry creates fresh one
            global _chroma_client
            _chroma_client = None
            wait = CHROMA_RETRY_DELAY * (2 ** attempt)
            print(f"[ChromaDB] Collection '{name}' attempt {attempt+1}/{CHROMA_MAX_RETRIES} failed: {e}. Retrying in {wait:.1f}s...")
            time.sleep(wait)

    raise RuntimeError(f"Failed to access collection '{name}' after {CHROMA_MAX_RETRIES} retries: {last_err}")


def _call_ollama(
    prompt: str, think: bool = False,
    json_mode: bool = False, custom_timeout: int = None
) -> str:
    """Send a text-only prompt to the local Ollama and return the response.

    Args:
        prompt: The prompt string.
        think: Whether to enable thinking mode (longer timeout).
        json_mode: If True, forces the LLM to output valid JSON (fast mode).
        custom_timeout: Override the default timeout in seconds.
    """
    if custom_timeout:
        timeout = custom_timeout
    elif think:
        timeout = OLLAMA_TIMEOUT_THINK
    else:
        timeout = OLLAMA_TIMEOUT

    body = {
        "model": OLLAMA_MODEL_FAST,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": -1,
        "think": False,  # ALWAYS disable — gemma4 is a thinking model, don't waste time
    }
    if json_mode:
        body["format"] = "json"
        body["options"] = {"temperature": 0}
    try:
        r = requests.post(
            f"{OLLAMA_URL}/api/chat", json=body, timeout=timeout
        )
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        # Strip <think>...</think> tags
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        content = re.sub(r"</think>", "", content)
        return content.strip()
    except Exception as e:
        return f"LLM Error: {e}"


def _load_history() -> list[str]:
    with _history_lock:
        if not os.path.exists(HISTORY_FILE):
            return []
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data[-5:]
            return []
        except Exception:
            return []


def _save_history(history: list[str]):
    with _history_lock:
        # keep only last 5
        trimmed = history[-5:]
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed, f, ensure_ascii=False, indent=2)


def _get_mysql_engine():
    try:
        # For the read-only user, we skip the CREATE DATABASE step
        # (rag_reader won't have CREATE privileges)
        # Just connect directly to the database
        if _mysql_user != "root":
            return create_engine(MYSQL_URL)

        # Root user - create database if needed (input server behavior)
        server_url = f"mysql+pymysql://{_mysql_user}:{_mysql_pass}@{_mysql_host}:{_mysql_port}/"
        server_engine = create_engine(server_url)
        with server_engine.begin() as conn:
            conn.execute(text("CREATE DATABASE IF NOT EXISTS excel_imports"))
        return create_engine(MYSQL_URL)
    except Exception as e:
        print(f"DB Connection Error: {e}")
        raise e


# =================================================================
# STREAM A - DOCUMENT RETRIEVAL (Doc_store)
# =================================================================

def stream_document(query: str, query_embedding: list[float]) -> dict:
    """Query Doc_store for top 5 chunks. NO LLM call — raw chunks go to
    final consolidation to save an Ollama round-trip (~30s)."""
    try:
        collection = _get_collection("Doc_store")

        results = collection.query(
            query_embeddings=[query_embedding], n_results=5
        )

        chunks = results.get("documents", [[]])[0]
        metadatas = results.get("metadatas", [[]])[0]

        if not chunks:
            return {"chunks": []}

        # Build chunk list with metadata for the UI
        chunk_cards = []
        for i, (chunk, meta) in enumerate(zip(chunks, metadatas)):
            chunk_cards.append({
                "index": i + 1,
                "text": chunk,
                "source": meta.get("source", "unknown") if meta else "unknown",
            })

        return {"chunks": chunk_cards}

    except Exception as e:
        return {"chunks": [], "error": f"Stream A Error: {e}"}


# =================================================================
# STREAM B - IMAGE / ENTITY RETRIEVAL (Image_store)
# =================================================================

def stream_image(query: str, query_embedding: list[float]) -> dict:
    """Query Image_store for top 7, find most frequent person, get all
    chunks for that person. NO LLM call — raw data goes to final
    consolidation to save an Ollama round-trip (~30s)."""
    try:
        collection = _get_collection("Image_store")

        # Top 7 similarity search
        results = collection.query(
            query_embeddings=[query_embedding], n_results=7
        )

        top7_docs = results.get("documents", [[]])[0]
        top7_metas = results.get("metadatas", [[]])[0]
        top7_distances = results.get("distances", [[]])[0]

        if not top7_docs:
            return {
                "top7_chunks": [],
                "top_name": None,
                "all_name_chunks": [],
            }

        # Build top 7 chunk cards
        top7_cards = []
        for i, (doc, meta) in enumerate(zip(top7_docs, top7_metas)):
            top7_cards.append({
                "index": i + 1,
                "text": doc,
                "person": meta.get("person", "unknown") if meta else "unknown",
                "source": meta.get("source", "unknown") if meta else "unknown",
            })

        # Count frequency of each person name
        name_freq: dict[str, int] = {}
        name_scores: dict[str, list[float]] = {}
        for meta, dist in zip(top7_metas, top7_distances):
            person = meta.get("person", "unknown") if meta else "unknown"
            name_freq[person] = name_freq.get(person, 0) + 1
            name_scores.setdefault(person, []).append(dist)

        # Find top name (highest frequency, tiebreak by lowest average distance)
        top_name = max(
            name_freq.keys(),
            key=lambda n: (name_freq[n], -sum(name_scores[n]) / len(name_scores[n]))
        )

        # Get ALL chunks for this person (metadata filter, no vector search)
        all_results = collection.get(
            where={"person": top_name},
        )

        all_docs = all_results.get("documents", [])
        all_metas = all_results.get("metadatas", [])

        all_name_cards = []
        for i, (doc, meta) in enumerate(zip(all_docs, all_metas)):
            all_name_cards.append({
                "index": i + 1,
                "text": doc,
                "person": meta.get("person", "unknown") if meta else "unknown",
                "source": meta.get("source", "unknown") if meta else "unknown",
            })

        return {
            "top7_chunks": top7_cards,
            "top_name": top_name,
            "all_name_chunks": all_name_cards,
        }

    except Exception as e:
        return {
            "top7_chunks": [],
            "top_name": None,
            "all_name_chunks": [],
            "error": f"Stream B Error: {e}",
        }


# =================================================================
# STREAM C - STRUCTURED / EXCEL RETRIEVAL (Excel_store -> MySQL)
#
# Three-step pipeline:
#   0. Multi-signal table routing (keyword + token + schema + vector)
#   1. LLM extracts structured intent (JSON) - what to query
#   2. Python builds SQL deterministically  - how to query
# =================================================================

from tokenizer import generate_token

# System tables to exclude from table scoring
_SYSTEM_TABLES = frozenset({
    "entity_tokens", "rep_doctor_lists", "rep_visits",
})


def _score_candidate_tables(
    query: str, query_embedding: list[float]
) -> list[tuple[str, float, dict]]:
    """Score all user tables using 4 signals and return sorted results.

    Returns:
        List of (table_name, total_score, score_breakdown) sorted by score desc.
        score_breakdown is a dict with keys: keyword, token, schema, vector.
    """
    scores: dict[str, dict] = {}  # table -> {keyword, token, schema, vector}
    query_lower = query.lower()
    query_words = set(query_lower.split())

    # Tokenize significant words from the query (skip short ones)
    query_tokens = []
    for w in query.split():
        if len(w) > 1:
            t = generate_token(w)
            if t:
                query_tokens.append(t)

    try:
        engine = _get_mysql_engine()
        with engine.connect() as conn:
            # Get all user tables
            all_tables = [
                r[0] for r in conn.execute(text("SHOW TABLES")).fetchall()
            ]
            all_tables = [t for t in all_tables if t not in _SYSTEM_TABLES]

            if not all_tables:
                return []

            for tbl in all_tables:
                scores[tbl] = {"keyword": 0, "token": 0, "schema": 0, "vector": 0}

            # --- Signal 1: Keyword match on table name (+10) ---
            for tbl in all_tables:
                tbl_words = tbl.lower().replace('_', ' ').split()
                for tw in tbl_words:
                    if len(tw) > 2 and tw in query_lower:
                        scores[tbl]["keyword"] = 10.0
                        break

            # --- Signal 2: Token registry hit (+5 per matching token) ---
            # Build case-insensitive lookup: entity_tokens may store table
            # names with different capitalisation than SHOW TABLES returns
            # (e.g. "Sales data" vs "sales data" on Windows MySQL).
            scores_lower = {tbl.lower(): tbl for tbl in scores}

            if query_tokens:
                for qt in query_tokens:
                    # Strip underscores so partial tokens match:
                    # e.g. query "sk" → token "SK" → clean "SK"
                    #       stored token "S_K_RAI" → clean "SKRAI"
                    #       "SKRAI" LIKE '%SK%' → MATCH ✅
                    clean_qt = qt.replace("_", "")
                    if not clean_qt:
                        continue
                    try:
                        hits = conn.execute(text(
                            "SELECT DISTINCT table_name FROM entity_tokens "
                            "WHERE REPLACE(token, '_', '') LIKE :pattern"
                        ), {"pattern": f"%{clean_qt}%"}).fetchall()
                        for (hit_tbl,) in hits:
                            # Case-insensitive match against scored tables
                            canonical = scores_lower.get(hit_tbl.lower())
                            if canonical:
                                scores[canonical]["token"] += 5.0
                    except Exception:
                        pass  # entity_tokens may not exist yet

            # --- Signal 3: Schema relevance (+5) ---
            # Check if query words match column names in each table
            for tbl in all_tables:
                try:
                    cols = conn.execute(text(f"SHOW COLUMNS FROM `{tbl}`"))
                    col_names_raw = [r[0] for r in cols.fetchall()]
                    # Skip _token columns for schema matching
                    col_names_raw = [c for c in col_names_raw if not c.endswith("_token")]
                    for col in col_names_raw:
                        col_words = col.lower().replace('_', ' ').split()
                        if any(w in query_words for w in col_words if len(w) > 2):
                            scores[tbl]["schema"] = 5.0
                            break
                except Exception:
                    pass

    except Exception as e:
        print(f"[TableScore] DB error during scoring: {e}")

    # --- Signal 4: Vector similarity from ChromaDB (0-5 range) ---
    try:
        collection = _get_collection("Excel_store")
        results = collection.query(
            query_embeddings=[query_embedding], n_results=min(5, max(1, len(scores)))
        )
        metas_list = results.get("metadatas", [[]])[0]
        dists_list = results.get("distances", [[]])[0]

        for meta, dist in zip(metas_list, dists_list):
            tbl = meta.get("source", "")
            if tbl in scores:
                # Lower distance = better match; normalize to 0-5
                similarity = max(0.0, (2.0 - dist) / 2.0) * 5.0
                scores[tbl]["vector"] = max(scores[tbl]["vector"], similarity)
    except Exception as e:
        print(f"[TableScore] ChromaDB error: {e}")

    # Build ranked list
    ranked = []
    for tbl, breakdown in scores.items():
        total = sum(breakdown.values())
        ranked.append((tbl, total, breakdown))
    ranked.sort(key=lambda x: -x[1])

    return ranked

def _is_varchar_numeric(dtype: str) -> bool:
    """Check if a MySQL column type is text-based (varchar, text, etc.)."""
    dtype_lower = dtype.lower()
    return any(t in dtype_lower for t in ("text", "varchar", "char", "longtext", "mediumtext"))


def build_sql_from_intent(intent: dict, table_name: str, col_info: list[tuple]) -> str:
    """Build a syntactically correct MySQL query from the LLM's structured intent.

    Args:
        intent: Parsed JSON from the LLM with keys:
            answerable, filter_keywords, filter_column,
            select_columns, aggregation, exclude_values
        table_name: The MySQL table name.
        col_info: List of (column_name, column_type) tuples from SHOW COLUMNS.

    Returns:
        A valid MySQL SELECT query string.
    """
    # Build a lookup for column types
    col_type_map = {name: dtype for name, dtype in col_info}
    valid_columns = set(col_type_map.keys())

    # -- Defensive: flatten any nested lists and ensure all values are strings --
    def _flatten_strings(val) -> list[str]:
        """Ensure val is a flat list of strings, regardless of LLM output shape."""
        if isinstance(val, str):
            return [val]
        if isinstance(val, list):
            result = []
            for item in val:
                if isinstance(item, str):
                    result.append(item)
                elif isinstance(item, list):
                    result.extend(_flatten_strings(item))
                else:
                    result.append(str(item))
            return result
        return [str(val)] if val else []

    # -- SELECT clause --
    select_columns = _flatten_strings(intent.get("select_columns", []))
    aggregation = str(intent.get("aggregation", "none"))

    # Validate column names - only use columns that actually exist
    validated_select = [c for c in select_columns if c in valid_columns]
    if not validated_select:
        # Fallback: if LLM picked bad column names, select all
        validated_select = list(valid_columns)

    # Always include the filter column so results are labeled
    # (e.g., "Party" alongside "Aug" so LLM knows which entity each value belongs to)
    filter_column = str(intent.get("filter_column", ""))
    if (filter_column and filter_column in valid_columns
            and filter_column not in validated_select
            and aggregation == "none"):
        validated_select.insert(0, filter_column)

    select_parts = []
    for col in validated_select:
        dtype = col_type_map.get(col, "")
        # If it's a varchar column that holds numbers (like 'Jul', 'Aug', 'Total_Value'),
        # and we're doing aggregation, cast to decimal
        if aggregation != "none" and _is_varchar_numeric(dtype):
            expr = f"CAST(NULLIF(REPLACE(`{col}`, '-', ''), '') AS DECIMAL(15,2))"
        else:
            expr = f"`{col}`"

        if aggregation != "none" and aggregation != "COUNT":
            select_parts.append(f"{aggregation}({expr}) AS `{aggregation}_{col}`")
        elif aggregation == "COUNT":
            select_parts.append(f"COUNT(*) AS `count`")
            break  # COUNT(*) only needs one entry
        else:
            select_parts.append(expr)

    select_clause = ", ".join(select_parts)

    # -- WHERE clause --
    where_parts = []

    # Filter by keywords (defensively flatten)
    filter_keywords = _flatten_strings(intent.get("filter_keywords", []))
    filter_column = str(intent.get("filter_column", ""))

    if filter_keywords and filter_column and filter_column in valid_columns:
        # Check if a _token column exists for this filter column
        token_col = f"{filter_column}_token"
        has_token_col = token_col in valid_columns

        # Build set of column name variants for collision detection
        # e.g. column "Jul" → {"jul", "july"} so "july" won't be used as a Party filter
        col_name_variants = set()
        for cname, _ in col_info:
            cn_lower = cname.lower().rstrip("_")
            col_name_variants.add(cn_lower)
            # Common month/word expansions
            _month_map = {
                "jan": "january", "feb": "february", "mar": "march",
                "apr": "april", "may": "may", "jun": "june",
                "jul": "july", "aug": "august", "sep": "september",
                "oct": "october", "nov": "november", "dec": "december",
            }
            if cn_lower in _month_map:
                col_name_variants.add(_month_map[cn_lower])

        for kw in filter_keywords:
            # Skip keywords that look like column names (e.g., "july" ≈ "Jul" column)
            if kw.lower() in col_name_variants:
                print(f"[SQL Builder] Skipping filter keyword '{kw}' — matches column name")
                continue

            # Escape single quotes in keyword to prevent SQL injection
            safe_kw = kw.replace("'", "''")

            if has_token_col:
                # Token-aware: query both original and tokenized columns
                # Strip underscores so partial tokens match:
                # e.g. query "sk" → token "SK" → clean "SK"
                #       stored "S_K_RAI" → REPLACE → "SKRAI"
                #       "SKRAI" LIKE '%SK%' → MATCH ✅
                token_kw = generate_token(safe_kw)
                if token_kw:
                    clean_token = token_kw.replace("_", "")
                    where_parts.append(
                        f"(REPLACE(`{token_col}`, '_', '') LIKE '%{clean_token}%' "
                        f"OR `{filter_column}` LIKE '%{safe_kw}%')"
                    )
                else:
                    where_parts.append(f"`{filter_column}` LIKE '%{safe_kw}%'")
            else:
                where_parts.append(f"`{filter_column}` LIKE '%{safe_kw}%'")

    # Exclude summary rows
    exclude_values = _flatten_strings(intent.get("exclude_values", []))
    if exclude_values and filter_column and filter_column in valid_columns:
        # Use the first column (usually the label column) for exclusion
        first_col = col_info[0][0] if col_info else filter_column
        for val in exclude_values:
            safe_val = val.replace("'", "''")
            where_parts.append(f"`{first_col}` != '{safe_val}'")

    # -- Assemble --
    sql = f"SELECT {select_clause} FROM `{table_name}`"
    if where_parts:
        sql += f" WHERE {' AND '.join(where_parts)}"

    return sql


def _run_intent_pipeline(
    query: str, table_name: str, schema_str: str,
    col_info: list[tuple], column_names: list[str],
    sample_rows_str: str, tail_rows_str: str
) -> tuple:
    """Run the intent extraction + SQL builder pipeline.

    Returns:
        (sql_query: str or None, error_msg: str or None)
    """
    # Step 1: Ask LLM to extract intent as structured JSON
    intent_prompt = get_intent_extraction_prompt(
        query, table_name, schema_str,
        column_names, sample_rows_str, tail_rows_str
    )

    raw_response = _call_ollama(
        intent_prompt, think=False,
        json_mode=True, custom_timeout=OLLAMA_TIMEOUT_INTENT
    )

    if raw_response.startswith("LLM Error:"):
        return None, f"Intent extraction failed: {raw_response}"

    # Step 2: Parse the JSON (should always be valid due to schema enforcement)
    try:
        intent = json.loads(raw_response)
    except json.JSONDecodeError as e:
        # Try to extract JSON from the response (e.g., wrapped in markdown)
        json_match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if json_match:
            try:
                intent = json.loads(json_match.group())
            except json.JSONDecodeError:
                return None, f"Could not parse intent JSON: {e}"
        else:
            return None, f"Could not parse intent JSON: {e}"

    print(f"[Stream C] Intent: {json.dumps(intent, indent=2)}")

    # Step 3: Check if there's usable data
    # Don't trust 'answerable' blindly — the model often sets it to false
    # even when it correctly identifies filter_column and keywords.
    # Only reject if there's genuinely NOTHING to work with.
    has_keywords = bool(intent.get("filter_keywords"))
    has_column = bool(intent.get("filter_column"))
    if not has_keywords and not has_column:
        return None, "No usable filter data in intent — cannot build SQL."

    # Step 4: Build SQL deterministically
    sql = build_sql_from_intent(intent, table_name, col_info)

    return sql, None


def _run_fallback_sql_pipeline(
    query: str, table_name: str, schema_str: str,
    sample_rows_str: str, tail_rows_str: str
) -> tuple:
    """Fallback: ask LLM to generate raw SQL (old approach).

    Returns:
        (sql_query: str or None, error_msg: str or None)
    """
    sql_prompt = get_sql_generation_prompt_fallback(
        query, table_name, schema_str, sample_rows_str, tail_rows_str
    )
    raw_sql = _call_ollama(sql_prompt, think=False)

    if raw_sql.startswith("LLM Error:"):
        return None, f"Fallback SQL generation failed: {raw_sql}"

    if raw_sql.strip().lower() == "nothing":
        return None, "LLM determined no relevant SQL could be formed."

    # Sanitize
    cleaned = raw_sql.strip()
    cleaned = re.sub(r"^```(?:sql)?", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"```$", "", cleaned)
    cleaned = cleaned.strip().strip("`").strip()

    # Fix table name quoting
    if table_name and " " in table_name:
        for bad_quote in ["'", '"']:
            cleaned = cleaned.replace(
                f"{bad_quote}{table_name}{bad_quote}", f"`{table_name}`"
            )

    if not cleaned.lower().startswith(("select", "show", "describe", "with")):
        return None, f"LLM returned invalid SQL: {cleaned[:200]}"

    return cleaned, None


def stream_excel(query: str, query_embedding: list[float]) -> dict:
    """Query Excel_store for schema chunk, extract intent via LLM,
    build SQL programmatically, execute against MySQL.

    Uses multi-signal table routing: keyword + token + schema + vector.
    """
    try:
        # Step 0: Score all candidate tables
        ranked = _score_candidate_tables(query, query_embedding)

        if not ranked or ranked[0][1] == 0:
            return {
                "chunk": None,
                "sql_query": None,
                "sql_result": None,
                "llm_output": "No Excel/table data found.",
            }

        table_name = ranked[0][0]
        top_score = ranked[0][1]
        top_breakdown = ranked[0][2]
        print(
            f"[Stream C] Table selected: `{table_name}` "
            f"(score={top_score:.1f}, breakdown={top_breakdown})"
        )
        if len(ranked) > 1:
            runner_up = ranked[1]
            print(
                f"[Stream C] Runner-up: `{runner_up[0]}` "
                f"(score={runner_up[1]:.1f}, breakdown={runner_up[2]})"
            )

        # Fetch text slab from Chroma for context (best-effort)
        text_slab = ""
        try:
            collection = _get_collection("Excel_store")
            chroma_results = collection.get(where={"source": table_name})
            if chroma_results and chroma_results.get("documents"):
                text_slab = chroma_results["documents"][0]
        except Exception:
            pass

        if not text_slab:
            # Fallback: try vector search
            try:
                collection = _get_collection("Excel_store")
                vr = collection.query(
                    query_embeddings=[query_embedding], n_results=1
                )
                if vr.get("documents", [[]])[0]:
                    text_slab = vr["documents"][0][0]
            except Exception:
                pass

        # -- Gather context from MySQL (schema + sample rows + tail rows) --
        schema_str = ""
        sample_rows_str = ""
        tail_rows_str = ""
        col_info = []        # [(col_name, col_type), ...]
        column_names = []    # [col_name, ...]

        try:
            engine = _get_mysql_engine()
            with engine.connect() as conn:
                # Actual column names + types (includes _token columns)
                col_result = conn.execute(text(f"SHOW COLUMNS FROM `{table_name}`"))
                col_info = [(row[0], row[1]) for row in col_result.fetchall()]
                
                # For intent extraction: hide _token columns from LLM
                # (it only needs to see user-facing columns)
                user_col_info = [
                    (name, dtype) for name, dtype in col_info
                    if not name.endswith("_token")
                ]
                column_names = [name for name, _ in user_col_info]
                if user_col_info:
                    schema_str = ", ".join(
                        f"`{name}` {dtype}" for name, dtype in user_col_info
                    )

                # Build SELECT clause excluding _token columns for LLM samples
                user_cols_sql = ", ".join(f"`{name}`" for name in column_names)
                if not user_cols_sql:
                    user_cols_sql = "*"

                # 5 sample rows from top (skip row 0 which may be header)
                sample_result = conn.execute(
                    text(f"SELECT {user_cols_sql} FROM `{table_name}` LIMIT 5 OFFSET 1")
                )
                s_cols = list(sample_result.keys())
                s_data = [dict(zip(s_cols, r)) for r in sample_result.fetchall()]
                if s_data:
                    import json as _json
                    sample_rows_str = _json.dumps(s_data, default=str, indent=2)

                # Last 5 rows (summary rows typically live here)
                count_r = conn.execute(text(f"SELECT COUNT(*) FROM `{table_name}`"))
                total = count_r.scalar()
                if total and total > 5:
                    tail_r = conn.execute(
                        text(f"SELECT {user_cols_sql} FROM `{table_name}` LIMIT 5 OFFSET {max(0, total - 5)}")
                    )
                    t_cols = list(tail_r.keys())
                    t_data = [dict(zip(t_cols, r)) for r in tail_r.fetchall()]
                    if t_data:
                        import json as _json
                        tail_rows_str = _json.dumps(t_data, default=str, indent=2)

        except Exception as e:
            print(f"[Stream C] MySQL context error for `{table_name}`: {e}")
            return {
                "chunk": text_slab, "sql_query": None,
                "sql_result": None,
                "llm_output": f"Could not access MySQL table `{table_name}`: {e}",
            }

        # ==============================================================
        # STEP 2: Intent Extraction -> Programmatic SQL (primary path)
        # Falls back to raw SQL generation if intent extraction fails
        # ==============================================================
        cleaned_sql, error = _run_intent_pipeline(
            query, table_name, schema_str,
            col_info, column_names,
            sample_rows_str, tail_rows_str
        )

        if error:
            print(f"[Stream C] Intent pipeline failed: {error}. Trying fallback...")
            cleaned_sql, fallback_error = _run_fallback_sql_pipeline(
                query, table_name, schema_str,
                sample_rows_str, tail_rows_str
            )
            if fallback_error:
                return {
                    "chunk": text_slab, "sql_query": None,
                    "sql_result": None,
                    "llm_output": f"Both intent and fallback SQL failed. Intent: {error} | Fallback: {fallback_error}",
                }

        print(f"[Stream C] SQL: {cleaned_sql[:200]}")

        # ==============================================================
        # STEP 3: Execute SQL with retry
        # ==============================================================
        sql_result = None
        llm_output = ""
        max_sql_attempts = 2

        for sql_attempt in range(max_sql_attempts):
            try:
                engine = _get_mysql_engine()
                with engine.connect() as conn:
                    result = conn.execute(text(cleaned_sql))
                    columns = list(result.keys())
                    rows = [dict(zip(columns, row)) for row in result.fetchall()]
                    sql_result = rows if rows else []

                if sql_result:
                    llm_output = f"SQL returned {len(sql_result)} row(s). Data passed to final consolidation."
                else:
                    llm_output = "The SQL query executed successfully but returned no rows."
                break

            except Exception as e:
                error_msg = str(e)
                if sql_attempt < max_sql_attempts - 1:
                    # On SQL execution error, try the fallback pipeline
                    print(f"[Stream C] SQL execution error: {error_msg}. Trying fallback SQL...")
                    cleaned_sql, fallback_error = _run_fallback_sql_pipeline(
                        query, table_name, schema_str,
                        sample_rows_str, tail_rows_str
                    )
                    if fallback_error:
                        sql_result = f"SQL Error: {error_msg}"
                        llm_output = f"SQL execution failed: {error_msg}"
                        break
                    print(f"[Stream C] Fallback SQL: {cleaned_sql[:100]}...")
                else:
                    sql_result = f"SQL Error: {error_msg}"
                    llm_output = f"SQL execution failed after {max_sql_attempts} attempts: {error_msg}"

        # Convert any non-serializable values in sql_result
        if isinstance(sql_result, list):
            for row in sql_result:
                for k, v in row.items():
                    if not isinstance(v, (str, int, float, bool, type(None))):
                        row[k] = str(v)

        return {
            "chunk": text_slab,
            "sql_query": cleaned_sql,
            "sql_result": sql_result,
            "llm_output": llm_output,
        }

    except Exception as e:
        return {
            "chunk": None,
            "sql_query": None,
            "sql_result": None,
            "llm_output": f"Stream C Error: {e}",
        }


# =================================================================
# CONTEXT WINDOW GUARD
# =================================================================

MAX_TOKENS = 28000
CHARS_PER_TOKEN = 4
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN  # 112,000


def _truncate_for_context(query: str, output1: str, output2: str, output3: str, history: list[str]) -> tuple:
    """Truncate outputs if total prompt would exceed context window."""
    base_chars = len(query) + 500  # prompt template overhead
    hist_chars = sum(len(h) for h in history)
    total = base_chars + hist_chars + len(output1) + len(output2) + len(output3 or "")

    if total <= MAX_CHARS:
        return output1, output2, output3

    budget = MAX_CHARS - base_chars - hist_chars
    if budget <= 0:
        return output1[:1000], output2[:1000], (output3 or "")[:1000]

    # All three sources carry equal weight (any may have the answer)
    o3_len = len(output3 or "")
    o1_len = len(output1)
    o2_len = len(output2)

    # Give equal budget to all three sources (each may contain critical evidence)
    o3_budget = int(budget * 0.34)
    o1_budget = int(budget * 0.33)
    o2_budget = int(budget * 0.33)

    return (
        output1[:o1_budget] if o1_len > o1_budget else output1,
        output2[:o2_budget] if o2_len > o2_budget else output2,
        (output3 or "")[:o3_budget] if o3_len > o3_budget else output3,
    )


# =================================================================
# MAIN ENDPOINT
# =================================================================

class QueryRequest(BaseModel):
    query: str


@app.post("/query")
@limiter.limit("10/minute")
async def handle_query(request: Request, req: QueryRequest, _=Depends(verify_token)):
    query = req.query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query cannot be empty.")

    # Step 2: Embed the query
    try:
        embed_fn = _get_embedding_fn()
        query_embedding = embed_fn([query])[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding error: {e}")

    # Step 3: Run three streams concurrently
    stream_results: dict[str, Any] = {}

    def _run_stream(name, fn, *args):
        try:
            return name, fn(*args)
        except Exception as e:
            return name, {"error": str(e)}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_run_stream, "doc", stream_document, query, query_embedding),
            pool.submit(_run_stream, "image", stream_image, query, query_embedding),
            pool.submit(_run_stream, "excel", stream_excel, query, query_embedding),
        ]
        for future in as_completed(futures):
            name, result = future.result()
            stream_results[name] = result

    doc_result = stream_results.get("doc", {})
    img_result = stream_results.get("image", {})
    excel_result = stream_results.get("excel", {})

    # --- Build condensed text summaries from raw chunks ---
    # (Avoids needle-in-haystack: we condense programmatically, not with LLM)

    # Doc summary: take first 300 chars of each chunk with source label
    doc_chunks = doc_result.get("chunks", [])
    if doc_chunks:
        doc_parts = []
        for c in doc_chunks[:5]:  # max 5 chunks
            snippet = c["text"][:300].strip()
            source = c.get("source", "unknown")
            doc_parts.append(f"[Source: {source}] {snippet}")
        doc_summary = "\n---\n".join(doc_parts)
    else:
        doc_summary = "No relevant documents found."

    # Image summary: entity name + ALL entity chunks (each is ~200 chars, fits easily)
    top_name = img_result.get("top_name")
    img_all_chunks = img_result.get("all_name_chunks", [])
    if top_name and img_all_chunks:
        img_parts = [f"Top identified entity: {top_name} ({len(img_all_chunks)} records)"]
        for c in img_all_chunks:  # send ALL — each is small
            snippet = c["text"][:300].strip()
            source = c.get("source", "")
            img_parts.append(f"  - [Source: {source}] {snippet}")
        img_summary = "\n".join(img_parts)
    else:
        img_summary = "No relevant information found in image records."

    # Excel summary: SQL result + interpretation (already compact)
    sql_result_raw = excel_result.get("sql_result")
    excel_llm = excel_result.get("llm_output", "")
    if sql_result_raw and isinstance(sql_result_raw, list):
        excel_output = f"SQL Result: {json.dumps(sql_result_raw[:30], default=str)}\nInterpretation: {excel_llm}"
    elif sql_result_raw and isinstance(sql_result_raw, str):
        excel_output = f"{sql_result_raw}\n{excel_llm}"
    else:
        excel_output = excel_llm if excel_llm else None

    # Step 4: Load history
    history = _load_history()

    # Step 5: Context window guard + final LLM call
    doc_summary, img_summary, excel_output = _truncate_for_context(
        query, doc_summary, img_summary, excel_output, history
    )

    final_prompt = get_final_consolidation_prompt(
        query, doc_summary, img_summary, excel_output, history
    )
    print(f"[Final] Excel data for consolidation: {str(excel_output)[:500]}")
    print(f"[Final] Image data for consolidation ({len(img_all_chunks)} chunks): {img_summary[:800]}")
    final_answer = _call_ollama(final_prompt)

    # Append new answer to history - skip if it contains error messages
    error_keywords = ["LLM Error:", "Stream A Error:", "Stream B Error:", "Stream C Error:", "SQL Error:"]
    if not any(kw in final_answer for kw in error_keywords):
        history.append(final_answer)
        _save_history(history)

    # Build response
    response = {
        "final_answer": final_answer,
        "stream_doc": {
            "chunks": doc_result.get("chunks", []),
        },
        "stream_image": {
            "top7_chunks": img_result.get("top7_chunks", []),
            "top_name": img_result.get("top_name"),
            "all_name_chunks": img_result.get("all_name_chunks", []),
        },
        "stream_excel": {
            "chunk": excel_result.get("chunk"),
            "sql_query": excel_result.get("sql_query"),
            "sql_result": excel_result.get("sql_result"),
            "llm_output": excel_result.get("llm_output", ""),
        },
        "history_snapshot": _load_history(),
    }

    return response


# =================================================================
# CROSSCHECK — Deterministic Doctor Visit Analysis
# =================================================================

from math import radians, cos, sin, asin, sqrt

PROXIMITY_THRESHOLD_METERS = 50  # same as input side


def _haversine_meters(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS points."""
    R = 6_371_000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))


@app.get("/crosscheck/filters")
async def crosscheck_filters(_=Depends(verify_token)):
    """Return available profiles and their month_year values for the crosscheck UI."""
    try:
        engine = _get_mysql_engine()
        with engine.connect() as conn:
            # Get profiles that have either a doctor list or visits
            profiles_result = conn.execute(text(
                "SELECT DISTINCT profile FROM rep_doctor_lists "
                "UNION SELECT DISTINCT profile FROM rep_visits"
            ))
            profiles = sorted(set(r[0] for r in profiles_result.fetchall()))

            # For each profile, get available months from rep_visits
            months_map = {}
            for p in profiles:
                months_result = conn.execute(text(
                    "SELECT DISTINCT month_year FROM rep_visits WHERE profile = :p ORDER BY month_year"
                ), {"p": p})
                months_map[p] = [r[0] for r in months_result.fetchall()]

        return {"profiles": profiles, "months": months_map}
    except Exception as e:
        return {"profiles": [], "months": {}, "error": str(e)}


# ── Doctor name normalization for abbreviation-tolerant matching ──
import re as _re

def _normalize_doctor_name(name: str):
    """Normalize a doctor name to (SURNAME, [initials]).

    Pipeline:
    1. Strip all dots  (P.K. → PK)
    2. Remove 'Dr' prefix
    3. Uppercase + split into tokens
    4. Expand short tokens (≤2 chars) into per-char initials (PK → P, K)
    5. Last token = surname, rest → first-letter initials
    """
    n = name.replace(".", "").strip()
    # Strip leading "Dr" (case-insensitive)
    n = _re.sub(r'^(?:DR|Dr|dr)\s*', '', n).strip()
    tokens = n.upper().split()
    if not tokens:
        return ("", [])

    # Expand short tokens: "PK" → ["P", "K"]
    expanded = []
    for t in tokens:
        if len(t) <= 2 and t.isalpha():
            expanded.extend(list(t))  # each char becomes a token
        else:
            expanded.append(t)

    surname = expanded[-1]
    initials = [t[0] for t in expanded[:-1]]
    return (surname, initials)


def _doctor_names_match(name_a: str, name_b: str) -> bool:
    """Check if two doctor names match using surname + initials comparison."""
    s1, i1 = _normalize_doctor_name(name_a)
    s2, i2 = _normalize_doctor_name(name_b)
    if not s1 or not s2:
        return False
    if s1 != s2:
        return False
    # Surnames match — check initials are compatible
    # Initials lists must have the same length AND match element-by-element.
    # e.g. ["G","B"] == ["G","B"] ✓  (G.B. Biswa vs Gobind Bind Biswa)
    #      ["M","P"] != ["M"]     ✗  (MP Verma vs Mirdula Verma)
    if len(i1) != len(i2):
        return False
    return all(a == b for a, b in zip(i1, i2))


@app.get("/crosscheck")
async def crosscheck(profile: str, month_year: str, _=Depends(verify_token)):
    """Run deterministic crosscheck: compare working list vs actual visits.

    Returns three lists:
    1. met_doctors — on the list AND have visit images (with count)
    2. not_met_doctors — on the list but NO visit images
    3. unmatched_visits — have visit images but NOT on the working list
    """
    try:
        engine = _get_mysql_engine()
        with engine.connect() as conn:
            # 1. Load working list
            list_result = conn.execute(text(
                "SELECT doctor_name FROM rep_doctor_lists WHERE profile = :p"
            ), {"p": profile})
            working_list = [r[0] for r in list_result.fetchall()]

            # 2. Load visits for this month
            visits_result = conn.execute(text(
                "SELECT doctor_name, latitude, longitude, visit_date, image_filename "
                "FROM rep_visits WHERE profile = :p AND month_year = :m "
                "ORDER BY doctor_name, visit_date"
            ), {"p": profile, "m": month_year})
            visits = [
                {"name": r[0], "lat": r[1], "lng": r[2], "date": str(r[3]) if r[3] else None, "image": r[4]}
                for r in visits_result.fetchall()
            ]

        if not working_list:
            return {
                "profile": profile, "month_year": month_year,
                "total_in_list": 0, "total_met": 0, "total_not_met": 0,
                "met_doctors": [], "not_met_doctors": [],
                "unmatched_visits": [],
                "warning": "No doctor working list found for this profile. Upload one from the Input page."
            }

        # 3. Build visit lookup: group by doctor name
        visit_groups = {}  # name -> [visit_info, ...]
        for v in visits:
            visit_groups.setdefault(v["name"], []).append(v)

        # 4. Match working list against visits using surname + initials
        met_doctors = []
        not_met_doctors = []
        matched_visit_names = set()

        for list_doctor in working_list:
            best_match = None
            # Tier 1: exact match (case-insensitive)
            for vname in visit_groups:
                if vname.strip().lower() == list_doctor.strip().lower():
                    best_match = vname
                    break
            # Tier 2: surname + initials match
            if not best_match:
                for vname in visit_groups:
                    if vname not in matched_visit_names and _doctor_names_match(list_doctor, vname):
                        best_match = vname
                        break

            if best_match:
                vg = visit_groups[best_match]
                met_doctors.append({
                    "name": list_doctor,
                    "visit_count": len(vg),
                    "last_visit": vg[-1]["date"],
                    "images": [v["image"] for v in vg],
                    "locations": list(set(
                        (v["lat"], v["lng"]) for v in vg if v["lat"] is not None
                    )),
                })
                matched_visit_names.add(best_match)
            else:
                not_met_doctors.append(list_doctor)

        # 5. Find unmatched visits (visited but not on the working list)
        unmatched_visits = []
        for vname, vg in visit_groups.items():
            if vname not in matched_visit_names:
                unmatched_visits.append({
                    "name": vname,
                    "visit_count": len(vg),
                    "last_visit": vg[-1]["date"],
                    "image": vg[0]["image"],
                    "note": "Not on working list",
                })

        return {
            "profile": profile,
            "month_year": month_year,
            "total_in_list": len(working_list),
            "total_met": len(met_doctors),
            "total_not_met": len(not_met_doctors),
            "met_doctors": met_doctors,
            "not_met_doctors": not_met_doctors,
            "unmatched_visits": unmatched_visits,
        }
    except Exception as e:
        return {
            "profile": profile, "month_year": month_year,
            "error": str(e),
            "total_in_list": 0, "total_met": 0, "total_not_met": 0,
            "met_doctors": [], "not_met_doctors": [], "unmatched_visits": [],
        }


# =================================================================
# ENTRY POINT
# =================================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8002, reload=True)
