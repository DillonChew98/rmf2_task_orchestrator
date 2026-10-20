# HTTP examples

Crossflow diagrams that use the `http_request` node to drive the lab robots.

| Folder | Device | `uri` |
|---|---|---|
| [`mir/`](mir/README.md) | MiR250 mobile base | `${lab.mir_host}` |
| [`ur/`](ur/README.md) | UR arm, through the MARVEL robot PC | `${lab.ur_host}` |

Each folder's README lists the diagrams in that folder. The README also marks the diagrams
that need an edit before sending, and the diagrams that move the robot.

## Setup

Set up the orchestrator as in the [main README](../../README.md#getting-started). Then set
`lab.toml` and `.env.development` for the mocks or for the real lab, as below. The
orchestrator reads these files only at startup. Restart the orchestrator after each change.

### Mock servers

Before the first run, do the Setup steps in
[mock_servers/README.md](../../mock_servers/README.md#setup). Then start the mocks:

```bash
cd mock_servers && ./run_all.sh
```

`lab.toml`:

```toml
[lab]
mir_host         = "http://127.0.0.1:50065"
ur_host          = "http://127.0.0.1:50066"
bench_waypoint   = "mock-position-bench"
spm_waypoint     = "mock-position-bench"
dsc_waypoint     = "mock-position-dsc"
tga_waypoint     = "mock-position-tga"
ftir_waypoint    = "mock-position-ftir"
gpc_waypoint     = "mock-position-gpc"
charger_waypoint = "mock-position-charger"
```

`.env.development`:

```bash
MIR_AUTH="Basic mock"   # the mock accepts any value
```

### Real lab

`lab.toml`:

```toml
[lab]
mir_host         = "http://<mir-ip>"
ur_host          = "http://<robot-pc-ip>:8080"
bench_waypoint   = "b04b2c25-c5f1-11f1-abf0-000e8eaca9f1"
spm_waypoint     = "b04b2c25-c5f1-11f1-abf0-000e8eaca9f1"
dsc_waypoint     = "907dcdfd-c610-11f1-abf0-000e8eaca9f1"
tga_waypoint     = "c459cb45-c879-11f1-abd5-000e8eaca9f1"
ftir_waypoint    = "e94dea94-c850-11f1-abd5-000e8eaca9f1"
gpc_waypoint     = "2d624c18-c851-11f1-abd5-000e8eaca9f1"
charger_waypoint = "a295c878-c39b-11f1-bf09-000e8eaca9f1"
```

The guids belong to MiR map `L8`. A guid from another map fails at run time. For now,
`bench_waypoint` and `spm_waypoint` both point at `BENCH`.

`.env.development`:

```bash
MIR_AUTH="Basic <value>"
```

### HTTP client (optional)

`config.toml`. The timeouts below are the defaults:

```toml
[http]
request_timeout_secs = 30
connect_timeout_secs = 5
ca_bundle            = "/path/to/ca.pem"
```

## Send a diagram

On the real lab, send `mir/check_health.json` and `ur/check_health.json` to check if both the mir and ur are up 

From the repo root, start the orchestrator, then send a file:

```bash
cargo run                                                       # terminal 1
python3 scripts/send_diagram.py diagrams/http_examples/<file>   # terminal 2
```


## Node reference

### Config

| Field | Required | Notes |
|---|---|---|
| `method` | Yes | `GET`, `POST`, `PUT`, `PATCH` or `DELETE` |
| `uri` | No | Full URL. Without `uri`, the node reads `{"uri": "...", "body": ...}` from the node input. `body` is optional |
| `headers` | No | String map. The node replaces a `${NAME}` value with env var `NAME`. The node adds `Content-Type: application/json` to a request with a body |
| `body` | No | JSON. Without `body`, the node sends the node input as the body. The node sends no body for `GET` and `DELETE` |
| `retry` | No | Without `retry`, the node tries once |

### Retry

```json
"retry": { "max_attempts": 5, "wait_secs": 5, "retryable_statuses": [408, 429, 503] }
```

These values are the defaults. `"retry": {}` uses the defaults.

- The node retries connection failures, timeouts and `retryable_statuses`. Other error
  statuses fail at once.
- The node does not retry a `POST` or `PATCH` timeout, because the server may have acted.
- Retries stop when the workflow ends.

### Output

The node returns a `Result`. Connect a `fork_result` after the node.

| Response | Output |
|---|---|
| 2xx, JSON body | `ok`: the parsed JSON |
| 2xx, other body | `ok`: the text as a string |
| 2xx, empty body | `ok`: `{"status": <code>}` |
| Other status, or connection failure | `err`: one string with the status and response text |

After retries, `err` starts with `failed after <n> attempts:`.
