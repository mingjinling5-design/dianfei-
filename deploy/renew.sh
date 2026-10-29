#!/bin/sh
set -eu
cd /opt/yispower
docker compose --profile maintenance run --rm certbot renew --quiet
docker compose exec -T nginx nginx -s reload
