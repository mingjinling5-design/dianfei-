FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai \
    YSP_DATA_DIR=/var/lib/ysp YSP_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
WORKDIR /app
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home app \
    && mkdir /var/lib/ysp && chown app:app /var/lib/ysp
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt
COPY manage.py ./
COPY portal ./portal
COPY ysp ./ysp
COPY web ./web
COPY licenses ./licenses
COPY THIRD_PARTY_NOTICES.md ./
COPY deploy ./deploy
COPY compose.yaml .env.example requirements.txt Dockerfile .dockerignore ./
RUN python deploy/make_source.py
USER app
EXPOSE 8000
CMD ["gunicorn","portal.wsgi:application","--bind=0.0.0.0:8000","--workers=2","--threads=4","--timeout=120","--access-logfile=-"]
