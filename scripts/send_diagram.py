#!/usr/bin/env python3
"""
Send workflow diagrams via AMQP.

Usage:
    pip install pika
    python scripts/send_diagram.py                  # send all from diagrams/mock_workflows/
    python scripts/send_diagram.py path/to/one.json  # send a single diagram
"""

import json
import os
import sys
from pathlib import Path

import pika

AMQP_HOST = os.environ.get("AMQP_HOST", "localhost")
AMQP_PORT = int(os.environ.get("AMQP_PORT", "5672"))
AMQP_USER = os.environ.get("AMQP_USER", "guest")
AMQP_PASS = os.environ.get("AMQP_PASS", "guest")
AMQP_EXCHANGE = "@RECEIVE@"
AMQP_EXCHANGE_KIND = "fanout"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOCK_WORKFLOWS_DIR = PROJECT_ROOT / "diagrams" / "mock_workflows"


def send_diagram(channel, diagram_path: Path):
    with open(diagram_path) as f:
        diagram = json.load(f)

    message = {
        "id": f"test-schedule-{diagram_path.stem}",
        "type": "Schedule",
        "payload": diagram,
    }

    body = json.dumps(message)
    channel.basic_publish(exchange=AMQP_EXCHANGE, routing_key="", body=body)
    print(f"Sent {diagram_path.name} ({len(body)} bytes)")


def main():
    if len(sys.argv) > 1:
        paths = [Path(sys.argv[1])]
    else:
        if not MOCK_WORKFLOWS_DIR.is_dir():
            print(f"Error: {MOCK_WORKFLOWS_DIR} not found")
            sys.exit(1)
        paths = sorted(MOCK_WORKFLOWS_DIR.glob("*.json"))
        if not paths:
            print(f"No .json files in {MOCK_WORKFLOWS_DIR}")
            sys.exit(1)
        print(f"Found {len(paths)} workflow(s) in {MOCK_WORKFLOWS_DIR.name}/\n")

    credentials = pika.PlainCredentials(AMQP_USER, AMQP_PASS)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=AMQP_HOST, port=AMQP_PORT, credentials=credentials)
    )
    channel = connection.channel()
    channel.exchange_declare(exchange=AMQP_EXCHANGE, exchange_type=AMQP_EXCHANGE_KIND, durable=True)

    for path in paths:
        send_diagram(channel, path)

    connection.close()
    print(f"\nDone — sent {len(paths)} diagram(s)")


if __name__ == "__main__":
    main()
