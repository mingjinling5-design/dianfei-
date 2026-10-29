# Ubuntu deployment

Target: `/opt/yispower`, initially five concurrent users. Web and database are
private Docker services. Only nginx ports 80 and 443 are published. The worker
collects per account approximately every ten minutes; PostgreSQL account row
locks prevent duplicate tasks across workers. All rooms, readings, events and
alerts include an owner. Login uses upstream SMS verification and Django's own
HTTP-only session cookie; there is no shared web login and no shared plaintext
credential file in production.

## Installation

1. Install Docker Engine and Docker Compose v2 from supported Ubuntu packages.
2. Copy the clean source package to `/opt/yispower`. Do not upload the local
   `data/`, `.env`, private SSH key or old login captures.
3. Ensure the cloud security group and local firewall permit inbound TCP 80/443.
4. Run `sudo sh deploy/bootstrap.sh YOUR_PUBLIC_IP` from `/opt/yispower`.

The bootstrap generates unique database and encryption keys in `.env` without
printing them, runs database migrations and the dual-account tests against
PostgreSQL, obtains an IP certificate, then starts the website and collector.
It registers an ACME account without an email if ACME_EMAIL is empty. An
administrator email can be set before running the bootstrap.

IP certificates require Certbot >=5.4 and the shortlived profile. The official
Certbot image provides the current client. Reference:
https://letsencrypt.org/2026/03/11/shorter-certs-certbot/
The renewal timer checks twice daily, and nginx reloads certificates after each
successful check. Port 80 remains necessary for the renewal challenge.

## Operations

```sh
cd /opt/yispower
sudo docker compose ps
sudo docker compose logs --tail=50 web worker
sudo systemctl list-timers ysp-renew.timer ysp-backup.timer
sudo sh deploy/backup.sh
sudo sh deploy/renew.sh
```

Backups are PostgreSQL custom-format dumps under `deploy/backups`, with private
permissions. Back up `.env` separately: the credential encryption key is needed
to recover saved upstream sessions. Never include `.env` in a public source
archive. No automatic deletion of old backups is configured.

For an upgrade, back up first, copy source without overwriting `.env` or
`deploy/letsencrypt`, then run `docker compose build` and `docker compose up -d`.
For a schema change, stop web/worker, run the migration service, then start them.

The site exposes a clean corresponding-source download at `/source` with the
third-party notices and licenses. The archive excludes runtime data and keys.
If changing the public address or enabling a proxy/CDN, update allowed hosts,
trusted HTTPS origins and the certificate configuration together.

## Local workstation

`start.ps1` runs Django/Waitress plus a separate collector with local SQLite.
This is for development and five-user isolation checks, not the production
database configuration. The original local history is migrated with
`python manage.py import_local`; accessing it still requires successful upstream
SMS authentication as its owner. Original files remain untouched.
