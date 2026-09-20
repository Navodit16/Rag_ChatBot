"""
System prompts for the RAG Output Pipeline.
Each function returns a formatted prompt string ready to send to the LLM.
Edit these prompts freely - the backend imports them by function name.
"""


# =================================================================
# STREAM C - Intent Extraction (Approach 4: LLM extracts intent,
#            Python builds SQL deterministically)
# =================================================================

# JSON Schema for intent extraction (kept for reference, using simple json mode now)
INTENT_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "answerable": {
            "type": "boolean"
        },
        "filter_keywords": {
            "type": "array",
            "items": {"type": "string"}
        },
        "filter_column": {
            "type": "string"
        },
        "select_columns": {
            "type": "array",
            "items": {"type": "string"}
        },
        "aggregation": {
            "type": "string",
            "enum": ["none", "SUM", "AVG", "COUNT", "MAX", "MIN"]
        },
        "exclude_values": {
            "type": "array",
            "items": {"type": "string"}
        }
    },
    "required": [
        "answerable", "filter_keywords", "filter_column",
        "select_columns", "aggregation", "exclude_values"
    ]
}


def get_intent_extraction_prompt(
    query: str, table_name: str, schema: str,
    column_names: list[str],
    sample_rows: str = "", tail_rows: str = ""
) -> str:
    """Prompt for Stream C - Intent Extraction.

    The LLM extracts structured intent as JSON. Python builds the SQL.
    This replaces raw SQL generation with a much more reliable approach.
    """
    sample_section = ""
    if sample_rows:
        sample_section = f"""
Sample rows (first 5 data rows):
{sample_rows}
"""

    tail_section = ""
    if tail_rows:
        tail_section = f"""
Last 5 rows:
{tail_rows}
"""

    columns_list = ", ".join(f'"{c}"' for c in column_names)

    return f"""You are a data analyst. Study the table below carefully, then output a JSON object.

TABLE NAME: {table_name}
COLUMNS: [{columns_list}]
SCHEMA (with types): {schema}
{sample_section}{tail_section}
USER QUESTION: {query}

INSTRUCTIONS - read the sample data carefully before answering:

1. "answerable": Can this table answer the question? true or false.

2. "filter_keywords": Extract EVERY identifying word from the question that helps locate the right rows. Study the sample data to understand what kind of values each column contains. Include ALL words - names, places, codes, abbreviations. Example: if the question is "sale of mani pharmacy dlw", the keywords are ["mani", "pharmacy", "dlw"]. NEVER leave any keyword out.

3. "filter_column": Which column contains those keywords? Look at the sample rows. If you see values like "DR.SHALIGRAM       CR" or "MANI PHARMACY       DLW" in one column, that is the filter column.

4. "select_columns": Which column(s) should be returned?
   - If the user asks for "total", "total sale", or "overall" and a column like "Total_Value" exists, use ["Total_Value"].
   - If the user asks about specific periods ("august", "september"), use those column names like ["Aug", "Sep"].
   - If the user says "show all" or "list", use all column names.

5. "aggregation": Use "none" for looking up a specific entity. Use "SUM" when user wants total across many rows. Use "COUNT" for "how many". Use "AVG" for averages.

6. "exclude_values": Look at the last 5 rows. If any first-column value is clearly a summary label (like "NET SALES", "GRAND TOTAL") rather than a real entity, list those values here. Otherwise use an empty list.

Output ONLY valid JSON. No explanation, no extra text."""


def get_sql_generation_prompt_fallback(
    query: str, table_name: str, schema: str,
    sample_rows: str = "", tail_rows: str = ""
) -> str:
    """Fallback prompt - raw SQL generation (used if intent extraction fails)."""
    sample_section = ""
    if sample_rows:
        sample_section = f"""
Sample rows:
{sample_rows}
"""
    tail_section = ""
    if tail_rows:
        tail_section = f"""
Last 5 rows:
{tail_rows}
"""
    return f"""You are an expert MySQL query generator.

TABLE: `{table_name}`
SCHEMA: {schema}
{sample_section}{tail_section}
QUESTION: {query}

RULES:
1. ALWAYS wrap the table name in backticks: `{table_name}`. Wrap ALL column names in backticks.
2. Extract every keyword from the question and include each as a LIKE '%keyword%' condition. Never drop any keyword.
3. If the user asks for total/total sale and a Total_Value column exists, use it directly.
4. For varchar numeric columns, use: CAST(NULLIF(REPLACE(`col`, '-', ''), '') AS DECIMAL(15,2))

Output ONLY the raw SQL query or the word Nothing."""


def get_final_consolidation_prompt(
    query: str,
    doc_summary: str,
    img_summary: str,
    excel_output: str,
    history: list[str]
) -> str:
    """Prompt for the final consolidated LLM call.

    Receives pre-condensed summaries from each stream (NOT raw dumps).
    The backend truncates/condenses data before passing it here to
    avoid needle-in-haystack issues.
    """
    if excel_output is None:
        excel_text = "No structured/tabular data was found."
    else:
        excel_text = str(excel_output)

    if history:
        history_text = "\n".join(f"{i+1}. {h}" for i, h in enumerate(history))
    else:
        history_text = "None"

    return f"""You are a precise data assistant. Answer the user's question using ONLY the sources below.

User Query: {query}

=== SOURCE 1: Excel/Tabular Data (SQL Results) ===
{excel_text}

=== SOURCE 2: Documents ===
{doc_summary}

=== SOURCE 3: Photos/Image Records ===
{img_summary}

=== Previous Responses (for conversational continuity) ===
{history_text}

CRITICAL RULES:
1. ALL three sources are valid evidence. If ANY source contains relevant data, you MUST include it in your answer. You CANNOT say "no information found" if Source 2 or Source 3 contain relevant matches.
2. When Source 1 (SQL) returns actual data rows with numbers, report those numbers directly (e.g. "The total sales of X is Rs. 357.1"). Use Indian Rupees (Rs.) for all currency values.
3. When Source 1 returns NO rows but Source 2 or Source 3 contain relevant information (e.g. a photo record showing a visit), you MUST use that evidence to answer the question.
4. Source 3 (Photos) contains records of field visits. Each record includes the person name, place, date, time, and the doctor visited. If the user asks about a visit and Source 3 shows a matching record, that IS evidence of the visit.
5. Only say "no information found" if ALL three sources genuinely have no relevant data.
6. Be concise. If the answer is a single number, just give it with context.
7. Do not fabricate information not present in the sources."""
