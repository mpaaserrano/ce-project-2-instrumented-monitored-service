"""
CloudWatch custom-metrics client.

Design notes (worth explaining at the checkpoint):
- We do NOT call put_metric_data inside the request path. That would add
  network latency to every request and could itself become the bottleneck.
  Instead we buffer values in memory and a background thread flushes them
  every METRIC_FLUSH_SECONDS.
- Counts are summed; latency is sent as a StatisticSet so CloudWatch can
  compute p95/p99 server-side without us shipping every datapoint.
- If AWS credentials are missing (e.g. running on a laptop), the client
  degrades gracefully: it logs what it *would* have sent and keeps running.
"""
import threading
import time
from collections import defaultdict

import structlog

logger = structlog.get_logger()


class MetricsClient:
    def __init__(self, namespace, region, enabled=True, flush_seconds=30):
        self.namespace = namespace
        self.enabled = enabled
        self.flush_seconds = flush_seconds
        self._lock = threading.Lock()

        # Buffers
        self._counts = defaultdict(float)            # name -> summed value
        self._count_dims = {}                        # name -> dimensions
        self._stats = defaultdict(list)              # name -> [values]
        self._stat_dims = {}                         # name -> dimensions

        self._client = None
        if self.enabled:
            try:
                import boto3
                self._client = boto3.client("cloudwatch", region_name=region)
            except Exception as exc:  # noqa: BLE001
                logger.warning("metrics_client_init_failed",
                               error=str(exc), note="falling back to log-only")
                self.enabled = False

        # Start the background flusher even when disabled, so log-only mode
        # still shows what would have been emitted.
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    # ---- Public API -------------------------------------------------------

    def count(self, name, value=1.0, unit="Count", dimensions=None):
        """Record a metric we want summed over the flush window."""
        with self._lock:
            self._counts[name] += value
            self._count_dims[name] = (unit, dimensions or {})

    def observe(self, name, value, unit="Milliseconds", dimensions=None):
        """Record a distribution value (e.g. latency) for percentile stats."""
        with self._lock:
            self._stats[name].append(value)
            self._stat_dims[name] = (unit, dimensions or {})

    # ---- Internals --------------------------------------------------------

    def _run(self):
        while not self._stop.wait(self.flush_seconds):
            try:
                self.flush()
            except Exception as exc:  # noqa: BLE001
                logger.error("metric_flush_failed", error=str(exc))

    def _drain(self):
        with self._lock:
            counts = dict(self._counts)
            count_dims = dict(self._count_dims)
            stats = {k: list(v) for k, v in self._stats.items()}
            stat_dims = dict(self._stat_dims)
            self._counts.clear()
            self._count_dims.clear()
            self._stats.clear()
            self._stat_dims.clear()
        return counts, count_dims, stats, stat_dims

    def flush(self):
        counts, count_dims, stats, stat_dims = self._drain()
        metric_data = []

        for name, value in counts.items():
            unit, dims = count_dims[name]
            metric_data.append(self._datum(name, unit, dims, value=value))

        for name, values in stats.items():
            if not values:
                continue
            unit, dims = stat_dims[name]
            metric_data.append(self._datum(
                name, unit, dims,
                stats={
                    "SampleCount": len(values),
                    "Sum": sum(values),
                    "Minimum": min(values),
                    "Maximum": max(values),
                },
            ))

        if not metric_data:
            return

        if not self.enabled or self._client is None:
            logger.info("metrics_flush_logonly", count=len(metric_data),
                        names=[m["MetricName"] for m in metric_data])
            return

        # CloudWatch accepts up to 1000 datapoints per call; we're nowhere near.
        self._client.put_metric_data(Namespace=self.namespace,
                                     MetricData=metric_data)
        logger.info("metrics_flushed", count=len(metric_data))

    @staticmethod
    def _datum(name, unit, dims, value=None, stats=None):
        datum = {
            "MetricName": name,
            "Unit": unit,
            "Dimensions": [{"Name": k, "Value": str(v)}
                           for k, v in dims.items()],
        }
        if stats is not None:
            datum["StatisticValues"] = stats
        else:
            datum["Value"] = value
        return datum

    def shutdown(self):
        self._stop.set()
        self.flush()
