#!/bin/bash
# Apply a Targeto update on the Oracle VM. Run ON THE VM (via Freebuff or SSH)
# after pushing code to GitHub:
#   bash ~/targeto/deploy/apply.sh
#
# Does, in order (all idempotent, safe to re-run):
#   1. Syncs ~/targeto to origin/main (first run converts the tarball deploy
#      into a git checkout; a backup is left at ~/targeto.bak).
#   2. Applies db/migrations/*.sql to Postgres (new columns etc.).
#   3. Rebuilds ONLY the dashboard container.
#   4. Imports data/fb_page_corrections.csv (corrected pages, verified
#      statuses, manual review queue) into the muse_* columns.
#   5. Health-checks the dashboard.
#
# Postgres and its data volume are NEVER wiped or recreated.
set -euo pipefail

REPO="Sarojkumar9999/targeto-senior-home-care-pipeline"
cd "$HOME/targeto"

echo "== 1/5 syncing code to origin/main"
if [ ! -d .git ]; then
  echo "first run: converting tarball deploy to a git checkout"
  cp -r "$HOME/targeto" "$HOME/targeto.bak"
  git init -b main -q
  git remote add origin "https://github.com/${REPO}.git" 2>/dev/null || true
fi
git fetch origin -q
git reset --hard origin/main -q
git log --oneline -1

echo "== 2/5 applying DB migrations"
for f in db/migrations/*.sql; do
  [ -e "$f" ] || continue
  echo "-- $f"
  sudo docker exec -i targeto-postgres psql -U targeto -d targeto -v ON_ERROR_STOP=1 -q < "$f"
done

echo "== 3/5 rebuilding dashboard (database untouched)"
sudo docker compose -f deploy/docker-compose.yml up -d --build dashboard

echo "== 4/5 importing Muse verifications (corrected pages + manual review queue)"
if [ -f data/fb_page_corrections.csv ]; then
  sudo docker compose -f deploy/docker-compose.yml run --rm dashboard python -m pipeline.muse_import data/fb_page_corrections.csv
elif [ -f data/muse_verifications.csv ]; then
  sudo docker compose -f deploy/docker-compose.yml run --rm dashboard python -m pipeline.muse_import data/muse_verifications.csv
else
  echo "no corrections csv yet, skipping"
fi

echo "== 5/5 health check"
for i in $(seq 1 12); do
  if curl -sf http://localhost:8010/login -o /dev/null; then
    echo "dashboard OK"
    exit 0
  fi
  sleep 5
done
echo "dashboard did NOT come up" >&2
sudo docker logs targeto-dashboard --tail 30 >&2 || true
exit 1
