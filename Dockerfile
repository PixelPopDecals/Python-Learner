FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc python3-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -m textblob.download_corpora

# Copy application files
COPY bot.py .
COPY challenges.json .

# Create data directory for SQLite
RUN mkdir -p /data

# Run the bot
CMD ["python", "bot.py"]