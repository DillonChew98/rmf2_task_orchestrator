"""Mock UR task server, port 50066: the robot-control PC's /task API.

Same routes, shapes and codes as marvel_mm's scripts/rest_api.py, for the
orchestrator's ur_perform_task template and the MiR template's
`mir_safe_to_move` check. Its arrival interlock asks the mock MiR where it is.

    python -m mock_servers.ur.server [--port 50066] [--task-seconds 8] [--no-interlock]
"""

from __future__ import annotations

import argparse
import logging

from ..harness.json_http import Request, bind, configure_logging, serve_until_stopped
from .arm import (
    ACTIONS,
    ALL_ITEMS,
    BUSY_STATES,
    TASK_ITEMS,
    TASKS,
    Arm,
    default_action_for,
    parse_items,
)
from .interlock import Interlock, MirLink

PORT = 50066
logger = logging.getLogger("mock_servers.ur")


def parse_status_message(message: str) -> tuple[str | None, str | None, str]:
    """(state, ros_task, controller) from "<STATE>[ task:<name>] (ctrl: <active|inactive>)"."""
    words = message.split(" ")
    state = words[0] or None
    ros_task = next((word[len("task:") :] for word in words if word.startswith("task:")), None)
    controller = "active" if "(ctrl: active)" in message else "inactive"
    return state, ros_task, controller


def home_fields(busy: bool) -> dict:
    """The arm leaves HOME while a task runs and is back there when it ends, ERROR too."""
    return {
        "arm_at_home": not busy,
        "mir_safe_to_move": not busy,
        "arm_home": {
            "max_dev_deg": 35.0 if busy else 0.0,
            "moving": busy,
            "joint_state_age_s": 0.02,
            "tolerance_deg": 1.0,
        },
    }


class UrApp:
    def __init__(self, arm: Arm, interlock: Interlock) -> None:
        self.arm = arm
        self.interlock = interlock

    def __call__(self, request: Request) -> tuple[int, object]:
        match request.method, request.path:
            case "POST", "/task/start":
                return self._start(request)
            case "GET", "/task/status":
                task = request.arg("task")
                return self._arm_status() if task is None else self._task_status(task)
            case "GET", "/tasks":
                return 200, {
                    "tasks": list(TASKS),
                    "actions": list(ACTIONS),
                    "default_action": {task: default_action_for(task) for task in TASKS},
                    "implemented_actions": list(ACTIONS),
                    "directions": {task: list(ACTIONS) for task in TASKS},
                    "items": dict(TASK_ITEMS),
                    "all_items_keyword": ALL_ITEMS,
                    "mir_arrival_check": self.interlock.info(),
                }
            case "GET", "/health":
                return 200, {"status": "ok"}
            case "POST", "/mock/fail-next":
                self.arm.fail_next()
                return 200, {"message": "the next task ends in ERROR"}
        return 404, {"detail": "Not Found"}

    def _start(self, request: Request) -> tuple[int, object]:
        body = {} if request.body is None else request.body
        if not isinstance(body, dict):
            return 422, {"detail": "the body must be a JSON object"}
        name = request.arg("task") if request.arg("task") is not None else body.get("task")
        action = (
            request.arg("action") if request.arg("action") is not None else body.get("action")
        ) or default_action_for(name)
        items = parse_items(request.query["item"] if "item" in request.query else body.get("item"))

        def answer(code: int, message: str, mir: dict | None = None) -> tuple[int, dict]:
            return code, {
                "success": code == 200,
                "message": message,
                "task": name,
                "action": action,
                "item": items,
                "ros_task": f"{action}_task",
                "mir": mir,
            }

        if not name:
            return answer(400, f"'task' is required. One of: {', '.join(TASKS)}")
        if name not in TASKS:
            return answer(404, f"unknown task '{name}'. Known tasks: {', '.join(TASKS)}")
        if action not in ACTIONS:
            return answer(400, f"unknown action '{action}'. Expected one of: {', '.join(ACTIONS)}")
        unknown = [item for item in items or [] if item != ALL_ITEMS and item not in TASK_ITEMS[name]]
        if unknown:
            return answer(
                400,
                f"'{name}' has no item {', '.join(unknown)}. "
                f"Have: {', '.join(TASK_ITEMS[name])}, or '{ALL_ITEMS}'",
            )
        arrived, code, why, mir = self.interlock.check(name)
        if not arrived:
            return answer(code, why, mir)
        code, message = self.arm.claim(name, f"{action}_task")
        logger.info("task %s %s item=%s: %s", name, action, items, message)
        return answer(code, message, mir)

    def _task_status(self, task: str) -> tuple[int, dict]:
        if task not in TASKS:
            return 404, {
                "task": task,
                "success": False,
                "message": f"unknown task '{task}'. Known tasks: {', '.join(TASKS)}",
                "state": None,
                "ros_task": None,
                "controller": None,
                **home_fields(self.arm.busy()),
            }
        message = self.arm.message(task)
        state, ros_task, controller = parse_status_message(message)
        return 200, {
            "task": task,
            "success": True,
            "message": message,
            "state": state,
            "ros_task": ros_task,
            "controller": controller,
            **home_fields(state in BUSY_STATES or self.arm.busy()),
        }

    def _arm_status(self) -> tuple[int, dict]:
        states = self.arm.states()
        busy = [task for task, state in states.items() if state in BUSY_STATES]
        if len(busy) > 1:
            return 409, {
                "success": False,
                "message": f"{len(busy)} tasks report busy at once ({', '.join(sorted(busy))}). "
                "The arm has more than one owner",
                "running": None,
                "task": None,
                "state": None,
                "ready": False,
                "controller": "active",
                "ros_task": None,
                "tasks": states,
                **home_fields(True),
            }
        if busy:
            name = busy[0]
            message = self.arm.message(name)
            state, ros_task, _ = parse_status_message(message)
        else:
            name, state, ros_task = None, "IDLE", None
            failed = sorted(task for task, state in states.items() if state == "ERROR")
            message = (
                f"idle. Last run of {', '.join(failed)} ended in ERROR"
                if failed
                else "idle. No task is running"
            )
        return 200, {
            "success": True,
            "message": message,
            "running": name,
            "task": name,
            "state": state,
            "ready": not busy,
            "controller": "active",
            "ros_task": ros_task,
            "tasks": states,
            **home_fields(bool(busy)),
        }


def _seconds(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mock-ur", description="Mock UR task server for the orchestrator's ur_perform_task."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=PORT, help=f"HTTP port (default {PORT})")
    parser.add_argument(
        "--task-seconds",
        type=_seconds,
        default=8.0,
        help="how long each task runs (default 8). The template stops waiting after 120",
    )
    parser.add_argument(
        "--mir-url",
        default="http://127.0.0.1:50065",
        help="the MiR the arrival interlock asks (default the mock MiR, http://127.0.0.1:50065)",
    )
    parser.add_argument("--no-interlock", action="store_true", help="start tasks wherever the MiR is")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    interlock = Interlock(MirLink(args.mir_url), enabled=not args.no_interlock, mir_url=args.mir_url)
    server = bind(args.host, args.port, UrApp(Arm(args.task_seconds), interlock))
    host, port = server.server_address[:2]
    check = f"on, asking {args.mir_url}" if interlock.enabled else "off"
    serve_until_stopped(
        server,
        f"mock UR task server listening on {host}:{port} | task={args.task_seconds:g}s"
        f" | arrival interlock {check}",
    )


if __name__ == "__main__":
    main()
