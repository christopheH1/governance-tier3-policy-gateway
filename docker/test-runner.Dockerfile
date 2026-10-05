# Test runner: exercises the services from inside the Docker networks.
FROM python:3.11-slim-bookworm@sha256:0bee7276f83efd4a1ee05bbbf4281d95ed28e079220a9457f25a93e3f1e3c31b
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY docker/requirements-test.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && useradd --system --uid 10001 runner \
 && mkdir /state && chown runner /state
USER runner
WORKDIR /tests
CMD ["python", "smoke_foundation.py"]
