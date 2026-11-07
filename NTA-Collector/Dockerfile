# NTA GTFS-Realtime collector. GitHub Actions publishes it for each release (README.md,
# section 4); docker-compose.yml builds it locally for testing (section 3).
FROM python:3.13-slim

# Log straight to `docker logs`, and skip .pyc files.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY collector.py .
# Fail the build, rather than the server, if the collector can't load its packages.
RUN python -c "import collector"

# The release this image is built for: the publishing workflow passes it in, and a local
# build is "dev". The collector logs it at start-up.
ARG VERSION=dev
ENV NTA_COLLECTOR_VERSION=$VERSION

# Run as an ordinary user rather than root. The compose file says which one,
# so the files written to /data belong to you on the server.
USER 1000:1000

CMD ["python", "collector.py"]
