# Vestigia audit ledger, built from the pinned ARTO clone in vendor/ARTO.
# Build context is the project root.
FROM python:3.11-slim-bookworm@sha256:0bee7276f83efd4a1ee05bbbf4281d95ed28e079220a9457f25a93e3f1e3c31b
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv/vestigia
COPY docker/requirements-vestigia.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY vendor/ARTO/vestigia/ /srv/vestigia/
# The one change made to upstream code: the API rate limit becomes configurable.
# vendor/ARTO itself stays unmodified. See docker/patch_vestigia.py and versions.lock.
COPY docker/patch_vestigia.py /tmp/patch_vestigia.py
RUN python /tmp/patch_vestigia.py /srv/vestigia/api_server.py
RUN useradd --system --uid 10001 arto \
 && mkdir -p data backups logs \
 && chown -R arto /srv/vestigia
USER arto
EXPOSE 8002
CMD ["python", "api_server.py"]
