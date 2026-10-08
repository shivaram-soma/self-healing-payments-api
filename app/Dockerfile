# Small official Python image
FROM python:3.12-slim

# Don't write .pyc files; print logs immediately
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application code (tests stay out of the image)
COPY main.py store.py ./

# Run as a normal user, not root
RUN useradd --create-home --uid 10001 appuser
USER 10001

EXPOSE 8000

# 1 worker process with 4 threads keeps Prometheus metrics simple
# (one process = one set of counters). A short graceful timeout means a
# stuck container is replaced quickly when Kubernetes restarts it.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", \
     "--timeout", "60", "--graceful-timeout", "10", "main:create_app()"]
