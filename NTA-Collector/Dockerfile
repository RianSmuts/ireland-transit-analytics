# NTA GTFS-Realtime collector. Build and run it with docker-compose.yml (see README.md).
FROM python:3.13-slim

# Log straight to `docker logs`, and skip .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector.py .

# Run as an ordinary user rather than root. docker-compose.yml says which one,
# so the files written to /data belong to you on the server.
USER 1000:1000

CMD ["python", "collector.py"]
