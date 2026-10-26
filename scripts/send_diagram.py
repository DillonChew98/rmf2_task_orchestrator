#!/usr/bin/env python3
"""
Send a workflow diagram via AMQP.

Usage:
    pip install pika
    python scripts/send_diagram.py <workflow>
    python scripts/send_diagram.py --list

Examples:
    python scripts/send_diagram.py storage_loading
    python scripts/send_diagram.py tga_mock_workflow
    python scripts/send_diagram.py storage_unloading_no_trayid
    python scripts/send_diagram.py storage_loading --task-id my-custom-id

Environment:
    AMQP_HOST  (default: localhost)
    AMQP_PORT  (default: 5672)
    AMQP_USER  (default: guest)
    AMQP_PASS  (default: guest)
"""

import argparse
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

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DIAGRAM_DIRS = [
    PROJECT_ROOT / "diagrams" / "mock_workflows",
]


def discover_workflows():
    workflows = {}
    for d in DIAGRAM_DIRS:
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.json")):
            workflows[f.stem] = f
    return workflows


def main():
    workflows = discover_workflows()

    parser = argparse.ArgumentParser(
        description="Send a workflow diagram via AMQP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Available workflows:\n  " + "\n  ".join(sorted(workflows.keys())),
    )
    parser.add_argument(
        "workflow",
        nargs="?",
        help="Workflow name (use --list to see available)",
    )
    parser.add_argument(
        "--list", action="store_true", help="List available workflows and exit"
    )
    parser.add_argument(
        "--task-id", help="Override the task ID (default: test-<workflow>)"
    )
    args = parser.parse_args()

    if args.list:
        print("Available workflows:")
        for name, path in sorted(workflows.items()):
            rel = path.relative_to(PROJECT_ROOT)
            print(f"  {name:<45s} {rel}")
        return

    if not args.workflow:
        parser.print_help()
        sys.exit(1)

    if args.workflow not in workflows:
        print(f"Error: unknown workflow '{args.workflow}'")
        print(f"Run with --list to see available workflows")
        sys.exit(1)

    diagram_path = workflows[args.workflow]
    task_id = args.task_id or f"test-{args.workflow}"

    with open(diagram_path) as f:
        diagram = json.load(f)

    message = {
        "id": task_id,
        "type": "Schedule",
        "payload": diagram,
    }

    credentials = pika.PlainCredentials(AMQP_USER, AMQP_PASS)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=AMQP_HOST, port=AMQP_PORT, credentials=credentials)
    )
    channel = connection.channel()
    channel.exchange_declare(exchange=AMQP_EXCHANGE, exchange_type="fanout", durable=True)

    body = json.dumps(message)
    channel.basic_publish(exchange=AMQP_EXCHANGE, routing_key="", body=body)
    print(f"Sent {diagram_path.name} as task '{task_id}' ({len(body)} bytes)")

    connection.close()


if __name__ == "__main__":
    main()
