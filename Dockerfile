# Roblox Sales Analytics — optional container image.
# Build:  docker build -t better-ugc-analytics-tool .
# Run:    see docker-compose.yml (recommended) or README → Docker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    RSA_CONFIG=/config/config.ini \
    RSA_DB_PATH=/data/sales.db \
    RSA_COOKIE_FILE=/run/secrets/roblox_cookie

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY roblox_sales ./roblox_sales

RUN useradd --system --uid 10001 --home-dir /app rsa \
 && mkdir -p /data /config && chown rsa /data
USER rsa

# Inside a container the app must listen on all interfaces of the CONTAINER, which is why the
# image requires RSA_PASSWORD_HASH. docker-compose.yml publishes the port on 127.0.0.1 only.
ENV RSA_HOST=0.0.0.0
EXPOSE 8930
VOLUME ["/data"]
CMD ["python", "-m", "roblox_sales", "serve"]
