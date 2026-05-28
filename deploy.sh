#!/usr/bin/env bash
# Deploy the Order Service on a fresh Ubuntu EC2 instance.
# Prereq: instance has an IAM role with CloudWatchAgentServerPolicy attached.
set -euo pipefail

APP_DIR="/home/ubuntu/app"

echo "==> Installing system packages"
sudo apt-get update -y
sudo apt-get install -y python3-pip python3-venv wget

echo "==> Setting up the app"
mkdir -p "$APP_DIR"
cp -r ./* "$APP_DIR"/ 2>/dev/null || true
cd "$APP_DIR"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements.txt

echo "==> Installing the CloudWatch agent"
wget -q https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i -E amazon-cloudwatch-agent.deb

echo "==> Starting the CloudWatch agent with our config"
sudo cp ../config/cloudwatch-agent-config.json \
  /opt/aws/amazon-cloudwatch-agent/etc/cloudwatch-agent-config.json
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config -m ec2 \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/cloudwatch-agent-config.json -s

echo "==> Starting the app with gunicorn (4 workers) on :5000"
export METRICS_ENABLED=true
export AWS_REGION="${AWS_REGION:-us-east-1}"
nohup .venv/bin/gunicorn -w 4 -b 0.0.0.0:5000 server:app \
  > "$APP_DIR/gunicorn.log" 2>&1 &

echo "==> Done. Test with:  curl http://localhost:5000/health"
