#!/bin/sh
set -eu

: "${SITES_ORIGIN_TOKEN:?Set a shared secret for the Sites gateway}"
: "${ADMIN_PASSWORD_HASH:?Set the Argon2 hash used for administrator sign-in}"
: "${SESSION_SECRET:?Set the session signing secret}"
: "${FERNET_KEY:?Set the integration encryption key}"
: "${DATABASE_URL:?Set the database path on the persistent volume}"
: "${UPLOAD_DIR:?Set the upload path on the persistent volume}"

python -m app.cli init-db
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1
