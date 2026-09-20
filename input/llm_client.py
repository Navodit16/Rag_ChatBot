import os
import requests
import base64
import re
import io
import time
from PIL import Image
from paddleocr import PaddleOCR
from dotenv import load_dotenv

load_dotenv()

OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:18k")
OLLAMA_TIMEOUT_SECONDS = 60

# --- Module-level PaddleOCR initialization (loaded once) ---
# det_limit_side_len=480 limits detection model input → major CPU speedup
ocr_en = PaddleOCR(lang='en', use_angle_cls=False, show_log=False, det_limit_side_len=480)


# ===== Old vision function — kept as fallback =====
def generate_from_image(prompt, image_bytes, model=OLLAMA_MODEL, timeout=OLLAMA_TIMEOUT_SECONDS, think=True):
    """
    Sends an image and prompt to the model via /api/chat.
    Set think=False to disable reasoning/thinking mode (faster, no infinite loops).
    """
    base64_image = base64.b64encode(image_bytes).decode('utf-8')
    
    chat_body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64_image]
            }
        ],
        "stream": False,
        "keep_alive": -1,
        "think": think
    }
    
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=chat_body, timeout=timeout)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        # Strip <think>...</think> tags from thinking models (including orphaned tags)
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        content = re.sub(r'</think>', '', content)  # orphaned closing tags
        content = content.strip()
        return content
    except Exception as e:
        print(f"Error calling Ollama Vision: {e}")
        return f"Error: Could not process image with Ollama. {e}"


# ===== Stage 2 helper: text-only qwen3.5 call =====
def generate_text_only(prompt, model=OLLAMA_MODEL):
    """
    Sends a text-only prompt (no image) to qwen3.5 via /api/chat.
    Used for Stage 2 background text analysis.
    """
    chat_body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "keep_alive": -1,
        "think": False,
        "options": {
            "num_predict": 50,
            "temperature": 0.1,
            "top_k": 20,
            "top_p": 0.8
        }
    }

    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", json=chat_body, timeout=OLLAMA_TIMEOUT_SECONDS)
        r.raise_for_status()
        content = r.json().get("message", {}).get("content", "")
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL).strip()
        return content
    except Exception as e:
        print(f"Error calling Ollama text-only: {e}")
        return "Not found"


# ===== Footer parsing helpers =====
def _group_ocr_into_rows(ocr_result):
    """
    Takes raw PaddleOCR result and groups text blocks into rows
    by Y-coordinate proximity. Returns list of row strings, top-to-bottom.
    """
    if not ocr_result or not ocr_result[0]:
        return []

    # Extract (y_center, text) from each detection
    items = []
    for line in ocr_result[0]:
        bbox = line[0]                    # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
        text = line[1][0]                 # detected text string
        y_center = (bbox[0][1] + bbox[2][1]) / 2
        x_left = bbox[0][0]
        items.append((y_center, x_left, text))

    # Sort by Y then X
    items.sort(key=lambda t: (t[0], t[1]))

    # Group into rows: items within 15px Y are the same row
    rows = []
    current_row = [items[0]]
    for item in items[1:]:
        if abs(item[0] - current_row[-1][0]) < 15:
            current_row.append(item)
        else:
            # Sort row items left-to-right, join text
            current_row.sort(key=lambda t: t[1])
            rows.append(" ".join(t[2] for t in current_row))
            current_row = [item]
    # Don't forget last row
    current_row.sort(key=lambda t: t[1])
    rows.append(" ".join(t[2] for t in current_row))

    return rows


