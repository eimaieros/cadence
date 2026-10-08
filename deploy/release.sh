#!/bin/bash
# Release one version of cadence on this host:  release.sh <git-sha>
#
# Runs as root, over SSM, from .github/workflows/deploy.yml, which first writes
# compose.yml, the Caddyfile and this script into /opt/cadence. Secrets come
# from Secrets Manager through the instance role, go into an env file only root
# can read, and never pass through GitHub.
#
# If the new version does not come up healthy, the previous one is started
# again and the release fails. That rollback covers code, not schema: the API
# runs `alembic upgrade head` when it starts, so a migration has to leave the
# previous release able to run (add a column, backfill, drop it a release
# later) for the rollback to land on something that works.
set -euo pipefail

TAG="${1:?usage: release.sh <image-tag>}"
cd /opt/cadence

# The data volume is mounted with nofail, so a host can boot without it. Then
# PostgreSQL would quietly initialise an empty cluster on the root volume, which
# goes away with the instance. Refuse instead.
if ! mountpoint -q /srv/cadence; then
  echo "/srv/cadence is not mounted: the data volume is missing" >&2
  exit 1
fi

imds() {
  local token
  token="$(curl -fsS -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')"
  curl -fsS -H "X-aws-ec2-metadata-token: ${token}" \
    "http://169.254.169.254/latest/meta-data/$1"
}

AWS_REGION="$(imds placement/region)"
export AWS_REGION

config="$(aws ssm get-parameter --name /cadence/deploy --query Parameter.Value --output text)"
field() { jq -er "$1" <<<"$config"; }
secret() {
  aws secretsmanager get-secret-value --secret-id "$(field "$1")" \
    --query SecretString --output text
}

# The secret holds "unset" until someone puts a real key in it, and an empty
# key runs the scripted interviewer.
anthropic="$(secret .secrets.anthropicApiKey)"
case "$anthropic" in
  sk-ant-*) ;;
  *) anthropic="" ;;
esac

umask 077
cat > /etc/cadence/release.env <<ENV
AWS_REGION=${AWS_REGION}
LOG_GROUP=$(field .logGroup)
API_IMAGE=$(field .apiRepository)
WEB_IMAGE=$(field .webRepository)
PUBLIC_URL=$(field .publicUrl)
POSTGRES_PASSWORD=$(secret .secrets.dbPassword)
JWT_SECRET=$(secret .secrets.jwtSecret)
ORIGIN_SECRET=$(secret .secrets.originVerify)
ANTHROPIC_API_KEY=${anthropic}
ENV

compose() {
  local tag="$1"
  shift
  TAG="$tag" docker compose --project-directory /opt/cadence \
    --env-file /etc/cadence/release.env "$@"
}

previous="$(cat /opt/cadence/current 2>/dev/null || true)"

compose "$TAG" pull --quiet
if compose "$TAG" up --detach --remove-orphans --wait --wait-timeout 240; then
  echo "$TAG" > /opt/cadence/current
  docker image prune --all --force --filter "until=168h" > /dev/null
  echo "released ${TAG}"
  exit 0
fi

echo "release ${TAG} did not come up healthy" >&2
compose "$TAG" ps >&2 || true
compose "$TAG" logs --tail 40 api >&2 || true
if [ -n "$previous" ] && [ "$previous" != "$TAG" ]; then
  echo "starting ${previous} again" >&2
  compose "$previous" up --detach --remove-orphans --wait --wait-timeout 240 || true
fi
exit 1
