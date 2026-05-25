FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

ENV PVE_BACKUP_DATA_DIR=/data \
    PVE_BACKUP_DIR=/backups \
    PVE_BACKUP_HOST=0.0.0.0 \
    PVE_BACKUP_PORT=5000 \
    PVE_BACKUP_USE_WAITRESS=true

VOLUME ["/data", "/backups"]

CMD ["python", "run.py"]
