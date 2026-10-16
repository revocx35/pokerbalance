# syntax=docker/dockerfile:1
FROM python:3.12-slim-trixie

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    POKER_DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin poker \
 && mkdir -p /data && chown poker:poker /data

COPY app ./app

USER poker
VOLUME ["/data"]
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,ssl,urllib.request as u; tls=os.environ.get('POKER_TLS','self-signed')!='off'; ctx=ssl._create_unverified_context(); u.urlopen(('https' if tls else 'http')+'://127.0.0.1:'+os.environ.get('POKER_PORT','8443')+'/api/state', context=ctx if tls else None, timeout=4)" || exit 1
CMD ["python", "-m", "app"]
