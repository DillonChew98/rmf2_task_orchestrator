"""Mock MiR REST API, port 50065.

Answers the calls the orchestrator's mir_move_to_waypoint template makes, the
UR task server's arrival interlock makes, and schedule_gui's robot card reads.
Any Authorization header is accepted and none is required.

    python -m mock_servers.mir.server [--port 50065] [--drive-seconds 8]
"""

from __future__ import annotations

import argparse

from ..harness.json_http import Request, bind, configure_logging, serve_until_stopped
from .robot import POSITIONS, MissionRejected, Robot, guid_for

PORT = 50065
API = "/api/v2.0.0/"


def _error(message: str) -> dict:
    return {"error_human": message}


class MirApp:
    def __init__(self, robot: Robot) -> None:
        self.robot = robot

    def __call__(self, request: Request) -> tuple[int, object]:
        method, path = request.method, request.path
        if (method, path) == ("POST", "/mock/fail-next"):
            self.robot.fail_next_move()
            return 200, {"message": "the next move mission ends Aborted, halfway"}
        parts = path[len(API) :].split("/") if path.startswith(API) else []
        match method, parts:
            case "GET", ["status"]:
                return 200, self.robot.status()
            case "GET", ["positions"]:
                return 200, self.robot.positions()
            case "GET", ["positions", guid]:
                found = self.robot.position(guid)
                return (200, found) if found else (404, _error(f"no position with guid {guid!r}"))
            case "GET", ["mission_queue"]:
                return 200, self.robot.missions()
            case "POST", ["mission_queue"]:
                try:
                    return 201, self.robot.post_mission(request.body)
                except MissionRejected as error:
                    return 400, _error(str(error))
            case "DELETE", ["mission_queue"]:
                self.robot.clear_queue()
                return 204, None
            case "GET", ["mission_queue", queue_id] if queue_id.isdigit():
                found = self.robot.mission(int(queue_id))
                return (200, found) if found else (404, _error(f"no mission {queue_id} in the queue"))
        return 404, _error(f"this mock has no {method} {path}")


def _seconds(text: str) -> float:
    value = float(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="mock-mir", description="Mock MiR REST API for the orchestrator's MiR templates."
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=PORT, help=f"HTTP port (default {PORT})")
    parser.add_argument(
        "--drive-seconds",
        type=_seconds,
        default=8.0,
        help="how long each mission drives (default 8). The template stops waiting after 120",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    server = bind(args.host, args.port, MirApp(Robot(args.drive_seconds)))
    host, port = server.server_address[:2]
    positions = ", ".join(f"{name}={guid_for(name)}" for name in POSITIONS)
    serve_until_stopped(
        server,
        f"mock MiR listening on {host}:{port} | drive={args.drive_seconds:g}s | at CHARGER"
        f" | positions: {positions}",
    )


if __name__ == "__main__":
    main()
