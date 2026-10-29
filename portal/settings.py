import os
import secrets
import ipaddress
from pathlib import Path
from urllib.parse import urlsplit, unquote

BASE_DIR=Path(__file__).resolve().parent.parent
PRODUCTION=os.environ.get('YSP_PRODUCTION')=='1'
DATA=Path(os.environ.get('YSP_DATA_DIR',str(BASE_DIR/'data')))
DATA.mkdir(parents=True,exist_ok=True)
keyfile=DATA/'django-secret.key'
if not PRODUCTION and not keyfile.exists():
    try:
        with keyfile.open('x',encoding='ascii') as f:f.write(secrets.token_urlsafe(64))
    except FileExistsError:pass
SECRET_KEY=os.environ.get('DJANGO_SECRET_KEY') or (keyfile.read_text().strip() if not PRODUCTION else '')
if not SECRET_KEY:raise RuntimeError('Production requires DJANGO_SECRET_KEY')
DEBUG=False
ALLOWED_HOSTS=os.environ.get('YSP_ALLOWED_HOSTS','127.0.0.1,localhost,testserver').split(',')
CSRF_TRUSTED_ORIGINS=[s for s in os.environ.get('YSP_TRUSTED_ORIGINS','').split(',') if s]
INSTALLED_APPS=['django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','portal.accounts']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','django.contrib.sessions.middleware.SessionMiddleware',
            'django.middleware.common.CommonMiddleware','django.middleware.csrf.CsrfViewMiddleware',
            'django.contrib.auth.middleware.AuthenticationMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF='portal.urls'
WSGI_APPLICATION='portal.wsgi.application'
url=os.environ.get('DATABASE_URL','')
if url:
    u=urlsplit(url)
    if u.scheme not in ('postgres','postgresql'):raise RuntimeError('DATABASE_URL must use PostgreSQL')
    DATABASES={'default':{'ENGINE':'django.db.backends.postgresql','NAME':unquote(u.path.lstrip('/')),
        'USER':unquote(u.username or ''),'PASSWORD':unquote(u.password or ''),'HOST':u.hostname,'PORT':u.port or 5432,
        'CONN_MAX_AGE':60}}
else:
    if PRODUCTION:raise RuntimeError('Production requires PostgreSQL DATABASE_URL')
    DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':DATA/'portal.sqlite3','OPTIONS':{'timeout':60,'transaction_mode':'IMMEDIATE'}}}
TIME_ZONE='Asia/Shanghai'
USE_TZ=True
LANGUAGE_CODE='zh-hans'
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE='Lax'
SESSION_COOKIE_SECURE=PRODUCTION
CSRF_COOKIE_SECURE=PRODUCTION
SESSION_COOKIE_AGE=7*24*3600
SECURE_SSL_REDIRECT=PRODUCTION
SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https') if PRODUCTION else None
SECURE_HSTS_SECONDS=31536000 if PRODUCTION else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS=PRODUCTION
# Browser preload submission is not applicable to a bare IP address.
SILENCED_SYSTEM_CHECKS=[]
try:
    ipaddress.ip_address(os.environ.get('YSP_HOST',''))
    SILENCED_SYSTEM_CHECKS=['security.W021']
except ValueError:pass
X_FRAME_OPTIONS='DENY'
DATA_UPLOAD_MAX_MEMORY_SIZE=64*1024
FILE_UPLOAD_MAX_MEMORY_SIZE=0
