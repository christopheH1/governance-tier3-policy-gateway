"""Telemetry for the policy gateway, using the OpenTelemetry SDK.

Telemetry fails open. Spans and metrics are exported in the background, and a
collector that is slow or down never delays, changes, or blocks a request. That
is deliberate, and it is the opposite of the audit ledger, which fails closed.

What is sent: the outcome of each request, its reasons, which agent, role, tool,
or model was involved, and how long each stage took. Tool parameters and prompt
content are never sent.

Set OTEL_EXPORTER_OTLP_ENDPOINT to switch it on. Without it, everything here is a no-op.
"""
import logging
import os
from contextlib import contextmanager
from typing import Optional

ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
enabled = bool(ENDPOINT)

if enabled:
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode

    # A collector that is down would otherwise fill the log with one line per failed batch.
    for noisy in ("opentelemetry.exporter", "opentelemetry.sdk", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    _resource = Resource.create({"service.name": "policy-gateway",
                                 "governance.tier": "tier3", "governance.profile": "profile3"})
    _tracer_provider = TracerProvider(resource=_resource)
    _tracer_provider.add_span_processor(BatchSpanProcessor(
        OTLPSpanExporter(endpoint=f"{ENDPOINT}/v1/traces", timeout=3),
        schedule_delay_millis=2000, max_queue_size=4096))
    trace.set_tracer_provider(_tracer_provider)
    _meter_provider = MeterProvider(resource=_resource, metric_readers=[PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{ENDPOINT}/v1/metrics", timeout=3), export_interval_millis=10000)])
    metrics.set_meter_provider(_meter_provider)

    _tracer = trace.get_tracer("policy-gateway")
    _meter = metrics.get_meter("policy-gateway")
    _decisions = _meter.create_counter("governance.decisions", unit="1",
                                       description="Requests decided by the policy gateway")
    _duration = _meter.create_histogram("governance.request.duration", unit="ms",
                                        description="Time a request spent in the policy gateway")


class RequestSpan:
    """Handle for the span covering one request."""

    def __init__(self, span=None):
        self._span = span
        self.trace_id: Optional[str] = None
        if span is not None and span.get_span_context().is_valid:
            self.trace_id = format(span.get_span_context().trace_id, "032x")

    def finish(self, path: str, status_code: int, body: dict, duration_ms: float) -> None:
        if self._span is None:
            return
        outcome = str(body.get("outcome", "unknown"))
        mode = str(body.get("mode", ""))
        self._span.set_attribute("governance.path", path)
        self._span.set_attribute("governance.outcome", outcome)
        self._span.set_attribute("http.response.status_code", status_code)
        if mode:
            self._span.set_attribute("governance.mode", mode)
        if body.get("reasons"):
            self._span.set_attribute("governance.reasons", [str(reason) for reason in body["reasons"]])
        if status_code >= 500:      # a dependency failed. A policy denial is an outcome, not an error.
            self._span.set_status(Status(StatusCode.ERROR))
        labels = {"path": path, "outcome": outcome, "mode": mode}
        _decisions.add(1, labels)
        _duration.record(duration_ms, {"path": path, "outcome": outcome})


@contextmanager
def request_span(name: str):
    if not enabled:
        yield RequestSpan()
        return
    with _tracer.start_as_current_span(name) as span:
        yield RequestSpan(span)


@contextmanager
def stage_span(name: str):
    if not enabled:
        yield
        return
    with _tracer.start_as_current_span(f"stage.{name}"):
        yield


def annotate(**attributes) -> None:
    """Attach facts to the span of the current request. Values of None are skipped."""
    if not enabled:
        return
    span = trace.get_current_span()
    for key, value in attributes.items():
        if value is not None:
            span.set_attribute(key.replace("_", ".", 1), value)
