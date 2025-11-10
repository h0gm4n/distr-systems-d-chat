# Use Python 3.14 slim image as base
FROM python:3.14-slim

# Set working directory
WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy server application files
COPY server/ ./server/

# Create directory for chat logs and configs
RUN mkdir -p /app/data /app/configs

# Copy default config files
COPY server/config_coordinator.json ./configs/
COPY server/config_worker_example.json ./configs/

# Create a non-root user for security
RUN useradd --create-home --shell /bin/bash appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose the default port (can be overridden)
EXPOSE 9000

# Set the working directory to server for easier imports
WORKDIR /app/server

# Default command - can be overridden
CMD ["python", "main.py", "--mode", "coordinator", "--host", "0.0.0.0", "--port", "9000"]
