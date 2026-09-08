FROM python:3.12-slim

# tesseract-ocr: the actual OCR engine pytesseract wraps (pip can't
# install this itself). No extra PyMuPDF system deps needed - it ships
# its own bundled MuPDF, unlike pdf2image/Wand which need Poppler/
# ImageMagick installed separately.
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD gunicorn app:app --bind 0.0.0.0:$PORT --timeout 120 --preload
