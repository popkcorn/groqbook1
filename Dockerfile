# Bookwright — single-stage image, no system PDF libraries required
# (unlike the old WeasyPrint path, which needed Pango/Cairo in packages.txt).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# DejaVu is bundled with the base image via fonts-dejavu-core; install it
# explicitly so the PDF exporter always finds a Unicode serif.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

COPY requirements-bookwright.txt ./
RUN pip install --no-cache-dir -r requirements-bookwright.txt

COPY bookwright ./bookwright
COPY static ./static
COPY server.py ./

RUN useradd --create-home --uid 10001 bookwright \
 && chown -R bookwright:bookwright /app
USER bookwright

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
