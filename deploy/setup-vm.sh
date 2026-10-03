#!/bin/sh
# Targeto VM bootstrap — Ubuntu 22.04, Oracle E2.1.Micro (1 GB RAM)
# Safe to re-run; every step is idempotent.
if [ "$(id -u)" -ne 0 ]; then exec sudo sh "$0" "$@"; fi
set -e

echo "== 1/5 swap (2 GB — OOM insurance for the 1 GB box)"
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q "^/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

echo "== 2/5 kernel tuning (low-RAM Postgres friendliness)"
cat > /etc/sysctl.d/99-targeto.conf <<EOF
vm.swappiness=10
vm.overcommit_memory=1
EOF
sysctl --system >/dev/null

echo "== 3/5 docker + compose plugin"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq docker.io docker-compose-v2 >/dev/null
systemctl enable --now docker >/dev/null 2>&1
usermod -aG docker ubuntu 2>/dev/null || true

echo "== 4/5 host firewall: allow 8010 (Oracle security list is separate)"
iptables -C INPUT -p tcp --dport 8010 -j ACCEPT 2>/dev/null || iptables -I INPUT -p tcp --dport 8010 -j ACCEPT
mkdir -p /etc/iptables && iptables-save > /etc/iptables/rules.v4 2>/dev/null || true

echo "== 5/5 done. Status:"
free -h | sed -n '1,3p'
docker --version
docker compose version
echo "BOOTSTRAP OK"
