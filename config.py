"""
Central configuration for the Order Service.

Everything is environment-driven so the same code runs locally (for development
and load testing) and on EC2 (where metrics publish to real CloudWatch).
"""
import os


class Config:
    # --- Server ---
    HOST = os.getenv("APP_HOST", "0.0.0.0")
    PORT = int(os.getenv("APP_PORT", "5000"))

    # --- Logging ---
    # The CloudWatch agent tails this file. Use an absolute path that the
    # agent config also points at.
    LOG_FILE = os.getenv("APP_LOG_FILE", "/home/ubuntu/app/application.log")
    LOG_LEVEL = os.getenv("APP_LOG_LEVEL", "INFO")

    # --- CloudWatch metrics ---
    # Set METRICS_ENABLED=false to run locally without AWS credentials.
    METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() == "true"
    METRIC_NAMESPACE = os.getenv("METRIC_NAMESPACE", "OrderService/Production")
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
    # How often the background thread flushes buffered metrics to CloudWatch.
    METRIC_FLUSH_SECONDS = int(os.getenv("METRIC_FLUSH_SECONDS", "30"))

    # --- Failure injection (for the incident-response exercise) ---
    # Guarded so this never accidentally ships to a real production system.
    INJECTION_ENABLED = os.getenv("INJECTION_ENABLED", "true").lower() == "true"
