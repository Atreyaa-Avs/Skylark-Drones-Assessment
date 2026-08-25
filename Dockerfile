# Production Dockerfile for Chainlit Founder BI Agent
FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

WORKDIR /app

# Install system dependencies if required
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source files
COPY . .

# Expose Chainlit port
EXPOSE 8000

# Run Chainlit web server
CMD ["chainlit", "run", "app.py", "--host", "0.0.0.0", "--port", "8000", "--headless"]
