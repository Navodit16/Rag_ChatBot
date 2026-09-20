import uuid
import io
import docx

def chunk_text(text, chunk_size=750, overlap=150):
    words = text.split()
    chunks = []
    
    if not words:
        return chunks
        
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        chunk = " ".join(words[start:end])
        chunks.append(chunk)
        start += (chunk_size - overlap)
        
    return chunks

def process_txt(file_bytes):
    return file_bytes.decode('utf-8', errors='ignore')

def process_docx(file_bytes):
    doc = docx.Document(io.BytesIO(file_bytes))
    return "\n".join([paragraph.text for paragraph in doc.paragraphs])
