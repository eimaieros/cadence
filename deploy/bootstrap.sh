#!/bin/bash
# First boot of the cadence host: Amazon Linux 2023 on Graviton (arm64).
#
# cloud-init runs this once per instance, as root. It installs Docker, mounts
# the data volume and leaves the machine waiting for its first release, which
# arrives over SSM from .github/workflows/deploy.yml and runs release.sh. Nothing
# here knows about the application: an instance can be thrown away and
# rebuilt, and the only thing that has to survive is the data volume.
set -euxo pipefail
exec > >(tee -a /var/log/cadence-bootstrap.log) 2>&1

retry() {
  local n=0
  until "$@"; do
    n=$((n + 1))
    if [ "$n" -ge 10 ]; then return 1; fi
    sleep $((n * 3))
  done
}

# --- Docker -------------------------------------------------------------------
retry dnf install -y docker amazon-ecr-credential-helper jq
systemctl enable --now docker

# Compose is not packaged for AL2023. Pinned, and checked against the digest
# published with the release, so a swapped binary fails here instead of
# running as root on every deploy.
COMPOSE_VERSION=v5.6.0
COMPOSE_SHA256=733ec76717ceb59052a9609b9dadfb523b2df8eab57a54212872d10a58078ea2
mkdir -p /usr/libexec/docker/cli-plugins
retry curl -fsSL -o /tmp/docker-compose \
  "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-aarch64"
echo "${COMPOSE_SHA256}  /tmp/docker-compose" | sha256sum -c -
install -m 0755 /tmp/docker-compose /usr/libexec/docker/cli-plugins/docker-compose

# Pull from ECR with the instance role: no `docker login`, no stored password.
mkdir -p /root/.docker
echo '{"credsStore": "ecr-login"}' > /root/.docker/config.json

# --- Memory ---------------------------------------------------------------------
# PostgreSQL, the API, the Next.js server and Caddy fit in 2 GiB with room to
# spare. The swap is for the moment they don't, so it degrades instead of the
# OOM killer choosing which of them dies.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- Data volume -------------------------------------------------------------
# A separate EBS volume holds PostgreSQL. It is attached after the instance
# starts, so wait for it, and only format it if it has never been formatted:
# a volume restored from a snapshot must come back with its data.
root_disk="/dev/$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")"
data_disk=""
for _ in $(seq 1 90); do
  data_disk="$(lsblk -dpno NAME,TYPE | awk '$2 == "disk" {print $1}' | grep -vx "$root_disk" | head -1 || true)"
  if [ -n "$data_disk" ]; then break; fi
  sleep 5
done
if [ -z "$data_disk" ]; then
  echo "the data volume was never attached" >&2
  exit 1
fi
if ! blkid "$data_disk"; then
  mkfs.xfs -L cadence-data "$data_disk"
fi
mkdir -p /srv/cadence
if ! grep -q 'LABEL=cadence-data' /etc/fstab; then
  echo 'LABEL=cadence-data /srv/cadence xfs defaults,nofail 0 2' >> /etc/fstab
fi
mount -a
mkdir -p /srv/cadence/pgdata /opt/cadence /etc/cadence
chmod 700 /etc/cadence

echo "bootstrap finished"
