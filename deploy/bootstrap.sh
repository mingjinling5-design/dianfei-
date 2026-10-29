#!/bin/sh
set -eu
cd /opt/yispower
if [ ! -f .env ]; then
    if [ -z "${1:-}" ]; then echo 'Usage: sudo sh deploy/bootstrap.sh PUBLIC_IP' >&2; exit 1; fi
    python3 deploy/generate_env.py --host "$1"
fi
set -a
. ./.env
set +a
mkdir -p deploy/acme deploy/letsencrypt deploy/backups
chmod 700 deploy/letsencrypt deploy/backups
docker compose config --quiet
docker compose build
docker compose up -d db migrate
docker compose run --rm web python manage.py test portal.accounts --verbosity 1
if docker inspect ysp-acme-bootstrap >/dev/null 2>&1; then
    echo 'An existing bootstrap container needs inspection before retry.' >&2
    exit 1
fi
docker run -d --name ysp-acme-bootstrap --label com.ysp.bootstrap=true -p 80:80 \
    -v /opt/yispower/deploy/bootstrap.conf:/etc/nginx/conf.d/default.conf:ro \
    -v /opt/yispower/deploy/acme:/var/www/acme:ro nginx:stable-alpine
trap 'docker rm -f ysp-acme-bootstrap >/dev/null 2>&1 || true' EXIT
if [ -n "${ACME_EMAIL:-}" ]; then
    docker compose --profile maintenance run --rm certbot certonly --non-interactive --agree-tos \
        --email "$ACME_EMAIL" --preferred-profile shortlived --webroot --webroot-path /var/www/acme \
        --ip-address "$YSP_HOST" --cert-name "$YSP_HOST"
else
    docker compose --profile maintenance run --rm certbot certonly --non-interactive --agree-tos \
        --register-unsafely-without-email --preferred-profile shortlived --webroot --webroot-path /var/www/acme \
        --ip-address "$YSP_HOST" --cert-name "$YSP_HOST"
fi
docker rm -f ysp-acme-bootstrap
trap - EXIT
docker compose up -d
install -m 644 deploy/ysp-renew.service deploy/ysp-renew.timer deploy/ysp-backup.service deploy/ysp-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ysp-renew.timer ysp-backup.timer
docker compose exec -T web python manage.py check --deploy --fail-level WARNING
docker compose ps
