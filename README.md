# Instrumented & Monitored Order Service

A deliberately small cloud web service built to demonstrate **production-grade observability**: structured logging, custom metrics, dashboards, alerting, and incident response on AWS CloudWatch.

The application itself is intentionally simple — the point is not feature complexity, it's that the service is **observable**: you can see what it's doing, measure it, alert on it, and diagnose it when it breaks.

---

## What It Does

A minimal order-processing API with full instrumentation:

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Liveness / hello |
| `/health` | GET | Health check (returns 503 when degraded) |
| `/orders` | POST | Create an order (business endpoint) |
| `/orders/<id>` | GET | Fetch an order |
| `/metrics-info` | GET | Human-readable list of emitted metrics |
| `/admin/inject/<mode>` | POST | Inject a failure (incident exercise) |
| `/admin/reset` | POST | Clear all injected failures |

---

## Observability Features

- **Structured JSON logging** via `structlog` — one log line per request, plus business events
- **Correlation IDs** on every request (propagated from `X-Correlation-ID` header or generated), returned in the response header for end-to-end tracing
- **Per-request latency** measured and logged
- **Proper log levels** — `INFO` (normal), `WARN` (client errors / slow), `ERROR` (5xx / exceptions)
- **7 custom CloudWatch metrics** (technical + business), buffered and flushed by a background thread so publishing never adds latency to the request path
- **Failure injection** so dashboards and alarms have something real to catch

### Metrics Emitted (`OrderService/Production`)

| Metric | Golden Signal | Type |
|--------|---------------|------|
| `RequestCount` | Rate | Technical |
| `APILatency` (p95/p99 via StatisticSet) | Duration | Technical |
| `ErrorCount` | Errors | Technical |
| `HealthCheckStatus` | Saturation proxy | Technical |
| `OrdersCreated` | — | Business |
| `OrderValue` | — | Business |
| `ItemsPerOrder` | — | Business |

Host metrics (CPU, memory, disk) are collected separately by the CloudWatch agent under `OrderService/Host`.

---

## Architecture

```
Client ──HTTP──> EC2 (gunicorn + Flask)
                   │
                   ├── structlog ──> application.log ──┐
                   │                                    │  CloudWatch Agent
                   └── boto3 put_metric_data ──┐        │
                                               ▼        ▼
                                      CloudWatch Metrics   CloudWatch Logs
                                               │                │
                                               ├── Dashboards ───┤
                                               └── Alarms ──> SNS ──> Email
```

---

## Prerequisites

- An EC2 instance (Ubuntu 22.04+, `t2.micro` is fine)
- An **IAM role** attached to the instance with:
  - `CloudWatchAgentServerPolicy` (logs + host metrics)
  - `cloudwatch:PutMetricData` permission (custom app metrics)
- Security group allowing inbound `22` (SSH) and `5000` (app) from your IP

---

## Deploy

```bash
# On the EC2 instance, from the project root
cd /home/ubuntu/app
chmod +x deploy.sh
./deploy.sh
```

The script installs dependencies, installs and starts the CloudWatch agent with `config/cloudwatch-agent-config.json`, and launches the app with gunicorn on port 5000.

Verify the agent:
```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status
```

---

## Configuration

All settings are environment-driven (see `app/config.py`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `APP_PORT` | `5000` | Listen port |
| `APP_LOG_FILE` | `/home/ubuntu/app/application.log` | Must match the agent config |
| `METRICS_ENABLED` | `true` | Set `false` to run locally without AWS |
| `METRIC_NAMESPACE` | `OrderService/Production` | CloudWatch namespace |
| `AWS_REGION` | `us-east-1` | Must match your instance region |
| `METRIC_FLUSH_SECONDS` | `30` | How often buffered metrics are sent |
| `INJECTION_ENABLED` | `true` | Enables the failure-injection endpoints |

---

## Run Locally (no AWS needed)

```bash
cd app
pip install -r requirements.txt
METRICS_ENABLED=false python3 server.py
# in another terminal:
python3 generate_traffic.py http://localhost:5000 60
```

In local mode, metrics are logged ("what we would have sent") instead of published.

---

## Test

```bash
# Health
curl http://<EC2_IP>:5000/health

# Create an order
curl -X POST http://<EC2_IP>:5000/orders \
  -H "Content-Type: application/json" \
  -d '{"amount":49.99,"items":3,"user_id":"u1"}'

# Generate 2 minutes of mixed traffic
python3 generate_traffic.py http://<EC2_IP>:5000 120
```

Then confirm in the AWS console:
- **Logs:** CloudWatch → Log groups → `/order-service/application`
- **Metrics:** CloudWatch → Metrics → Custom namespaces → `OrderService/Production`
- **Logs Insights** sample query:
  ```
  fields @timestamp, level, event, correlation_id, latency_ms
  | filter event = "order_created"
  | sort @timestamp desc
  ```

---

## Failure Injection (Incident Response Exercise)

Trigger controlled failures, then use the dashboard and alarms to diagnose them:

```bash
# Error spike (~50% of orders fail with 500)
curl -X POST "http://<IP>:5000/admin/inject/error?on=true"

# Latency spike (0.8–2.0s added per request)
curl -X POST "http://<IP>:5000/admin/inject/latency?on=true"

# CPU saturation (~10s burn)
curl -X POST "http://<IP>:5000/admin/inject/cpu"

# Memory leak (~50MB per call)
curl -X POST "http://<IP>:5000/admin/inject/memory"

# Clear everything
curl -X POST "http://<IP>:5000/admin/reset"
```

---

## Project Structure

```
.
├── README.md
├── app/
│   ├── server.py                  # Flask app + instrumentation + injection
│   ├── metrics.py                 # Buffered CloudWatch metrics client
│   ├── config.py                  # Env-driven configuration
│   ├── generate_traffic.py        # Load generator
│   ├── requirements.txt
│   └── deploy.sh                  # EC2 + agent setup
└── config/
    └── cloudwatch-agent-config.json
```

---

## Known Limitations / Production-Readiness Gaps

- The `/admin/inject/*` endpoints are **unauthenticated** — fine for a controlled lab, but they must be removed or locked down before any real deployment.
- The order store is **in-memory** — orders are lost on restart and not shared across gunicorn workers.
- Port 5000 should sit behind a load balancer with TLS in a real setup, not be exposed directly.

These are intentional teaching points for the "improvements" discussion.
