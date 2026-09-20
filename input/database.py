import os
from math import radians, cos, sin, asin, sqrt

# pyrefly: ignore [missing-import]
import chromadb
from sqlalchemy import create_engine, MetaData, text
# pyrefly: ignore [missing-import]
from dotenv import load_dotenv

load_dotenv()

DB_DIR = os.path.join(os.path.dirname(__file__), "chroma_data")

# Build MySQL URL from env vars (no more hardcoded password)
_mysql_user = os.getenv("MYSQL_USER", "root")
_mysql_pass = os.getenv("MYSQL_PASSWORD", "")
_mysql_host = os.getenv("MYSQL_HOST", "localhost")
_mysql_port = os.getenv("MYSQL_PORT", "3306")
_mysql_db   = os.getenv("MYSQL_DB", "excel_imports")
MYSQL_URL = f"mysql+pymysql://{_mysql_user}:{_mysql_pass}@{_mysql_host}:{_mysql_port}/{_mysql_db}"

_chroma_client = None

def get_chroma_client():
    global _chroma_client
    if _chroma_client is None:
        if not os.path.exists(DB_DIR):
            os.makedirs(DB_DIR)
        _chroma_client = chromadb.PersistentClient(path=DB_DIR)
    return _chroma_client

def get_doc_store():
    return get_chroma_client().get_or_create_collection(name="Doc_store")

def get_image_store():
    return get_chroma_client().get_or_create_collection(name="Image_store")

def get_excel_store():
    return get_chroma_client().get_or_create_collection(name="Excel_store")

def get_mysql_engine():
    try:
        server_url = f"mysql+pymysql://{_mysql_user}:{_mysql_pass}@{_mysql_host}:{_mysql_port}/"
        server_engine = create_engine(server_url)
        with server_engine.begin() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {_mysql_db}"))
        return create_engine(MYSQL_URL)
    except Exception as e:
        print(f"DB Connection Error: {e}")
        raise e


# =====================================================================
# Crosscheck tables — auto-created on first use
# =====================================================================

_crosscheck_tables_initialized = False

def init_crosscheck_tables():
    """Create rep_visits, rep_doctor_lists, and entity_tokens tables if they don't exist."""
    global _crosscheck_tables_initialized
    if _crosscheck_tables_initialized:
        return
    try:
        engine = get_mysql_engine()
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS rep_visits (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    profile VARCHAR(100) NOT NULL,
                    doctor_name VARCHAR(255) NOT NULL,
                    latitude DOUBLE,
                    longitude DOUBLE,
                    visit_date DATE,
                    visit_time VARCHAR(20),
                    month_year VARCHAR(7) NOT NULL,
                    image_filename VARCHAR(500) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_profile_month (profile, month_year),
                    INDEX idx_doctor (profile, doctor_name)
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS rep_doctor_lists (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    profile VARCHAR(100) NOT NULL,
                    doctor_name VARCHAR(255) NOT NULL,
                    list_version TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_profile (profile)
                )
            """))
            # Entity token registry — inverted index mapping tokens → tables
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS entity_tokens (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    token VARCHAR(255) NOT NULL,
                    table_name VARCHAR(150) NOT NULL,
                    column_name VARCHAR(100) NOT NULL,
                    raw_value VARCHAR(500),
                    INDEX idx_token (token),
                    INDEX idx_table (table_name),
                    UNIQUE KEY uq_token_table_col_raw (token, table_name, column_name, raw_value(100))
                )
            """))
        _crosscheck_tables_initialized = True
        print("[DB] Crosscheck tables initialized.")
        print("[DB] Entity token registry initialized.")
    except Exception as e:
        print(f"[DB] Crosscheck table init error: {e}")


# =====================================================================
# Haversine distance utility
# =====================================================================

PROXIMITY_THRESHOLD_METERS = 50  # meters — same clinic gate selfie tolerance

def haversine_meters(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS points using Haversine formula."""
    R = 6_371_000  # Earth radius in meters
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * R * asin(sqrt(a))
