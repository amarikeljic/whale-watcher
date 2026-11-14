# Pinned by digest (the multi-platform index for python:3.12-slim, read from the Docker
# registry on 2026-11-14), so a rebuild uses the same base. To move to a newer base on
# purpose, replace the digest and note it in docs/dependencies.md.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app

# Requirements first, so a code change does not reinstall the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY whalewatcher ./whalewatcher
COPY config.yaml ./config.yaml

# Which commit this image was built from, stamped on every row the service writes
# (whalewatcher/version.py). The image has no .git, so it is passed in at build time:
#   GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
# Without it the rows say "unknown"; the model version is still recorded.
ARG GIT_COMMIT=unknown
ENV WHALEWATCHER_COMMIT=$GIT_COMMIT

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
