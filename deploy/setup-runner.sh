#!/bin/bash
# One-time setup: GitHub Actions self-hosted runner on the Oracle VM.
#
# Run ON THE VM as the ubuntu user:
#   bash ~/targeto/deploy/setup-runner.sh <runner-token>
#
# Get the token: open the GitHub repo → Settings → Actions → Runners →
#   "New self-hosted runner" → Linux / x64 → copy the token shown there.
#
# What it does:
#   1. Converts ~/targeto (tarball deploy) into a git checkout of origin/main.
#      The postgres data volume is NOT touched — your 4,144 agencies are safe.
#      A backup is left at ~/targeto.bak just in case.
#   2. Downloads + configures the GitHub Actions runner.
#   3. Installs it as a systemd service so it survives reboots.
set -euo pipefail

TOKEN="${1:?Usage: bash ~/targeto/deploy/setup-runner.sh <runner-token>}"
REPO="Sarojkumar9999/targeto-senior-home-care-pipeline"
RUNNER_VER="2.337.0"

echo "== 1/3 converting ~/targeto to a git checkout (DB volume untouched)"
if [ ! -d "$HOME/targeto/.git" ]; then
  cp -r "$HOME/targeto" "$HOME/targeto.bak"
  echo "backup at ~/targeto.bak"
  cd "$HOME/targeto"
  git init -b main -q
  git remote add origin "https://github.com/${REPO}.git" 2>/dev/null || true
  git fetch origin -q
  git reset --hard origin/main -q
  echo "code now matches origin/main"
else
  echo "already a git checkout, skipping"
fi

echo "== 2/3 installing actions runner v${RUNNER_VER}"
mkdir -p "$HOME/actions-runner" && cd "$HOME/actions-runner"
if [ ! -f .runner ]; then
  if [ ! -f config.sh ]; then
    curl -sSL -o runner.tar.gz \
      "https://github.com/actions/runner/releases/download/v${RUNNER_VER}/actions-runner-linux-x64-${RUNNER_VER}.tar.gz"
    tar xzf runner.tar.gz
  fi
  ./config.sh --unattended --url "https://github.com/${REPO}" \
    --token "$TOKEN" --name targeto-vm --labels targeto-vm
else
  echo "runner already configured, skipping"
fi

echo "== 3/3 installing as a service"
if ! sudo ./svc.sh status >/dev/null 2>&1; then
  sudo ./svc.sh install
fi
sudo ./svc.sh start
sudo ./svc.sh status

echo ""
echo "RUNNER READY. Push to main and the VM deploys itself."
echo "Watch it live: GitHub repo → Actions tab."
