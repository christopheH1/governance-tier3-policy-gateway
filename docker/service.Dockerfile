# Own services: policy gateway, provisioner, mock tools, model adapter, agent runtime, console.
# One image recipe, selected by APP_DIR and, where the packages differ, REQUIREMENTS.
FROM python:3.11-slim-bookworm@sha256:0bee7276f83efd4a1ee05bbbf4281d95ed28e079220a9457f25a93e3f1e3c31b
ARG APP_DIR
ARG REQUIREMENTS=requirements-services.txt
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
WORKDIR /srv/app
COPY docker/${REQUIREMENTS} /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && useradd --system --uid 10001 svc \
 && mkdir /keys && chown svc /keys
COPY ${APP_DIR}/ /srv/app/
USER svc
# One worker: the gateway runs a single hold-expiry sweeper.
CMD ["sh", "-c", "exec python -m uvicorn app:app --host 0.0.0.0 --port ${PORT} --workers 1 --no-server-header"]
