# MiR examples

Drives the MiR250 mobile base, `MiR_1153`. In the lab it is at `http://192.168.68.50/api/v2.0.0`.

`${lab.mir_host}`, `${lab.tga_waypoint}` and `${lab.charger_waypoint}` come from `lab.toml`.
They are filled only when you send the diagram over RabbitMQ. See `../README.md`.
`${MIR_AUTH}` comes from `.env.development`.

The section template is `../../marvel/mir_move_to_waypoint.json`.

| File | Does | Shape |
|---|---|---|
| `status.json` | GET `/status` | one GET node |
| `go_to_waypoint.json` | drive to a waypoint | one POST node, body in config |
| `charge.json` | drive to the charger and charge | one POST node, body in config |
| `action_id.json` | find the newest queue entry, then the action id in it | two chained GETs |
| `wait_until_idle.json` | poll `/status` until the robot is ready, 60 s limit | poll loop |
| `move_to_waypoint.json` | wait until ready, queue a Move, poll until done. On failure, clear the queue and charge | the full move |

## Read this before running a POST

**Queueing a mission can start the robot moving.** There is no safe dry run.

| `state_id` | What a POST does |
|---|---|
| 3, Ready | The robot **drives immediately** |
| 4, Pause | The job waits as `Pending` |
| 5, Executing | Your job queues behind what is running |
| 10 or 12 | E-stop or fault. The job will not run |

Run `status.json` first, and act on that reading, not on an older one. Somebody else
can resume the robot between your read and your write.

To release a job queued while paused: `PUT /status {"state_id": 3}`.
To stop a robot: `PUT /status {"state_id": 4}`.

The full thirteen `state_id` values are in `../../../MiR.md`.

## Picking a destination

Set the guid in `lab.toml`:

- `go_to_waypoint.json` goes to `tga_waypoint`.
- `charge.json`, and the failure path of `move_to_waypoint.json`, go to `charger_waypoint`.
- `move_to_waypoint.json` takes its destination from the start message, `{"waypoint": "<guid>"}`.

The guids for each map are under "Waypoint guids" in `../../../MiR.md`. Use a guid on
the loaded map.

`charger_waypoint` must be the `MiR Charge 48V` marker, type 20. Do **not** use the
entry position beside it, type 21. Both have the same name, so look positions up by
guid, never by name.

## The mission parameter name is per mission

| Mission | guid | Destination parameter |
|---|---|---|
| `Move` | `mirconst-guid-0000-0001-actionlist00` | `Position` |
| `ChargeAtStation` | `mirconst-guid-0000-0004-actionlist00` | `chargingStationPosition` |
| `rmf_move_to_position` | `8f4cf9e5-4da1-11f0-865d-000e8eaca9f1` | `end_waypoint` |

All three wrap the same underlying action. Only the exposed name differs, so never
copy a parameter name from one mission to another. To find the name for any other
mission, `GET /missions/{guid}/actions` and take any parameter whose `input_name` is
not null. The swagger does not document these.

## Auth

Basic auth, but the password is SHA-256 hashed before encoding. A normal Basic Auth
tab sends the raw password and gets a 401.

Each file writes the header as `"Authorization": "${MIR_AUTH}"`. The http node swaps in the
env var `MIR_AUTH` on each request. Set it in `.env.development`, quoted:
`MIR_AUTH="Basic ..."`. `../../../MiR.md` has the command that builds the value.
