#!/bin/sh
set -eu
umask 077
cd /opt/yispower
mkdir -p deploy/backups
docker compose exec -T db pg_dump -U ysp -d ysp -Fc > "deploy/backups/$(date -u +%Y%m%d-%H%M%S).dump"