def _parse_footer_rows(rows):
    """
    Parses GPS footer rows using pattern matching (not fixed indices).
    Handles variable number of rows, split addresses, extra headers, etc.

    Returns (place, date, time, lat, lng) tuple.
    """
    place = "Not found"
    date = "Not found"
    time_val = "Not found"
    lat = None
    lng = None

    all_text = " ".join(rows)

    # --- Extract Lat/Long BEFORE skipping those rows ---
    lat_match = re.search(r'Lat\s*([\d.]+)', all_text, re.IGNORECASE)
    lng_match = re.search(r'Long\s*([\d.]+)', all_text, re.IGNORECASE)
    if lat_match:
        try:
            lat = float(lat_match.group(1))
        except ValueError:
            lat = None
    if lng_match:
        try:
            lng = float(lng_match.group(1))
        except ValueError:
            lng = None

    # --- Classify each row ---
    SKIP_PATTERN = re.compile(r'Lat\s*\d|Long\s*\d|Note\s*:|Camera|GPS\s*Map|GMT|Maps$', re.IGNORECASE)
    DATE_PATTERN = re.compile(r'\d{1,2}/\d{1,2}/\d{4}')

    address_rows = []  # rows that look like address parts
    for row in rows:
        row_stripped = row.strip()
        if not row_stripped:
            continue
        # Skip non-address rows
        if SKIP_PATTERN.search(row_stripped):
            continue
        if DATE_PATTERN.search(row_stripped):
            continue
        # Remaining rows with commas or a pincode are address parts
        if ',' in row_stripped or re.search(r'\d{6}', row_stripped):
            address_rows.append(row_stripped)

    # --- Place: combine all address rows, preferring the longer/more specific ones ---
    if address_rows:
        # If there are multiple address rows, the first is usually City,State,Country
        # and the rest form the detailed street address. Use the detailed ones.
        # Strategy: if any row has a pincode, collect that row + all rows between
        # the city row and the pincode row to form the full address.
        pincode_idx = None
        for i, row in enumerate(address_rows):
            if re.search(r'\d{6}', row):
                pincode_idx = i
                break

        if pincode_idx is not None:
            # Combine from row after the first (city) row up to and including the pincode row
            # If pincode_idx == 0, it means pincode is in the first address row
            if pincode_idx == 0:
                place = address_rows[0]
            else:
                # Skip the first row if it's a short city-level summary
                # (e.g., "Varanasi,Uttar Pradesh,India")
                # and combine the rest which form the full street address
                detailed = address_rows[1:pincode_idx + 1]
                if detailed:
                    place = ", ".join(detailed)
                else:
                    place = address_rows[pincode_idx]
        else:
            # No pincode found — use the longest address row
            place = max(address_rows, key=len)

    # --- Date: search ALL rows for DD/MM/YYYY ---
    date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', all_text)
    if date_match:
        date = date_match.group(1)

    # --- Time: search ALL rows for HH:MM AM/PM (handle no-space like "01:24PM") ---
    time_match = re.search(r'(\d{1,2}:\d{2})\s*([AaPp][Mm])', all_text)
    if time_match:
        time_val = f"{time_match.group(1)} {time_match.group(2).upper()}"

    return place, date, time_val, lat, lng


# ===== Main 2-stage hybrid extraction =====
def extract_from_image_hybrid(image_bytes, person_name):
    """
    2-stage hybrid image extraction:
      Stage 1: PaddleOCR (English) on footer crop → Place, Date, Time
      Stage 2: PaddleOCR (Hindi) on background crop → text-only qwen3.5 → Doctor/Building name
    Falls back to old generate_from_image() if PaddleOCR fails on either crop.
    """
    import tempfile
    temp_files = []
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        # ---- Stage 1: Footer (bottom 25%) ----
        footer = img.crop((0, int(h * 0.75), w, h))
        footer_path = _save_temp_crop(footer, temp_files)
        footer_result = ocr_en.ocr(footer_path, cls=True)
        print(f"[DEBUG] Footer raw OCR result count: {len(footer_result[0]) if footer_result and footer_result[0] else 0}")
        if footer_result and footer_result[0]:
            for i, line in enumerate(footer_result[0]):
                print(f"  [DEBUG] Footer text {i}: '{line[1][0]}' (conf={line[1][1]:.2f}, y_center={(line[0][0][1]+line[0][2][1])/2:.0f})")
        footer_rows = _group_ocr_into_rows(footer_result)
        print(f"[DEBUG] Footer grouped rows ({len(footer_rows)}):")
        for i, row in enumerate(footer_rows):
            print(f"  [DEBUG] Row {i}: '{row}'")
        place, date, time_val, lat, lng = _parse_footer_rows(footer_rows)
        print(f"[DEBUG] Parsed → Place: {place}, Date: {date}, Time: {time_val}, Lat: {lat}, Lng: {lng}")

        # ---- Stage 2: Background (top 75%) — use vision model ----
        # OCR struggles with Hindi signboards, vision model can read them directly
        background = img.crop((0, 0, w, int(h * 0.75)))

        # Resize to max 1024px for a better balance of speed and legibility
        bg_w, bg_h = background.size
        max_dim = 1024
        if max(bg_w, bg_h) > max_dim:
            scale = max_dim / max(bg_w, bg_h)
            background = background.resize(
                (int(bg_w * scale), int(bg_h * scale)),
                Image.LANCZOS
            )

        buf = io.BytesIO()
        background.convert("RGB").save(buf, format='JPEG')
        bg_image_bytes = buf.getvalue()

        bg_prompt = (
            "Look at this photo. Is there any doctor name, hospital name, "
            "or building name visible on a signboard or wall? "
            "Text may be in Hindi, Urdu, or English. "
            "Transliterate Hindi/Urdu to English. "
            "Reply ONLY with the name. If nothing found, reply: Not found"
        )
        bg_answer = generate_from_image(bg_prompt, bg_image_bytes, timeout=90, think=False)
        # If vision timed out or errored, just say Not found
        if bg_answer.startswith("Error:"):
            bg_answer = "Not found"
        print(f"[DEBUG] Vision model background answer: '{bg_answer}'")

        # ---- Final output ----
        return (
            f"{{Name: {person_name}, Place: {place}, Date: {date}, "
            f"Time: {time_val}, Any Doctor Name or Building Name Visible in background: {bg_answer}}}"
        )

    except Exception as e:
        # Fallback: use old full-image vision call
        print(f"PaddleOCR failed, falling back to vision model: {e}")
        from prompts import get_image_extraction_prompt
        prompt = get_image_extraction_prompt(person_name)
        return generate_from_image(prompt, image_bytes)
    finally:
        # Clean up temp files
        for f in temp_files:
            try:
                os.remove(f)
            except Exception:
                pass


