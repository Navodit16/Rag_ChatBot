import os
import uuid
import shutil
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

load_dotenv()

# pyrefly: ignore [missing-import]
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.staticfiles import StaticFiles
# pyrefly: ignore [missing-import]
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest
# pyrefly: ignore [missing-import]
from pydantic import BaseModel
from typing import List, Dict, Any
# pyrefly: ignore [missing-import]
import pandas as pd
import io
# pyrefly: ignore [missing-import]
from sqlalchemy import Table, Column, String, Integer, Float, Boolean, Text, MetaData

from database import get_doc_store, get_image_store, get_excel_store, get_mysql_engine, init_crosscheck_tables
from document_processor import chunk_text, process_docx, process_txt
from llm_client import extract_from_image_hybrid, extract_for_crosscheck, parse_doctor_names
from auth import verify_token
from datetime import datetime
# pyrefly: ignore [missing-import]
from sqlalchemy import text as sa_text

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

# --- App init (docs disabled for production) ---
app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# --- CORS — locked down to allowed origins ---
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:8001").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
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

# --- Static file mounts ---
PROFILES_DIR = os.path.join(os.path.dirname(__file__), "profiles")
os.makedirs(PROFILES_DIR, exist_ok=True)
app.mount("/static_profiles", StaticFiles(directory=PROFILES_DIR), name="static_profiles")

# --- Initialize crosscheck tables on startup ---
try:
    init_crosscheck_tables()
except Exception as e:
    print(f"[WARN] Could not init crosscheck tables at startup: {e}")

