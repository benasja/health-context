# Runs health_sync.py next to a Health Bridge receiver: watches its database,
# rebuilds the context pack when the phone has synced, and serves the MCP
# server over `docker exec -i`. See docs/health-bridge.md.
#
# No dependencies to install: the project is standard library only, and the
# slim image already carries the timezone database the importer needs.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY *.py profile.example.md ./

# The receiver keeps its database at mode 0600, so this has to run as the
# same user the receiver does; compose sets that from .env. This default only
# keeps the image from running as root when started by hand.
USER 1000:1000

ENTRYPOINT ["python3", "/app/health_sync.py"]
CMD ["--bridge-db", "/bridge/health.sqlite", "--out-root", "/output"]
