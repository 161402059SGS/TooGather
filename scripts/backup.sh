#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Encrypted database backup for TooGather.
#
# Usage:
#   scripts/backup.sh <age-public-key>
#
# Creates backups/toogather-YYYYmmdd-HHMMSS.sql.gz.age next to the compose file.
# The backup is encrypted with `age` (https://age-encryption.org), so a copied
# backup file is useless without the matching private key.
#
# Copy the resulting file OFF this machine (company drive, cloud storage).
# A backup stored on the same disk does not survive that disk failing.
#
# Restore (on a fresh install, with the stack running):
#   age -d -i key.txt backups/FILE.sql.gz.age | gunzip | \
#     docker compose exec -T db psql -U toogather -d toogather
# ---------------------------------------------------------------------------
set -euo pipefail

RECIPIENT="${1:-}"
if [[ -z "$RECIPIENT" ]]; then
  echo "Usage: $0 <age-public-key>   (create a key pair with: age-keygen -o key.txt)" >&2
  exit 1
fi
command -v age >/dev/null || { echo "Install 'age' first: https://age-encryption.org" >&2; exit 1; }

mkdir -p backups
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/toogather-${STAMP}.sql.gz.age"

# pg_dump runs inside the database container; plain SQL keeps restores simple.
docker compose exec -T db pg_dump -U toogather -d toogather --no-owner \
  | gzip \
  | age -r "$RECIPIENT" > "$OUT"

echo "Backup written to $OUT"