# ===== 1. Text/Doc Input =====
@app.post("/upload_doc")
@limiter.limit("10/minute")
async def upload_doc(request: Request, file: UploadFile = File(...), _=Depends(verify_token)):
    try:
        content = await file.read()
        is_docx = file.filename.endswith(".docx")
        
        if is_docx:
            text_data = process_docx(content)
        else:
            text_data = process_txt(content)
            
        chunks = chunk_text(text_data)
        if not chunks:
            raise HTTPException(status_code=400, detail="Document could not be chunked.")
            
        collection = get_doc_store()
        ids = [f"{file.filename}_{uuid.uuid4()}" for _ in chunks]
        metadatas = [{"source": file.filename, "type": "document"} for _ in chunks]
        
        collection.add(documents=chunks, metadatas=metadatas, ids=ids)
        return {"status": "success", "chunks_processed": len(chunks)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ===== 2. Image Input =====
@app.get("/profiles")
async def list_profiles(_=Depends(verify_token)):
    try:
        profiles = [d for d in os.listdir(PROFILES_DIR) if os.path.isdir(os.path.join(PROFILES_DIR, d))]
        return {"profiles": profiles}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/profiles")
async def create_profile(name: str = Form(...), _=Depends(verify_token)):
    profile_path = os.path.join(PROFILES_DIR, name)
    if os.path.exists(profile_path):
        raise HTTPException(status_code=400, detail="Profile already exists.")
    os.makedirs(profile_path)
    return {"status": "success", "name": name}

@app.delete("/profiles/{name}")
async def delete_profile(name: str, _=Depends(verify_token)):
    profile_path = os.path.join(PROFILES_DIR, name)
    if os.path.exists(profile_path):
        shutil.rmtree(profile_path)
    
    # Also delete corresponding entries in Chroma DB Image_store
    collection = get_image_store()
    results = collection.get(where={"person": name})
    if results and results.get("ids") and len(results["ids"]) > 0:
        collection.delete(ids=results["ids"])

    # Cascade delete crosscheck data from MySQL
    try:
        engine = get_mysql_engine()
        with engine.begin() as conn:
            conn.execute(sa_text("DELETE FROM rep_visits WHERE profile = :p"), {"p": name})
            conn.execute(sa_text("DELETE FROM rep_doctor_lists WHERE profile = :p"), {"p": name})

        # Drop the dynamic {profile}_working_list table
        wl_table_name = f"{name.lower().replace(' ', '_')}_working_list"
        meta_obj = MetaData()
        meta_obj.reflect(bind=engine)
        if wl_table_name in meta_obj.tables:
            meta_obj.tables[wl_table_name].drop(engine)

        # Clean up entity_tokens for this working list
        with engine.begin() as conn:
            try:
                conn.execute(sa_text(
                    "DELETE FROM entity_tokens WHERE table_name = :tbl"
                ), {"tbl": wl_table_name})
            except Exception:
                pass
    except Exception as e:
        print(f"[WARN] Could not clean up crosscheck tables for profile {name}: {e}")

    # Clean up Chroma Excel_store entry for the doctor list
    try:
        excel_store = get_excel_store()
        wl_table_name = f"{name.lower().replace(' ', '_')}_working_list"
        ex_results = excel_store.get(where={"source": wl_table_name})
        if ex_results and ex_results.get("ids"):
            excel_store.delete(ids=ex_results["ids"])
    except Exception as e:
        print(f"[WARN] Could not clean up Excel_store for profile {name}: {e}")

    return {"status": "success"}

@app.put("/profiles/{old_name}")
async def rename_profile(old_name: str, new_name: str = Form(...), _=Depends(verify_token)):
    old_path = os.path.join(PROFILES_DIR, old_name)
    new_path = os.path.join(PROFILES_DIR, new_name)
    if not os.path.exists(old_path):
        raise HTTPException(status_code=404, detail="Profile not found.")
    if os.path.exists(new_path):
        raise HTTPException(status_code=400, detail="Target name already exists.")
    os.rename(old_path, new_path)

    # Update Chroma Image_store: re-add entries with new person name
    try:
        collection = get_image_store()
        results = collection.get(where={"person": old_name})
        if results and results.get("ids") and len(results["ids"]) > 0:
            for i, doc_id in enumerate(results["ids"]):
                meta = results["metadatas"][i].copy()
                meta["person"] = new_name
                doc = results["documents"][i]
                collection.delete(ids=[doc_id])
                collection.add(documents=[doc], metadatas=[meta], ids=[doc_id])
    except Exception as e:
        print(f"[WARN] Could not update Image_store for rename {old_name} → {new_name}: {e}")

    # Update MySQL crosscheck tables
    try:
        engine = get_mysql_engine()
        with engine.begin() as conn:
            conn.execute(sa_text(
                "UPDATE rep_visits SET profile = :new WHERE profile = :old"
            ), {"new": new_name, "old": old_name})
            conn.execute(sa_text(
                "UPDATE rep_doctor_lists SET profile = :new WHERE profile = :old"
            ), {"new": new_name, "old": old_name})

        # Rename working list table: drop old, create new
        old_tbl = f"{old_name.lower().replace(' ', '_')}_working_list"
        new_tbl = f"{new_name.lower().replace(' ', '_')}_working_list"
        meta_obj = MetaData()
        meta_obj.reflect(bind=engine)
        if old_tbl in meta_obj.tables:
            with engine.begin() as conn:
                conn.execute(sa_text(f"RENAME TABLE `{old_tbl}` TO `{new_tbl}`"))

        # Update Chroma Excel_store entry source
        excel_store = get_excel_store()
        ex_results = excel_store.get(where={"source": old_tbl})
        if ex_results and ex_results.get("ids"):
            for i, eid in enumerate(ex_results["ids"]):
                meta = ex_results["metadatas"][i].copy()
                meta["source"] = new_tbl
                meta["profile"] = new_name
                doc = ex_results["documents"][i]
                excel_store.delete(ids=[eid])
                excel_store.add(documents=[doc], metadatas=[meta], ids=[eid])

        # Update entity_tokens table references
        try:
            with engine.begin() as conn:
                conn.execute(sa_text(
                    "UPDATE entity_tokens SET table_name = :new WHERE table_name = :old"
                ), {"new": new_tbl, "old": old_tbl})
        except Exception:
            pass
    except Exception as e:
        print(f"[WARN] Could not update MySQL/Excel_store for rename {old_name} → {new_name}: {e}")

    return {"status": "success"}

@app.post("/upload_image")
@limiter.limit("60/minute")
async def upload_image(request: Request, profile: str = Form(...), file: UploadFile = File(...), _=Depends(verify_token)):
    profile_path = os.path.join(PROFILES_DIR, profile)
    if not os.path.exists(profile_path):
        raise HTTPException(status_code=404, detail="Profile not found.")

    save_path = None
    doc_id = None
    try:
        content = await file.read()

        # ── Crosscheck-aware extraction (no LLM call) ──
        result = extract_for_crosscheck(content, profile, file.filename)

        if result.get("error"):
            raise HTTPException(status_code=400, detail=result["error"])

        month_year = result.get("month_year")
        doctors = result.get("doctors", [])
        lat = result.get("lat")
        lng = result.get("lng")
        date_str = result.get("date", "Not found")
        time_str = result.get("time", "Not found")
        chroma_doc = result.get("chroma_doc", "")

        # Determine save directory (month-based subfolder)
        if month_year:
            save_dir = os.path.join(profile_path, month_year)
            os.makedirs(save_dir, exist_ok=True)
        else:
            save_dir = profile_path  # fallback to flat if no date extracted

        save_path = os.path.join(save_dir, file.filename)

        # ── Save to Chroma Image_store ──
        collection = get_image_store()
        doc_id = f"img_{profile}_{uuid.uuid4()}"

        collection.add(
            documents=[chroma_doc],
            metadatas=[{
                "source": file.filename,
                "type": "image",
                "person": profile,
                "month_year": month_year or "",
            }],
            ids=[doc_id]
        )

        # ── Insert visit records into MySQL ──
        if doctors and month_year:
            try:
                engine = get_mysql_engine()
                # Parse date for MySQL DATE column
                visit_date = None
                if date_str and date_str != "Not found":
                    try:
                        visit_date = datetime.strptime(date_str, "%d/%m/%Y").date()
                    except ValueError:
                        pass

                with engine.begin() as conn:
                    for doc_name in doctors:
                        conn.execute(sa_text("""
                            INSERT INTO rep_visits
                            (profile, doctor_name, latitude, longitude, visit_date, visit_time, month_year, image_filename)
                            VALUES (:profile, :doctor, :lat, :lng, :vdate, :vtime, :my, :img)
                        """), {
                            "profile": profile,
                            "doctor": doc_name,
                            "lat": lat,
                            "lng": lng,
                            "vdate": visit_date,
                            "vtime": time_str if time_str != "Not found" else None,
                            "my": month_year,
                            "img": file.filename,
                        })
            except Exception as db_err:
                print(f"[WARN] rep_visits insert failed: {db_err}")
                # Don't fail the whole upload for this

        # Save image locally only after successful processing + DB write
        with open(save_path, "wb") as f:
            f.write(content)

        return {
            "status": "success",
            "doctors": doctors,
            "month_year": month_year,
            "extracted": chroma_doc,
        }
    except HTTPException:
        raise
    except Exception as e:
        # Clean up file if it was written before the failure
        if save_path and os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception:
                pass
        # Rollback Chroma entry if it was added
        try:
            if doc_id:
                get_image_store().delete(ids=[doc_id])
        except Exception:
            pass
        # Rollback MySQL visit rows if they were inserted
        try:
            if doctors and month_year:
                engine = get_mysql_engine()
                with engine.begin() as conn:
                    conn.execute(sa_text(
                        "DELETE FROM rep_visits WHERE profile = :p AND image_filename = :f"
                    ), {"p": profile, "f": file.filename})
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/profiles/{profile}/images")
async def list_profile_images(profile: str, _=Depends(verify_token)):
    profile_path = os.path.join(PROFILES_DIR, profile)
    if not os.path.exists(profile_path):
        return {"images": []}
    # Collect images from both flat directory and month subfolders
    images = []
    for root, dirs, files in os.walk(profile_path):
        for f in files:
            if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
                # Get relative path from profile dir
                rel_path = os.path.relpath(os.path.join(root, f), profile_path)
                images.append(rel_path.replace("\\", "/"))
    return {"images": images}

@app.get("/profiles/{profile}/months")
async def list_profile_months(profile: str, _=Depends(verify_token)):
    """List available MM_YYYY month folders for a profile."""
    profile_path = os.path.join(PROFILES_DIR, profile)
    if not os.path.exists(profile_path):
        return {"months": []}
    months = [
        d for d in os.listdir(profile_path)
        if os.path.isdir(os.path.join(profile_path, d)) and len(d) == 7 and '_' in d
    ]
    months.sort()
    return {"months": months}

# ===== 2b. Doctor Working List =====
@app.post("/upload_doctor_list")
@limiter.limit("10/minute")
async def upload_doctor_list(
    request: Request,
    profile: str = Form(...),
    file: UploadFile = File(...),
    _=Depends(verify_token)
):
    """Upload a doctor working list Excel/CSV for a profile.
    Replaces any existing list for this profile."""
    if not file.filename.endswith(('.xlsx', '.xls', '.csv')):
        raise HTTPException(status_code=400, detail="Invalid file type. Use .xlsx, .xls, or .csv")

    try:
        content = await file.read()

        # Parse the file — expect doctor names in the first column
        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(content), header=None).fillna("")
        else:
            df = pd.read_excel(io.BytesIO(content), header=None).fillna("")

        # Extract doctor names from first column (skip header row if it looks like a header)
        all_values = df.iloc[:, 0].astype(str).tolist()
        # Filter: keep rows that look like doctor names (start with Dr) or any non-empty value
        doctor_names = [v.strip() for v in all_values if v.strip()]

        # Remove header-like values (e.g., "Doctor Name", "Name", "Sr No")
        header_patterns = ['name', 'doctor', 'sr', 'no', 'serial', 'sno', 'index']
        if doctor_names and any(h in doctor_names[0].lower() for h in header_patterns):
            doctor_names = doctor_names[1:]

        if not doctor_names:
            raise HTTPException(status_code=400, detail="No doctor names found in the first column.")

        # Replace existing list in MySQL
        engine = get_mysql_engine()
        with engine.begin() as conn:
            conn.execute(sa_text("DELETE FROM rep_doctor_lists WHERE profile = :p"), {"p": profile})
            for name in doctor_names:
                conn.execute(sa_text("""
                    INSERT INTO rep_doctor_lists (profile, doctor_name)
                    VALUES (:p, :n)
                """), {"p": profile, "n": name})

        # Also create/update a MySQL table for RAG queries: "{profile}_working_list"
        table_name = f"{profile.lower().replace(' ', '_')}_working_list"
        try:
            metadata_obj = MetaData()
            metadata_obj.reflect(bind=engine)
            if table_name in metadata_obj.tables:
                existing = metadata_obj.tables[table_name]
                existing.drop(engine)
                metadata_obj.remove(existing)

            new_table = Table(
                table_name, metadata_obj,
                Column("Doctor_Name", String(255)),
            )
            new_table.create(engine)
            with engine.begin() as conn:
                for name in doctor_names:
                    conn.execute(new_table.insert(), {"Doctor_Name": name})

                # Add Doctor_Name_token column
                from tokenizer import generate_token
                try:
                    conn.execute(sa_text(
                        f"ALTER TABLE `{table_name}` ADD COLUMN `Doctor_Name_token` VARCHAR(500)"
                    ))
                    conn.execute(sa_text(
                        f"CREATE INDEX `idx_{table_name[:40]}_dnt` ON `{table_name}` (`Doctor_Name_token`)"
                    ))
                    # Clean old registry entries
                    try:
                        conn.execute(sa_text(
                            "DELETE FROM entity_tokens WHERE table_name = :tbl"
                        ), {"tbl": table_name})
                    except Exception:
                        pass
                    for name in doctor_names:
                        token = generate_token(name)
                        if token:
                            conn.execute(sa_text(
                                f"UPDATE `{table_name}` SET `Doctor_Name_token` = :token "
                                f"WHERE `Doctor_Name` = :raw"
                            ), {"token": token, "raw": name})
                            try:
                                conn.execute(sa_text(
                                    "INSERT IGNORE INTO entity_tokens "
                                    "(token, table_name, column_name, raw_value) "
                                    "VALUES (:token, :tbl, :col, :raw)"
                                ), {"token": token, "tbl": table_name,
                                    "col": "Doctor_Name", "raw": name[:500]})
                            except Exception:
                                pass
                except Exception as tok_err:
                    print(f"[TOKEN] Doctor list tokenize warning: {tok_err}")

            # Embed to Chroma Excel_store for RAG (with token-enriched slab)
            from tokenizer import generate_token as _gt
            token_entries = [_gt(n) for n in doctor_names[:50]]
            token_entries = [t for t in token_entries if t]
            text_slab = (
                f"{{Table: {table_name}, Schema: Doctor_Name, "
                f"Entries of first column: {', '.join(doctor_names[:50])}, "
                f"Entries (tokenized): {', '.join(token_entries)}}}"
            )
            collection = get_excel_store()
            # Remove old entry for this table if exists
            existing_results = collection.get(where={"source": table_name})
            if existing_results and existing_results.get("ids"):
                collection.delete(ids=existing_results["ids"])
            doc_id = f"excel_{table_name}_{uuid.uuid4()}"
            collection.add(
                documents=[text_slab],
                metadatas=[{"source": table_name, "type": "excel_table", "is_doctor_list": "true", "profile": profile}],
                ids=[doc_id]
            )
        except Exception as tbl_err:
            print(f"[WARN] Working list table creation failed: {tbl_err}")

        return {
            "status": "success",
            "profile": profile,
            "doctors_loaded": len(doctor_names),
            "table_name": table_name,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/doctor_list/{profile}")
async def get_doctor_list(profile: str, _=Depends(verify_token)):
    """Get the current working list for a profile."""
    try:
        engine = get_mysql_engine()
        with engine.connect() as conn:
            result = conn.execute(
                sa_text("SELECT doctor_name, list_version FROM rep_doctor_lists WHERE profile = :p ORDER BY doctor_name"),
                {"p": profile}
            )
            rows = result.fetchall()
            if not rows:
                return {"profile": profile, "doctors": [], "count": 0, "last_updated": None}
            doctors = [r[0] for r in rows]
            last_updated = str(rows[0][1]) if rows else None
            return {
                "profile": profile,
                "doctors": doctors,
                "count": len(doctors),
                "last_updated": last_updated,
            }
    except Exception as e:
        return {"profile": profile, "doctors": [], "count": 0, "error": str(e)}

class DoctorListPayload(BaseModel):
    profile: str
    doctors: List[str]

@app.post("/save_doctor_list")
@limiter.limit("10/minute")
async def save_doctor_list(request: Request, payload: DoctorListPayload, _=Depends(verify_token)):
    """Save a doctor working list from the table editor.
    Receives the profile name and a list of doctor names extracted from the selected column.
    Auto-sets table name to {profile}_working_list and header to Doctor_Name."""
    profile = payload.profile.strip()
    doctor_names = [d.strip() for d in payload.doctors if d.strip()]

    # Auto-prepend "Dr" if not already present
    doctor_names = [
        name if name.upper().startswith("DR") else f"Dr {name}"
        for name in doctor_names
    ]

    if not profile:
        raise HTTPException(status_code=400, detail="Profile name is required.")
    if not doctor_names:
        raise HTTPException(status_code=400, detail="No doctor names provided.")

    try:
        engine = get_mysql_engine()
        table_name = f"{profile.lower().replace(' ', '_')}_working_list"

        # 1. Replace rep_doctor_lists rows
        with engine.begin() as conn:
            conn.execute(sa_text("DELETE FROM rep_doctor_lists WHERE profile = :p"), {"p": profile})
            for name in doctor_names:
                conn.execute(sa_text("""
                    INSERT INTO rep_doctor_lists (profile, doctor_name)
                    VALUES (:p, :n)
                """), {"p": profile, "n": name})

        # 2. Create/replace the dynamic MySQL table
        metadata_obj = MetaData()
        metadata_obj.reflect(bind=engine)
        if table_name in metadata_obj.tables:
            existing = metadata_obj.tables[table_name]
            existing.drop(engine)
            metadata_obj.remove(existing)

        new_table = Table(
            table_name, metadata_obj,
            Column("Doctor_Name", String(255)),
        )
        new_table.create(engine)
        with engine.begin() as conn:
            for name in doctor_names:
                conn.execute(new_table.insert(), {"Doctor_Name": name})

            # Add Doctor_Name_token column + registry
            from tokenizer import generate_token
            try:
                conn.execute(sa_text(
                    f"ALTER TABLE `{table_name}` ADD COLUMN `Doctor_Name_token` VARCHAR(500)"
                ))
                conn.execute(sa_text(
                    f"CREATE INDEX `idx_{table_name[:40]}_dnt` ON `{table_name}` (`Doctor_Name_token`)"
                ))
                try:
                    conn.execute(sa_text(
                        "DELETE FROM entity_tokens WHERE table_name = :tbl"
                    ), {"tbl": table_name})
                except Exception:
                    pass
                for name in doctor_names:
                    token = generate_token(name)
                    if token:
                        conn.execute(sa_text(
                            f"UPDATE `{table_name}` SET `Doctor_Name_token` = :token "
                            f"WHERE `Doctor_Name` = :raw"
                        ), {"token": token, "raw": name})
                        try:
                            conn.execute(sa_text(
                                "INSERT IGNORE INTO entity_tokens "
                                "(token, table_name, column_name, raw_value) "
                                "VALUES (:token, :tbl, :col, :raw)"
                            ), {"token": token, "tbl": table_name,
                                "col": "Doctor_Name", "raw": name[:500]})
                        except Exception:
                            pass
            except Exception as tok_err:
                print(f"[TOKEN] Doctor list re-upload tokenize warning: {tok_err}")

        # 3. Update Chroma Excel_store (with token-enriched slab)
        from tokenizer import generate_token as _gt
        token_entries = [_gt(n) for n in doctor_names[:50]]
        token_entries = [t for t in token_entries if t]
        text_slab = (
            f"{{Table: {table_name}, Schema: Doctor_Name, "
            f"Entries of first column: {', '.join(doctor_names[:50])}, "
            f"Entries (tokenized): {', '.join(token_entries)}}}"
        )
        collection = get_excel_store()
        existing_results = collection.get(where={"source": table_name})
        if existing_results and existing_results.get("ids"):
            collection.delete(ids=existing_results["ids"])
        doc_id = f"excel_{table_name}_{uuid.uuid4()}"
        collection.add(
            documents=[text_slab],
            metadatas=[{"source": table_name, "type": "excel_table", "is_doctor_list": "true", "profile": profile}],
            ids=[doc_id]
        )

        return {
            "status": "success",
            "profile": profile,
            "doctors_loaded": len(doctor_names),
            "table_name": table_name,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
# ===== 3. Excel Input =====
def infer_alchemy_type(value):
    if value is None:
        return String(255)
    if isinstance(value, bool):
        return Boolean
    if isinstance(value, int):
        return Integer
    if isinstance(value, float):
        return Float
    if isinstance(value, str):
        if len(value) > 255:
            return Text
        return String(255)
    return String(255)

class TableData(BaseModel):
    table_name: str
    data: List[Dict[str, Any]]
    headers: List[str] = []
    raw_data: List[List[Any]] = []

class SavePayload(BaseModel):
    tables: List[TableData]

@app.post("/upload_excel")
@limiter.limit("10/minute")
async def upload_excel(request: Request, file: UploadFile = File(...), _=Depends(verify_token)):
    if not file.filename.endswith(('.xlsx', '.xls', '.csv')):
        raise HTTPException(status_code=400, detail="Invalid file type.")
    try:
        content = await file.read()
        sheets_data = {}

        if file.filename.endswith('.csv'):
            df = pd.read_csv(io.BytesIO(content), header=None).fillna("")
            sheets_data["Sheet1"] = df.values.tolist()
        else:
            xls = pd.ExcelFile(io.BytesIO(content))
            for sheet_name in xls.sheet_names:
                df = pd.read_excel(xls, sheet_name=sheet_name, header=None).fillna("")
                sheets_data[sheet_name] = df.values.tolist()
                
        return {"filename": file.filename, "sheets": sheets_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")

@app.post("/save_tables")
@limiter.limit("10/minute")
async def save_tables(request: Request, payload: SavePayload, _=Depends(verify_token)):
    from tokenizer import generate_token

    engine = get_mysql_engine()
    metadata = MetaData()
    collection = get_excel_store()

    saved_count = 0
    try:
        with engine.begin() as conn:
            for table_spec in payload.tables:
                table_name = table_spec.table_name.lower()  # normalize case to match MySQL
                rows = table_spec.data
                
                if not rows:
                    continue
                    
                first_row = rows[0]
                
                metadata.reflect(bind=engine)
                if table_name in metadata.tables:
                    existing_table = metadata.tables[table_name]
                    existing_table.drop(engine)
                    metadata.remove(existing_table)
                
                # Also clean up old entity_tokens for this table
                try:
                    conn.execute(sa_text(
                        "DELETE FROM entity_tokens WHERE table_name = :tbl"
                    ), {"tbl": table_name})
                except Exception:
                    pass  # table may not exist yet on first run
                
                ordered_headers = table_spec.headers if table_spec.headers else list(first_row.keys())
                
                columns = []
                for col_name in ordered_headers:
                    val = first_row.get(col_name)
                    col_type = infer_alchemy_type(val)
                    columns.append(Column(col_name, col_type))
                
                new_table = Table(table_name, metadata, *columns)
                new_table.create(engine)
                conn.execute(new_table.insert(), rows)
                
                # =============================================================
                # TOKEN SYSTEM: Add _token columns for all varchar columns
                # =============================================================
                varchar_cols = [
                    col_name for col_name in ordered_headers
                    if isinstance(first_row.get(col_name), str)
                ]
                
                for col_name in varchar_cols:
                    token_col = f"{col_name}_token"
                    try:
                        conn.execute(sa_text(
                            f"ALTER TABLE `{table_name}` "
                            f"ADD COLUMN `{token_col}` VARCHAR(500)"
                        ))
                        conn.execute(sa_text(
                            f"CREATE INDEX `idx_{table_name[:30]}_{token_col[:30]}` "
                            f"ON `{table_name}` (`{token_col}`)"
                        ))
                    except Exception as e:
                        print(f"[TOKEN] Could not add column {token_col}: {e}")
                        continue
                    
                    # Fetch all values, generate tokens, update + register
                    all_vals = conn.execute(sa_text(
                        f"SELECT `{col_name}` FROM `{table_name}`"
                    )).fetchall()
                    
                    for (raw_val,) in all_vals:
                        raw_str = str(raw_val) if raw_val else ""
                        token = generate_token(raw_str)
                        if not token:
                            continue
                        
                        # Update the _token column
                        conn.execute(sa_text(
                            f"UPDATE `{table_name}` "
                            f"SET `{token_col}` = :token "
                            f"WHERE `{col_name}` = :raw"
                        ), {"token": token, "raw": raw_val})
                        
                        # Register in entity_tokens registry (ignore duplicates)
                        try:
                            conn.execute(sa_text(
                                "INSERT IGNORE INTO entity_tokens "
                                "(token, table_name, column_name, raw_value) "
                                "VALUES (:token, :tbl, :col, :raw)"
                            ), {
                                "token": token,
                                "tbl": table_name,
                                "col": col_name,
                                "raw": raw_str[:500],
                            })
                        except Exception:
                            pass  # duplicate, ignore
                    
                    print(f"[TOKEN] {table_name}.{token_col}: "
                          f"tokenized {len(all_vals)} values")
                
                # =============================================================
                # CHROMA: Build token-enriched text slab
                # =============================================================
                if table_spec.raw_data and len(table_spec.raw_data) >= 2:
                    raw_schema = ", ".join(str(v) for v in table_spec.raw_data[0])
                    # Build BOTH raw and tokenized first-column entries
                    raw_entries = [
                        str(table_spec.raw_data[r][0])
                        for r in range(1, len(table_spec.raw_data))
                    ]
                    token_entries = [generate_token(e) for e in raw_entries]
                    token_entries = [t for t in token_entries if t]  # remove empties
                    
                    text_slab = (
                        f"{{Table: {table_name}, "
                        f"Schema: {raw_schema}, "
                        f"Entries of first column: {', '.join(raw_entries)}, "
                        f"Entries (tokenized): {', '.join(token_entries)}}}"
                    )
                else:
                    # Fallback: use ordered_headers and dict values
                    schema_str = ", ".join(str(k) for k in ordered_headers)
                    first_col_key = ordered_headers[0] if ordered_headers else list(first_row.keys())[0]
                    raw_entries = [str(r.get(first_col_key, "")) for r in rows]
                    token_entries = [generate_token(e) for e in raw_entries]
                    token_entries = [t for t in token_entries if t]
                    
                    text_slab = (
                        f"{{Table: {table_name}, "
                        f"Schema: {schema_str}, "
                        f"Entries of first column: {', '.join(raw_entries)}, "
                        f"Entries (tokenized): {', '.join(token_entries)}}}"
                    )
                
                doc_id = f"excel_{table_name}_{uuid.uuid4()}"
                collection.add(
                    documents=[text_slab],
                    metadatas=[{"source": table_name, "type": "excel_table"}],
                    ids=[doc_id]
                )
                saved_count += 1
                
        return {"status": "success", "tables_saved": saved_count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database save error: {str(e)}")

# ===== 4. Deletion Endpoints =====
@app.get("/delete_view/docs")
def view_docs(_=Depends(verify_token)):
    collection = get_doc_store()
    results = collection.get()
    docs = [{"id": ids, "source": meta.get("source", "unknown"), "chunk": doc[:100] + "..."} for ids, doc, meta in zip(results.get("ids", []), results.get("documents", []), results.get("metadatas", []))]
    return {"items": docs}

@app.delete("/delete_item/docs/{item_id}")
def delete_doc(item_id: str, _=Depends(verify_token)):
    collection = get_doc_store()
    collection.delete(ids=[item_id])
    return {"status": "success"}

@app.get("/delete_view/images")
def view_images(profile: str = None, _=Depends(verify_token)):
    collection = get_image_store()
    if profile:
        results = collection.get(where={"person": profile})
    else:
        results = collection.get()
    docs = [{"id": ids, "person": meta.get("person", "unknown"), "source": meta.get("source", "unknown"), "month_year": meta.get("month_year", ""), "data": doc} for ids, doc, meta in zip(results.get("ids", []), results.get("documents", []), results.get("metadatas", []))]
    return {"items": docs}

@app.delete("/delete_item/images/{item_id}")
def delete_image(item_id: str, _=Depends(verify_token)):
    collection = get_image_store()
    # To delete physical file we could: source = meta['source'], profile = meta['person'], path = os.path.join(PROFILES_DIR, profile, source) 
    # and os.remove(path). I'll fetch the item first.
    person = None
    source = None
    results = collection.get(ids=[item_id])
    if results and results.get("metadatas") and len(results["metadatas"]) > 0:
        meta = results["metadatas"][0]
        person = meta.get("person")
        source = meta.get("source")
        if person and source:
            # Check flat directory first, then month subfolders
            img_path = os.path.join(PROFILES_DIR, person, source)
            if os.path.exists(img_path):
                try:
                    os.remove(img_path)
                except Exception as e:
                    pass
            else:
                # Search month subfolders (profiles/{person}/MM_YYYY/{source})
                profile_dir = os.path.join(PROFILES_DIR, person)
                if os.path.isdir(profile_dir):
                    for subfolder in os.listdir(profile_dir):
                        sub_path = os.path.join(profile_dir, subfolder, source)
                        if os.path.isfile(sub_path):
                            try:
                                os.remove(sub_path)
                            except Exception:
                                pass
                            break
    collection.delete(ids=[item_id])
    
    # Also delete corresponding rep_visits rows for this image
    if person and source:
        try:
            engine = get_mysql_engine()
            with engine.begin() as conn:
                conn.execute(sa_text(
                    "DELETE FROM rep_visits WHERE profile = :p AND image_filename = :f"
                ), {"p": person, "f": source})
        except Exception as e:
            print(f"[WARN] Could not clean up rep_visits: {e}")
    
    return {"status": "success"}

@app.get("/delete_view/tables")
def view_tables(_=Depends(verify_token)):
    collection = get_excel_store()
    results = collection.get()
    docs = [{"id": ids, "source": meta.get("source", "unknown"), "data": doc} for ids, doc, meta in zip(results.get("ids", []), results.get("documents", []), results.get("metadatas", []))]
    return {"items": docs}

@app.delete("/delete_item/tables/{item_id}")
def delete_table(item_id: str, _=Depends(verify_token)):
    collection = get_excel_store()
    # Get the table name and metadata before deleting from Chroma
    results = collection.get(ids=[item_id])
    table_name = None
    is_doctor_list = False
    profile_name = None
    if results and results.get("metadatas") and len(results["metadatas"]) > 0:
        meta = results["metadatas"][0]
        table_name = meta.get("source")
        is_doctor_list = meta.get("is_doctor_list") == "true"
        profile_name = meta.get("profile")
    
    collection.delete(ids=[item_id])
    
    # Also drop the corresponding MySQL table
    if table_name:
        try:
            engine = get_mysql_engine()
            metadata = MetaData()
            metadata.reflect(bind=engine)
            if table_name in metadata.tables:
                metadata.tables[table_name].drop(engine)

            # Clean up entity_tokens for this table
            with engine.begin() as conn:
                try:
                    conn.execute(sa_text(
                        "DELETE FROM entity_tokens WHERE table_name = :tbl"
                    ), {"tbl": table_name})
                except Exception:
                    pass

            # If this was a doctor working list, also clean rep_doctor_lists
            if is_doctor_list and profile_name:
                with engine.begin() as conn:
                    conn.execute(sa_text(
                        "DELETE FROM rep_doctor_lists WHERE profile = :p"
                    ), {"p": profile_name})
        except Exception as e:
            print(f"Warning: Could not drop MySQL table '{table_name}': {e}")
    
    return {"status": "success"}

# --- Serve frontend (MUST be after all API routes) ---
# Mount the whole directory with html=True so index.html is served at /
BASE_DIR = os.path.dirname(__file__)
app.mount("/", StaticFiles(directory=BASE_DIR, html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=True)
