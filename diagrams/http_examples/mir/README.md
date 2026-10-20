# MiR examples

Diagrams that drive the MiR250 mobile base, `MiR_1153`, through the MiR REST API at
`${lab.mir_host}/api/v2.0.0`. For setup and how to send a file, see the
[HTTP examples README](../README.md).

| File | Does |
|---|---|
| `check_health.json` | Call `GET /status`. Pass if the robot answers and reports no errors |
| `get_status.json` | Call `GET /status` and return the whole reply |
| `wait_until_idle.json` | Poll `/status` every 2 s until the robot is idle. Give up after 60 s |
| `send_to_waypoint.json` | Queue a `Move` to `tga_waypoint` |
| `send_to_charger.json` | Queue a `ChargeAtStation` at `charger_waypoint` |
| `move_to_waypoint.json` | Wait until the robot is idle, queue a `Move` to the waypoint in `set_waypoint`, and poll until the move ends. Skip the move if the robot is within 0.3 m. On failure, clear the queue and send the robot to the charger |
| `get_newest_action_id.json` | Find the newest queue entry, then the action id in that entry. Fails on the mock, because the mock MiR has no `GET /mission_queue/{id}/actions` |

> [!WARNING]
> On the real lab, `send_to_waypoint.json`, `send_to_charger.json` and `move_to_waypoint.json`
> drive the MiR. Before sending these diagrams, make sure that the MiR path is clear.

## Set the waypoint

Before sending `move_to_waypoint.json`, set the station in the first step, `set_waypoint`:

```json
"cel": "{'waypoint': '${lab.<station>_waypoint}'}"
```

`<station>` is `bench`, `spm`, `dsc`, `tga`, `ftir` or `gpc`. To send the MiR to the
charger, use `send_to_charger.json`.

## Send

With the orchestrator running, from the repo root:

```bash
python3 scripts/send_diagram.py diagrams/http_examples/mir/get_status.json
```
