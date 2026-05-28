"""
Simple traffic generator. Produces a realistic mix of requests so the
dashboard has data and metrics move.

Usage:
    python generate_traffic.py                 # localhost, 60s
    python generate_traffic.py http://HOST:5000 120
"""
import json
import random
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:5000"
DURATION = int(sys.argv[2]) if len(sys.argv) > 2 else 60


def post(path, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


def get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:  # noqa: BLE001
        return 0, None


def main():
    end = time.time() + DURATION
    created, errors, total = 0, 0, 0
    last_order_id = None

    while time.time() < end:
        roll = random.random()
        if roll < 0.55:  # create an order
            status, body = post("/orders", {
                "amount": round(random.uniform(5, 500), 2),
                "items": random.randint(1, 6),
                "user_id": f"user-{random.randint(1, 50)}",
            })
            if body and body.get("order_id"):
                last_order_id = body["order_id"]
            created += status == 201
        elif roll < 0.75 and last_order_id:  # fetch a known order
            get(f"/orders/{last_order_id}")
        elif roll < 0.85:  # fetch a random (often 404) order
            get(f"/orders/ord-{random.randint(0, 99999):05x}")
        else:  # health check
            get("/health")

        total += 1
        errors += status == 500 if 'status' in dir() else 0
        time.sleep(random.uniform(0.05, 0.25))

    print(f"sent={total} orders_created={created} duration={DURATION}s")


if __name__ == "__main__":
    main()
