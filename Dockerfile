FROM python:3.12-slim

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=$BUILD_VERSION

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /app/data

VOLUME ["/app/data"]

# BOT_TOKEN передаётся при запуске: docker run -e BOT_TOKEN=xxx
CMD ["python", "-m", "app.main"]
