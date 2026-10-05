# Tessera identity service, built from the pinned ARTO clone in vendor/ARTO.
# Build context is the project root.
FROM python:3.11-slim-bookworm@sha256:0bee7276f83efd4a1ee05bbbf4281d95ed28e079220a9457f25a93e3f1e3c31b
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/tessera
COPY docker/requirements-tessera.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY vendor/ARTO/tessera/ /srv/tessera/
RUN useradd --system --uid 10001 arto \
 && mkdir -p data logs shared_state \
 && chown -R arto /srv/tessera
USER arto
EXPOSE 8001
CMD ["python", "api_server.py"]
