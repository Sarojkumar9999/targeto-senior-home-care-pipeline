#!/bin/bash
# Ship Targeto to the Oracle VM and bring the dashboard live.
# Usage: bash deploy/push-to-vm.sh
set -euo pipefail

HOST=ubuntu@80.225.252.48
KEY="$HOME/Downloads/ssh-key-2026-09-30.key"
SSH="ssh -i $KEY -o BatchMode=yes $HOST"
SCP="scp -i $KEY -o BatchMode=yes"
BACKUP=backups/targeto_freeze_2026-09-30.sql

echo "== 1/6 upload project (code + db + deploy kit, no junk)"
tar czf /tmp/targeto-ship.tgz --exclude='.venv' --exclude='.git' --exclude='__pycache__' \
    --exclude='backups' --exclude='*.pyc' --exclude='.env' \
    pipeline db docker-compose.yml Dockerfile requirements.txt deploy
$SCP /tmp/targeto-ship.tgz "$HOST:/tmp/"

echo "== 2/6 bootstrap server (swap, docker, firewall)"
$SSH 'mkdir -p ~/targeto && tar xzf /tmp/targeto-ship.tgz -C ~/targeto && sh ~/targeto/deploy/setup-vm.sh'

echo "== 3/6 build + start containers (postgres + dashboard, no pgadmin)"
$SSH 'cd ~/targeto && sudo docker compose -f deploy/docker-compose.yml up -d --build 2>&1 | tail -3'

echo "== 4/6 wait for postgres healthy"
$SSH 'for i in $(seq 1 30); do sudo docker exec targeto-postgres pg_isready -U targeto -d targeto >/dev/null 2>&1 && break; sleep 2; done; sudo docker exec targeto-postgres pg_isready -U targeto -d targeto'

echo "== 5/6 upload + restore frozen DB"
$SCP "$BACKUP" "$HOST:/tmp/targeto_restore.sql"
$SSH 'sudo docker exec -i targeto-postgres psql -U targeto -d targeto -v ON_ERROR_STOP=1 -q < /tmp/targeto_restore.sql 2>&1 | grep -v "^$" | tail -3; rm -f /tmp/targeto_restore.sql'

echo "== 6/6 verify"
$SSH 'sleep 3; curl -s -o /dev/null -w "dashboard http: %{http_code}\n" http://localhost:8010/login; sudo docker exec -i targeto-postgres psql -U targeto -d targeto -t -c "SELECT count(*) FROM agencies;"; sudo docker ps --format "{{.Names}} | {{.Status}}"'
echo "DEPLOY DONE → http://80.225.252.48:8010"
