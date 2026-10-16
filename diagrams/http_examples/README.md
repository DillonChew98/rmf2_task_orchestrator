# HTTP examples

Diagrams for the `http_request` node, one folder for each device. `mir/` drives the MiR250
base and `ur/` drives the UR arm. Each folder has its own README.

## Running one

Hosts and waypoints are `${lab.NAME}` placeholders. The orchestrator fills them from
`lab.toml`, but only for diagrams sent over RabbitMQ. Send one with:

```bash
python scripts/send_diagram.py diagrams/http_examples/mir/status.json
```

The script needs `pika`, the Python RabbitMQ client: `pip install pika`.

`POST /api/executor/run` and the editor at <http://localhost:2727/> do not fill
placeholders. A run there fails on the first request, because the URI still reads
`${lab.mir_host}/...`.

`lab.toml` points at the mock servers. For the real lab, set these, plus the waypoint
guids from `../../MiR.md`. Restart the orchestrator after a change.

```toml
mir_host = "http://192.168.68.50"
ur_host = "http://192.168.68.25:8080"
```

## Node config

| Field | Required | Notes |
|---|---|---|
| `method` | yes | `GET`, `POST`, `PUT`, `PATCH` or `DELETE` |
| `uri` | yes | full URL, scheme included |
| `headers` | no | string map. `Content-Type: application/json` is set for you when a body is sent |
| `body` | no | JSON. When absent, the message flowing into the node is sent as the body. Ignored for `GET` and `DELETE` |

## What comes out

The node returns a `Result`, so follow it with a `fork_result`.

- 2xx with a JSON body: `ok` carries the parsed JSON.
- 2xx with an empty body: `ok` carries `{"status": <code>}`.
- Anything else, including a connection failure: `err` carries the status and response
  text as one string.

## Client settings

Timeout and a custom CA go in the `[http]` table of `config.toml`, alongside the
orchestrator's own `host` and `port`. Both are optional.

```toml
[http]
host = "127.0.0.1"
port = 2727
request_timeout_secs = 30
connect_timeout_secs = 5
ca_bundle = "/path/to/ca.pem"
```
