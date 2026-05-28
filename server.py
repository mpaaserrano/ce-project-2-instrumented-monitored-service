"""
Order Service — a deliberately small web service built to be *observable*.

Endpoints
  GET  /                      -> hello / liveness
  GET  /health                -> health check (used by ALB / agent)
  POST /orders                -> create an order   (business endpoint)
  GET  /orders/<order_id>     -> fetch an order
  GET  /metrics-info          -> human-readable list of what we emit
  POST /admin/inject/<mode>   -> incident-response failure injection

Observability features
  * Structured JSON logs (structlog) -> file -> CloudWatch agent -> Logs
  * Correlation ID on every request (generated or propagated from header)
  * Per-request latency measurement
  * 6 custom CloudWatch metrics (technical + business)
  * Proper ERROR / WARN / INFO log levels
  * Failure injection so the dashboard/alarms have something to catch

Run locally without AWS:  METRICS_ENABLED=false python server.py
"""
import logging
import os
import random
import threading
import time
import uuid

import structlog
from flask import Flask, g, jsonify, request

from config import Config
from metrics import MetricsClient

# --------------------------------------------------------------------------
# Logging: stdlib handler writes raw JSON lines to the file the agent tails.
# --------------------------------------------------------------------------
os.makedirs(os.path.dirname(Config.LOG_FILE), exist_ok=True)
logging.basicConfig(
    filename=Config.LOG_FILE,
    level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
    format="%(message)s",
)
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
log = structlog.get_logger()

# --------------------------------------------------------------------------
# App + metrics client
# --------------------------------------------------------------------------
app = Flask(__name__)
metrics = MetricsClient(
    namespace=Config.METRIC_NAMESPACE,
    region=Config.AWS_REGION,
    enabled=Config.METRICS_ENABLED,
    flush_seconds=Config.METRIC_FLUSH_SECONDS,
)

# Toggles for the incident-response exercise. Flipped via /admin/inject/<mode>.
INJECT = {"error": False, "latency": False, "cpu": False, "memory": False}
_memory_hog = []  # holds allocations for the simulated memory leak

# A trivial in-memory "database" so GET /orders/<id> can succeed or 404.
ORDERS = {}


# --------------------------------------------------------------------------
# Request lifecycle hooks — this is where most observability lives.
# --------------------------------------------------------------------------
@app.before_request
def start_request():
    g.start = time.perf_counter()
    # Propagate an incoming correlation ID if present (distributed tracing
    # across services), otherwise mint a fresh one.
    g.correlation_id = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))

    # Simulated latency injection — sleeps before the handler runs.
    if INJECT["latency"]:
        time.sleep(random.uniform(0.8, 2.0))


@app.after_request
def finish_request(response):
    elapsed_ms = (time.perf_counter() - g.get("start", time.perf_counter())) * 1000
    cid = g.get("correlation_id", "unknown")
    endpoint = request.path
    status = response.status_code

    # One structured log line per request (the backbone of Logs Insights).
    level = "error" if status >= 500 else ("warning" if elapsed_ms > 1000 else "info")
    getattr(log, level)(
        "request_completed",
        correlation_id=cid,
        method=request.method,
        path=endpoint,
        status=status,
        latency_ms=round(elapsed_ms, 2),
        ip=request.remote_addr,
    )

    # ---- Metrics -------------------------------------------------------
    # 1. RequestCount  (traffic / Golden Signal: rate)
    metrics.count("RequestCount", 1, dimensions={"endpoint": endpoint})
    # 2. APILatency    (Golden Signal: duration) -> CW computes p95/p99
    metrics.observe("APILatency", elapsed_ms, dimensions={"endpoint": endpoint})
    # 3. ErrorCount    (Golden Signal: errors)
    if status >= 500:
        metrics.count("ErrorCount", 1, dimensions={"endpoint": endpoint})

    response.headers["X-Correlation-ID"] = cid
    return response


@app.errorhandler(Exception)
def handle_unexpected(exc):
    cid = g.get("correlation_id", "unknown")
    log.error("unhandled_exception", correlation_id=cid,
              path=request.path, error=str(exc), error_type=type(exc).__name__)
    # after_request still runs and records the 500 in metrics.
    return jsonify({"error": "internal_server_error", "correlation_id": cid}), 500


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return jsonify({"message": "Order Service up",
                    "correlation_id": g.correlation_id})


@app.route("/health")
def health():
    # Report unhealthy if a failure is being injected, so alarms can fire.
    healthy = not (INJECT["cpu"] or INJECT["memory"])
    metrics.count("HealthCheckStatus", 1.0 if healthy else 0.0, unit="None")
    body = {"status": "healthy" if healthy else "degraded",
            "injections": {k: v for k, v in INJECT.items() if v}}
    return jsonify(body), (200 if healthy else 503)


