import hashlib

def generate_pdf_hash(pdf_bytes):
    return hashlib.sha256(pdf_bytes).hexdigest()
