# RAG Output Pipeline — Startup Guide

## Prerequisites
- MySQL server running on `localhost:3306` (database `excel_imports` is auto-created)
- Ollama running on `http://localhost:11434` with model `qwen3.5` pulled
- The **input pipeline** must have been used at least once to populate ChromaDB collections (`Doc_store`, `Image_store`, `Excel_store`)

## 1. Activate the Virtual Environment

Open a terminal and run:

```powershell
cd c:\Users\kapoo\Desktop\Excel_to_sql_ui
.\backend\venv\Scripts\Activate
```

## 2. Start the Backend Server

```powershell
cd output
python app.py
```

This starts a FastAPI server on **http://localhost:8002**.

## 3. Open in Browser

Navigate to:

```
http://localhost:8002
```

## File Structure

```
Excel_to_sql_ui/output/
├── app.py              # FastAPI backend — all stream logic
├── prompts.py          # System prompts (edit freely)
├── index.html          # Frontend UI
├── style.css           # Styles (inherited from input/)
├── app.js              # Frontend logic (fetch, render, accordion)
├── history.json        # Auto-created on first query, managed by backend
├── requirements.txt    # Dependency notes
└── README.md           # This file
```

## Notes

- **history.json** is auto-created on the first query. It stores the last 5 final answers for continuity context.
- All three retrieval streams (Documents, Images, Excel/SQL) run **concurrently** to minimize wait time.
- The first query may be slower as the embedding model (`all-MiniLM-L6-v2`) downloads on first use.
- If you encounter a `sentence-transformers` import error, install it in the venv:
  ```powershell
  pip install sentence-transformers
  ```
