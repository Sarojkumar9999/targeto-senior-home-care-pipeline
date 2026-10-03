FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY pipeline ./pipeline
COPY db ./db
EXPOSE 8010
CMD ["python", "-m", "pipeline.app"]
