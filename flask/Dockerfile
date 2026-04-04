FROM python:3.12-slim

# Install ffmpeg and tzdata
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY backend/app.py .

# Copy frontend
COPY frontend/ ./frontend/

# Persistent volumes
RUN mkdir -p /data /recordings

EXPOSE 3000

VOLUME ["/data", "/recordings"]

CMD ["python", "app.py"]
