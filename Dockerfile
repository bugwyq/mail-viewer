FROM python:3.12-slim

WORKDIR /app
COPY server.py /app/server.py

ENV APP_BIND=0.0.0.0
ENV APP_PORT=8787

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import json, urllib.request; print(json.load(urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3))['ok'])"

CMD ["python", "/app/server.py"]