def _save_temp_crop(pil_img, temp_files_list):
    """Save a PIL Image to a temp JPEG file and return the path."""
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix='.jpg', delete=False)
    pil_img.convert("RGB").save(tmp, format='JPEG')
    tmp.close()
    temp_files_list.append(tmp.name)
    return tmp.name


# =====================================================================
# Doctor name parsing from image filename
# =====================================================================

def parse_doctor_names(filename):
    """Parse doctor name(s) from an image filename.

    Supported formats:
      "Dr abc.jpeg"                   → ["Dr abc"]
      "Dr abc Dr asd Dr kjd.jpeg"     → ["Dr abc", "Dr asd", "Dr kjd"]
      "Dr abc, Dr asd, Dr kjd.jpeg"   → ["Dr abc", "Dr asd", "Dr kjd"]
      "Dr abc,Dr asd,Dr kjd.jpeg"     → ["Dr abc", "Dr asd", "Dr kjd"]

    Returns:
      (list[str], error_str or None)
      On success: (["Dr abc", "Dr asd"], None)
      On failure: ([], "error message")
    """
    # Strip extension
    name_part = os.path.splitext(filename)[0].strip()

    if not name_part:
        return [], "Empty filename."

    # Check if it starts with "Dr" (case-insensitive)
    if not re.match(r'^[Dd][Rr]\b', name_part):
        return [], (
            "Photo name must start with 'Dr'. "
            "The image should be of a rep's selfie at a doctor's cabin, "
            "and the file must be named after the doctor (e.g., 'Dr Sharma.jpeg')."
        )

    # Split on comma first, then on space-before-"Dr"
    # Strategy: replace commas with a unique separator, then split on " Dr" boundary
    # This handles: "Dr abc, Dr asd" and "Dr abc Dr asd" and "Dr abc,Dr asd"
    normalized = re.sub(r',\s*', ' , ', name_part)  # ensure space around commas
    # Split on comma or on space boundary before "Dr" (but not the first one)
    parts = re.split(r'\s*,\s*|\s+(?=[Dd][Rr]\b)', normalized)

    doctors = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # Each part should start with Dr
        if re.match(r'^[Dd][Rr]\b', part):
            doctors.append(part)
        # Skip stray fragments that don't start with Dr

    if not doctors:
        return [], "Could not parse any doctor names from the filename."

    return doctors, None


# =====================================================================
# Crosscheck-specific extraction (no LLM call)
# =====================================================================

def extract_for_crosscheck(image_bytes, person_name, filename):
    """Crosscheck-aware image extraction.

    - Stage 1: PaddleOCR on footer → date, time, lat, lng, place
    - Doctor name: parsed from filename (no LLM vision call)

    Returns:
      dict with keys: doctors, place, date, time, lat, lng, month_year, error
    """
    # Parse doctor names from filename
    doctors, name_error = parse_doctor_names(filename)
    if name_error:
        return {"error": name_error}

    import tempfile
    temp_files = []
    try:
        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        # Footer OCR (bottom 15%) — GPS overlay is at the very bottom
        footer = img.crop((0, int(h * 0.85), w, h))
        MAX_OCR_WIDTH = 600
        if footer.width > MAX_OCR_WIDTH:
            ratio = MAX_OCR_WIDTH / footer.width
            footer = footer.resize(
                (MAX_OCR_WIDTH, int(footer.height * ratio)),
                Image.LANCZOS
            )
        footer_path = _save_temp_crop(footer, temp_files)
        t0 = time.perf_counter()
        footer_result = ocr_en.ocr(footer_path, cls=False)
        print(f"[OCR] {filename} footer: {time.perf_counter()-t0:.1f}s ({footer.width}x{footer.height}px)")
        footer_rows = _group_ocr_into_rows(footer_result)
        place, date, time_val, lat, lng = _parse_footer_rows(footer_rows)

        # Derive month_year from date (DD/MM/YYYY → MM_YYYY)
        month_year = None
        if date and date != "Not found":
            try:
                parts = date.split('/')
                if len(parts) == 3:
                    month_year = f"{parts[1].zfill(2)}_{parts[2]}"
            except Exception:
                pass

        # Build the Chroma document string (same format, doctor name from filename)
        doctor_names_str = ", ".join(doctors)
        chroma_doc = (
            f"{{Name: {person_name}, Place: {place}, Date: {date}, "
            f"Time: {time_val}, Doctor Name in background: {doctor_names_str}}}"
        )

        return {
            "doctors": doctors,
            "place": place,
            "date": date,
            "time": time_val,
            "lat": lat,
            "lng": lng,
            "month_year": month_year,
            "chroma_doc": chroma_doc,
            "error": None,
        }

    except Exception as e:
        return {"error": f"Image processing failed: {e}"}
    finally:
        for f in temp_files:
            try:
                os.remove(f)
            except Exception:
                pass
