FROM python:3.12-slim
WORKDIR /service
RUN groupadd -r retail && useradd -r -g retail retail
COPY pyproject.toml ./
COPY requirements.lock ./
COPY app ./app
COPY connectors ./connectors
COPY docs/api.md ./docs/api.md
COPY static ./static
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir -r requirements.lock
# Connector packages installed from the admin page live on a volume shared by the API and worker.
RUN mkdir -p /data/connectors /data/downloads && chown -R retail:retail /data
ENV INSTALLED_CONNECTORS_DIR=/data/connectors
# The published Android app (served at /download) lives on a volume, so an update needs no new image.
ENV APP_DOWNLOADS_DIR=/data/downloads
USER retail
ENV PYTHONUNBUFFERED=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
