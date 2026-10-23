#!/usr/bin/env python3
"""
Send one template from diagrams/marvel/ via AMQP.

The diagram it sends runs the template as its one section, the way the schedule adapter
names a template. So the orchestrator adds the template and fills ${section.experiment_id}
from --experiment-id. The run ends with the template's ok, failed or precedence output.

Usage:
    pip install pika
    python scripts/send_template.py --list
    python scripts/send_template.py tga_close_furnace
    python scripts/send_template.py dsc_process --experiment-id EXP-1

Environment:
    AMQP_HOST  (default: localhost)
    AMQP_PORT  (default: 5672)
    AMQP_USER  (default: guest)
    AMQP_PASS  (default: guest)
"""

import argparse
import json
import os
from pathlib import Path

AMQP_HOST = os.environ.get("AMQP_HOST", "localhost")
AMQP_PORT = int(os.environ.get("AMQP_PORT", "5672"))
AMQP_USER = os.environ.get("AMQP_USER", "guest")
AMQP_PASS = os.environ.get("AMQP_PASS", "guest")
AMQP_EXCHANGE = "@RECEIVE@"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PROJECT_ROOT / "diagrams" / "marvel"
SECTION = "step"
EXPERIMENT_ID = "${section.experiment_id}"

# `resource` always says ok, even after a failure. Only `precedence` may end the run.
OUTPUT_TARGETS = {
    "ok": {"builtin": "terminate"},
    "failed": {"builtin": "terminate"},
    "precedence": {"builtin": "terminate"},
    "resource": {"builtin": "dispose"},
}


def discover_templates():
    templates = {}
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        templates.update(json.loads(path.read_text()).get("templates", {}))
    return templates


def needs_experiment_id(template):
    return EXPERIMENT_ID in json.dumps(template)


def wrap(name, template, experiment_id=None):
    """A diagram that runs template `name` as its one section."""
    section = {
        "type": "section",
        "template": name,
        "connect": {out: OUTPUT_TARGETS[out] for out in template["outputs"]},
    }
    if experiment_id:
        section["experiment_id"] = experiment_id
    return {
        "version": "0.1.0",
        "start": "run_started",
        "ops": {
            # Same first step as the schedule adapter's diagrams, so check_input sees an ok status.
            "run_started": {
                "type": "transform",
                "cel": "{'status': 'ok'}",
                "next": {SECTION: "start"},
                "on_error": {"builtin": "dispose"},
            },
            SECTION: section,
        },
    }


def main():
    templates = discover_templates()

    parser = argparse.ArgumentParser(description="Send one template via AMQP.")
    parser.add_argument("template", nargs="?", help="Template name (use --list to see them)")
    parser.add_argument("--list", action="store_true", help="List templates and exit")
    parser.add_argument("--experiment-id", help="Fills ${section.experiment_id}")
    parser.add_argument("--task-id", help="Override the task ID (default: test-<template>)")
    args = parser.parse_args()

    if args.list:
        print("\n".join(sorted(templates)))
        return
    if not args.template:
        parser.error("give a template name, or use --list")
    if args.template not in templates:
        parser.error(f"unknown template '{args.template}'. Run with --list to see them.")
    template = templates[args.template]
    if needs_experiment_id(template) and not args.experiment_id:
        parser.error(f"{args.template} needs --experiment-id")

    task_id = args.task_id or f"test-{args.template}"
    message = {
        "id": task_id,
        "type": "Schedule",
        "payload": wrap(args.template, template, args.experiment_id),
    }

    import pika

    credentials = pika.PlainCredentials(AMQP_USER, AMQP_PASS)
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(host=AMQP_HOST, port=AMQP_PORT, credentials=credentials)
    )
    channel = connection.channel()
    channel.exchange_declare(exchange=AMQP_EXCHANGE, exchange_type="fanout", durable=True)

    body = json.dumps(message)
    channel.basic_publish(exchange=AMQP_EXCHANGE, routing_key="", body=body)
    print(f"Sent {args.template} as task '{task_id}' ({len(body)} bytes)")

    connection.close()


if __name__ == "__main__":
    main()
