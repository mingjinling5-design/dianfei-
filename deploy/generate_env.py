"""Run on the deployment machine. Never prints generated secrets."""
import argparse
import base64
import ipaddress
import secrets
import re
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('--host',required=True,help='Public IP address of the deployment server')
p.add_argument('--email',default='')
args=p.parse_args()
ipaddress.ip_address(args.host)
if args.email and not re.fullmatch(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',args.email):
    p.error('Invalid email address')
target=Path(__file__).resolve().parent.parent/'.env'
text=f'''YSP_HOST={args.host}
YSP_ALLOWED_HOSTS={args.host}
YSP_TRUSTED_ORIGINS=https://{args.host}
DJANGO_SECRET_KEY={secrets.token_urlsafe(64)}
YSP_CREDENTIAL_KEY={base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()}
POSTGRES_PASSWORD={secrets.token_urlsafe(32)}
ACME_EMAIL={args.email}
'''
with target.open('x',encoding='utf-8') as f:f.write(text)
target.chmod(0o600)
print('Created private .env; keep it with your encrypted database backups.')