@app.route("/orders", methods=["POST"])
def create_order():
    cid = g.correlation_id

    # Simulated error injection — fail a fraction of orders.
    if INJECT["error"] and random.random() < 0.5:
        log.error("order_failed", correlation_id=cid, reason="injected_failure")
        return jsonify({"error": "order_processing_failed",
                        "correlation_id": cid}), 500

    data = request.get_json(silent=True) or {}
    amount = float(data.get("amount", 0) or 0)
    items = int(data.get("items", 0) or 0)
    user_id = data.get("user_id", "anonymous")

    # Validation -> WARN (a client error, not a server fault).
    if amount <= 0 or items <= 0:
        log.warning("order_rejected", correlation_id=cid,
                    reason="invalid_payload", amount=amount, items=items)
        return jsonify({"error": "amount and items must be > 0",
                        "correlation_id": cid}), 400

    order_id = f"ord-{uuid.uuid4().hex[:8]}"
    ORDERS[order_id] = {"order_id": order_id, "amount": amount,
                        "items": items, "user_id": user_id}

    log.info("order_created", correlation_id=cid, order_id=order_id,
             amount=amount, items=items, user_id=user_id)

    # ---- Business metrics ---------------------------------------------
    # 4. OrdersCreated (business: throughput of the thing we actually sell)
    metrics.count("OrdersCreated", 1)
    # 5. OrderValue    (business: revenue per order, watch the average)
    metrics.observe("OrderValue", amount, unit="None")
    # 6. ItemsPerOrder (business: basket size)
    metrics.observe("ItemsPerOrder", items, unit="Count")

    return jsonify({"status": "created", "order_id": order_id,
                    "correlation_id": cid}), 201


@app.route("/orders/<order_id>")
def get_order(order_id):
    cid = g.correlation_id
    order = ORDERS.get(order_id)
    if not order:
        log.warning("order_not_found", correlation_id=cid, order_id=order_id)
        return jsonify({"error": "not_found", "correlation_id": cid}), 404
    log.info("order_retrieved", correlation_id=cid, order_id=order_id)
    return jsonify({**order, "correlation_id": cid})


@app.route("/metrics-info")
def metrics_info():
    return jsonify({
        "namespace": Config.METRIC_NAMESPACE,
        "metrics": {
            "RequestCount":     "traffic / request rate (Golden Signal: rate)",
            "APILatency":       "request duration ms, use p95/p99 (duration)",
            "ErrorCount":       "5xx responses (errors)",
            "HealthCheckStatus":"1 healthy / 0 degraded (saturation proxy)",
            "OrdersCreated":    "business: successful orders",
            "OrderValue":       "business: revenue per order",
            "ItemsPerOrder":    "business: basket size",
        },
    })


# --------------------------------------------------------------------------
# Failure injection — drives the Day 2/3 incident-response exercise.
#   POST /admin/inject/error?on=true
#   POST /admin/inject/latency?on=false
#   POST /admin/inject/cpu        (one-shot burn, ~10s)
#   POST /admin/inject/memory     (leak ~50MB per call)
# --------------------------------------------------------------------------
@app.route("/admin/inject/<mode>", methods=["POST"])
def inject(mode):
    if not Config.INJECTION_ENABLED:
        return jsonify({"error": "injection_disabled"}), 403

    cid = g.correlation_id

    if mode in ("error", "latency"):
        on = request.args.get("on", "true").lower() == "true"
        INJECT[mode] = on
        log.warning("failure_injected", correlation_id=cid, mode=mode, on=on)
        return jsonify({"injected": mode, "on": on})

    if mode == "cpu":
        INJECT["cpu"] = True
        log.warning("failure_injected", correlation_id=cid, mode="cpu")

        def burn():
            end = time.time() + 10
            while time.time() < end:
                _ = sum(i * i for i in range(10000))
            INJECT["cpu"] = False
            log.info("cpu_injection_ended", correlation_id=cid)

        threading.Thread(target=burn, daemon=True).start()
        return jsonify({"injected": "cpu", "duration_s": 10})

    if mode == "memory":
        INJECT["memory"] = True
        _memory_hog.append(bytearray(50 * 1024 * 1024))  # ~50 MB
        log.warning("failure_injected", correlation_id=cid, mode="memory",
                    total_chunks=len(_memory_hog))
        return jsonify({"injected": "memory", "chunks": len(_memory_hog)})

    return jsonify({"error": "unknown_mode",
                    "valid": list(INJECT.keys())}), 400


@app.route("/admin/reset", methods=["POST"])
def reset():
    for k in INJECT:
        INJECT[k] = False
    _memory_hog.clear()
    log.info("injections_reset", correlation_id=g.correlation_id)
    return jsonify({"status": "reset"})


if __name__ == "__main__":
    log.info("application_started", port=Config.PORT,
             metrics_enabled=Config.METRICS_ENABLED,
             namespace=Config.METRIC_NAMESPACE)
    app.run(host=Config.HOST, port=Config.PORT)
