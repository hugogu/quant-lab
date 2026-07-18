FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector/ ./collector/
COPY api/ ./api/  # seed.py uses db_dsn from collector.db
ENV PYTHONPATH=/app
CMD ["python", "-m", "collector.scheduler"]
