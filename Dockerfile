FROM python:3.12-alpine

WORKDIR /app
COPY server.py services.json ./
COPY static ./static

RUN mkdir -p /data
ENV DATA_DIR=/data \
    PORT=8080 \
    CHECK_INTERVAL=60 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
CMD ["python", "server.py"]
