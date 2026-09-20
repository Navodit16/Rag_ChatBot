"""
Shared token generator for entity name fingerprinting.

Creates canonical, deterministic tokens from raw names so that
"DR.S K RAI             CR" and "dr sk rai" both produce "S_K_RAI".

Used by:
  - input/main.py  (at ingestion — _token columns + entity_tokens registry)
  - output/app.py  (at query time — token-aware SQL builder + table scoring)
"""

import re

# Known title prefixes to strip (order matters: longer first)
_TITLE_PREFIXES = r'^(DR|MR|MRS|MS|PROF|SHRI|SMT)\b\.?\s*'

# Known suffixes to strip
_KNOWN_SUFFIXES = r'\s+(CR|SR|JR|II|III|IV)\s*$'


def generate_token(raw: str) -> str:
    """Generate a canonical token (unique fingerprint) for a name.

    Rules applied in order:
      1. Convert to UPPERCASE
      2. Replace punctuation (dots, dashes, commas, slashes, parens) with spaces
      3. Collapse all whitespace to single spaces
      4. Strip title prefixes: Dr, Mr, Mrs, Ms, Prof, Shri, Smt
      5. Strip known suffixes: CR, SR, JR, II, III, IV
      6. Join remaining tokens with underscores

    Examples:
        "DR.S K RAI             CR"  →  "S_K_RAI"
        "Dr. B.B. Sahou"             →  "B_B_SAHOU"
        "Dr Mridula Verma"           →  "MRIDULA_VERMA"
        "sK RaI"                     →  "SK_RAI"
        "AARAMBH HOSPITAL"           →  "AARAMBH_HOSPITAL"
        "   "                        →  ""
        ""                           →  ""
    """
    if not raw or not str(raw).strip():
        return ''

    s = str(raw).upper().strip()

    # Replace punctuation with spaces
    s = re.sub(r'[.\-,/\\()\[\]{}:;!?@#$%^&*+=~`"\']+', ' ', s)

    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()

    # Strip title prefixes (may appear at start)
    s = re.sub(_TITLE_PREFIXES, '', s, flags=re.IGNORECASE).strip()

    # Strip known suffixes
    s = re.sub(_KNOWN_SUFFIXES, '', s, flags=re.IGNORECASE).strip()

    # Split into tokens, remove empties
    tokens = [t for t in s.split() if t]

    return '_'.join(tokens) if tokens else ''
