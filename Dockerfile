FROM python:3.12-slim

WORKDIR /app

# Requirements first, so a code change does not reinstall the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY whalewatcher ./whalewatcher
COPY config.yaml ./config.yaml

# The database belongs on the mounted volume, or the next `docker rm` takes the
# paper-trading history with it.
ENV WHALEWATCHER_DB=/app/data/whalewatcher.sqlite3 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No EXPOSE: the service only makes outbound requests and nothing listens.
#
# The heartbeat is written after every poll. The first start scores wallets
# before the first poll, which can take half an hour, hence the long start period.
HEALTHCHECK --interval=2m --timeout=30s --start-period=45m --retries=3 \
    CMD ["python", "-m", "whalewatcher", "health"]

# No USER, as in the UFC bot: the container writes into the bind-mounted
# /app/data, and a uid that does not own that directory on the host could not.
CMD ["python", "-m", "whalewatcher", "monitor"]
