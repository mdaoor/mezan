FROM node:22-slim AS web
WORKDIR /web
COPY web/package*.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY mizan ./mizan
COPY alembic.ini plugins.yaml ./
COPY migrations ./migrations
COPY plugins ./plugins
RUN pip install --no-cache-dir ".[postgres,redis]"
COPY --from=web /web/dist ./web/dist
ENV MIZAN_WEB_DIR=/app/web/dist MIZAN_DATA_DIR=/data
EXPOSE 8080
CMD ["mizan", "serve", "--port", "8080"]
