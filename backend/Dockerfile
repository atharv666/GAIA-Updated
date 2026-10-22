FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY gaia ./gaia
COPY scripts ./scripts

RUN useradd --create-home gaia && mkdir -p /app/data && chown gaia /app/data
USER gaia

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health')"
CMD ["uvicorn", "gaia.main:app", "--host", "0.0.0.0", "--port", "8000"]
