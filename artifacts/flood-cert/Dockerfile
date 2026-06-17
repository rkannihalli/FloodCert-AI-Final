FROM python:3.11-slim
# FORCE_REBUILD_1781726631

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y     libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0     libcairo2 libgdk-pixbuf-2.0-0 libffi-dev libssl-dev     shared-mime-info fonts-liberation fonts-dejavu-core     fonts-freefont-ttf && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Step A: install deps
COPY artifacts/flood-cert/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt && pip show weasyprint | grep Version

# Step B: copy app code
COPY artifacts/flood-cert/ ./

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
