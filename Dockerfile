# 摄像头回放台 — 容器镜像
#
# 只做两件事：把录像目录只读挂进来，把转码缓存写到可持久化的卷里。
# 镜像内自带 ffmpeg，不需要在宿主机装任何依赖。
FROM python:3.12-slim

LABEL org.opencontainers.image.title="xiaomi-camera-viewer" \
      org.opencontainers.image.description="把米家摄像头的碎片录像整合成可按时间轴回放的监控台" \
      org.opencontainers.image.source="https://github.com/szjmhxd/xiaomi-camera-viewer" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tini tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py /app/server.py
COPY web /app/web
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
 && mkdir -p /videos /cache

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CAM_ROOT=/videos \
    CAM_CACHE=/cache \
    CAM_PORT=8765 \
    CAM_CACHE_MAX_GB=20

VOLUME ["/cache"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request,sys; p=os.environ.get('CAM_PORT','8765'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/api/config',timeout=4).status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/docker-entrypoint.sh"]
