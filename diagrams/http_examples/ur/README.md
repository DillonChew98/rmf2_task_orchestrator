# UR arm examples

Diagrams that drive the UR arm through the REST API on the MARVEL robot PC, at
`${lab.ur_host}`. `marvel_full.launch.py` starts the API, a FastAPI server. For setup and how
to send a file, see the [HTTP examples README](../README.md).

| File | Does |
|---|---|
| `check_health.json` | Call `GET /health`. Pass if the server answers `ok` |
| `get_tasks.json` | Call `GET /tasks`, which lists the tasks, actions and items the server accepts |
| `start_task.json` | Call `POST /task/start` for the task in the `body`. Return once the task starts |
| `perform_task.json` | Wait until the arm is ready, start the task in `set_task`, and poll `GET /task/status` until the task ends |

> [!WARNING]
> On the real lab, `start_task.json` and `perform_task.json` move the arm. Before sending
> these diagrams, make sure that the arm path is clear.

## Safety

The arm starts a task only when the MiR is parked at that task's position. For a `gpc` task,
the MiR must be at `GPC`. The arm's first move is relative to the MiR, so at the wrong
position the arm can hit the bench or machine there.

| `task` | MiR position | Waypoint in `lab.toml` |
|---|---|---|
| `bench` | `BENCH`, for every item | `bench_waypoint` |
| `tga` | `TGA` | `tga_waypoint` |
| `gpc` | `GPC` | `gpc_waypoint` |
| `dsc` | `DSC` | `dsc_waypoint` |
| `ftir` | `FTIR` | `ftir_waypoint` |

Parked means within 0.35 m of the position, and not moving.

Before each task, the server gets the MiR position from the MiR. The mock UR gets the
position from the mock MiR.

| The MiR is | The server answers | The arm |
|---|---|---|
| Parked at the task's position | 200 | Starts the task |
| At another position, or moving | 412 | Does not move |
| Not reachable | 503 | Does not move |

## Before sending a task

Do these two steps before sending `start_task.json` or `perform_task.json`.

### 1. Set the task

Replace `<task>` with a task from the Safety table.

In `perform_task.json`, edit the `cel` of the first step, `set_task`:

```json
"cel": "{'task': '<task>', 'action': 'place', 'item': 'ALL'}"
```

In `start_task.json`, edit the `body` of the `start_task` step:

```json
"body": { "task": "<task>", "action": "place", "item": "ALL" }
```

Keep all three keys. In `perform_task.json`, a missing key stops the diagram before the task
starts. With `<task>` left in, the server answers 404. `POST /task/start` below lists the
values for `action` and `item`.

For example, to pick the DSC tray from the bench:
`{'task': 'bench', 'action': 'pick', 'item': 'DSC'}`.

### 2. Park the MiR

If the MiR is not at the task's position, the server answers 412. To park the MiR, send
`../mir/move_to_waypoint.json` with the task's waypoint. For the waypoint setting, see the
[MiR README](../mir/README.md#set-the-waypoint).

On the mocks, replace `<station>` with `bench`, `tga`, `gpc`, `dsc` or
`ftir`:

```bash
curl -X POST localhost:50065/api/v2.0.0/mission_queue -H 'Content-Type: application/json' \
  -d '{"mission_id": "mirconst-guid-0000-0001-actionlist00", "parameters": [{"input_name": "Position", "value": "mock-position-<station>"}]}'
```

## Send

With the orchestrator running, from the repo root:

```bash
python3 scripts/send_diagram.py diagrams/http_examples/ur/perform_task.json
```

## API

### `POST /task/start`

Send the fields in the body or as query parameters. If both have a field, the query value wins.

| Field | Values | Required |
|---|---|---|
| `task` | `bench`, `tga`, `gpc`, `dsc`, `ftir` | Yes |
| `action` | `place`: put the item on a surface or into a machine. `pick`: take the item from a surface or a machine | No. Defaults to `pick` for `bench` and `place` for the rest |
| `item` | `ALL`, or one or more items at the station. `bench` has `TGA`, `DSC`, `GPC` and `FTIR`. Each other station has one item, named after the station | No. Defaults to `ALL` |

| Code | Means |
|---|---|
| 200 | Started |
| 400 | Bad argument |
| 404 | Unknown task |
| 409 | The arm is busy, or a mission is running |
| 412 | The MiR is not parked at the task's position, or is moving |
| 501 | The station has no such action taught |
| 503 | The server cannot reach the task node or the MiR |

Every reply has `success` and `message`.

### `GET /task/status`

With `?task=<task>`, the reply covers that task. Without `?task`, the reply covers the arm
and adds `running` and `ready`.

| `state` | Means |
|---|---|
| `IDLE` | Not running |
| `WAITING_FOR_CONTROLLER` | Accepted, waiting for External Control on the arm |
| `EXECUTING` | Running |
| `PAUSED` | Paused |
| `COMPLETED` | Ended well |
| `ERROR` | Failed |

`COMPLETED` and `ERROR` stay until that task starts again.

Every reply also has `arm_at_home` and `mir_safe_to_move`. After an `ERROR`,
`mir_safe_to_move` shows whether the MiR can still drive.

### Other routes

- `GET /tasks`: lists the tasks, actions and items the server accepts.
- `POST /mission/start`, `GET /mission/status`: run and watch the full MiR and UR loop. For
  tests on the robot PC only.
- `GET /health`: answers `ok` when the server is up.
- `/docs`: interactive API docs.
