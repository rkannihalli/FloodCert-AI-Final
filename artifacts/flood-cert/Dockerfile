FROM python:3.11-slim
# SIMPLE_BUILD_1781726727

RUN apt-get update && apt-get install -y \
    libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
    libcairo2 libgdk-pixbuf-2.0-0 libffi-dev libssl-dev \
    shared-mime-info fonts-liberation fonts-dejavu-core \
    fonts-freefont-ttf && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip show weasyprint
COPY . .

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
