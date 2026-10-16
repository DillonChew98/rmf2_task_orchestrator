# UR arm examples

Drives the UR arm through the MARVEL robot PC. In the lab it is at `http://192.168.68.25:8080`.
The diagrams write it as `${lab.ur_host}`, from `lab.toml`. It is filled only over
RabbitMQ. See `../README.md`.

That is a FastAPI server running beside the mission node. It starts with
`marvel_full.launch.py`. No auth. Any machine on the `192.168.68.x` network can
call it, with no ROS 2 installed on the client side.

The section template is `../../marvel/ur_perform_task.json`.

| File | Does |
|---|---|
| `start_task.json` | POST `/task/start` — run one arm task |
| `perform_task.json` | wait for the arm to be ready, start one task, poll until it ends |

## The call returns before the arm finishes

`POST /task/start` returns as soon as the task **starts**. To know when it ends,
poll `GET /task/status?task=<task>`.

| `state` | Means |
|---|---|
| `EXECUTING` | Running |
| `COMPLETED` | Finished, success |
| `ERROR` | Failed |
| `WAITING_FOR_CONTROLLER` | Accepted, waiting for the arm's External Control to come up |
| `IDLE` / `PAUSED` | Not running / paused |

## Body fields

```json
{ "task": "grabgpc", "action": "place" }
```

| Field | Values | Required |
|---|---|---|
| `task` | `tga` `gpc` `dsc` `ftir` `tray_pickup` `grabtga` `grabgpc` `grabdsc` `grabftir` | yes |
| `action` | `place` — tray into the machine · `pick` — tray out of it | no, default `place` |
| `id` | which tray or holder at that machine | no |

`action` and `id` are accepted but not acted on yet. `action=pick` returns 501 rather
than quietly running a `place`. Send both anyway — the call will not change when they
go live.

`GET /tasks` asks the robot what it accepts, so a client can check its own config
against the robot rather than against this page.

## Status codes

| Code | Meaning |
|---|---|
| 200 | Started |
| 400 | Bad argument |
| 404 | Unknown task |
| 409 | That task already runs, or a mission holds the arm |
| 501 | Action not implemented yet |
| 503 | That task node is not running |

Every reply carries `success` and `message`, whatever the code.

## The whole loop, for testing only

`POST /mission/start` runs all twelve steps: the MiR drives, the UR works, back to the
charger. `GET /mission/status` returns the full run record, so slow polling cannot miss
a step. Interactive docs at <http://192.168.68.25:8080/docs>.

In deployment an outside orchestrator drives the MiR itself and calls one UR task at a
time. That is what `start_task.json` is for.
