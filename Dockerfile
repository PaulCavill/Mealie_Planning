FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py env_loader.py ./
COPY mealie_client ./mealie_client
COPY core ./core
COPY web ./web

# 568:568 is the built-in `apps` user and group on TrueNAS. The app needs no
# home directory and nothing but /data to be writable, so it also runs as any
# other uid:gid (docker-compose `user:`) as long as that user can write /data.
RUN groupadd --gid 568 app \
    && useradd --uid 568 --gid 568 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown 568:568 /data
ENV HOME=/tmp
USER 568:568

VOLUME /data
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "web.app:app", "--host", "0.0.0.0", "--port", "8080"]
