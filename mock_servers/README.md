# mock_servers

Six fake lab devices for running the orchestrator and the backend with no
hardware. Four speak real SiLA 2 over gRPC. Two speak the MiR and UR HTTP APIs.

| Mock | Port | Stands in for |
|---|---|---|
| `x3dsc` | 50061 | TA Instruments Discovery X3 DSC |
| `sdt` | 50062 | TA Instruments Discovery SDT 650 (TGA) |
| `gpc` | 50063 | Agilent WinGPC |
| `ftir` | 50064 | Bruker OPUS FTIR |
| `mir` | 50065 | The MiR REST API |
| `ur` | 50066 | The task server on the robot-control PC that drives the UR arm |

All six bind `127.0.0.1`. Another machine cannot reach them unless you pass
`--host 0.0.0.0`.

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/). No other checkout. The real
instrument packages sit in `vendor/`.

```bash
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
scripts/make_dev_certs.sh        # writes certs/: a dev CA and a localhost server cert
```

## Run

```bash
./run_all.sh                      # all six, logs in logs/, Ctrl-C stops them
./run_all.sh --run-seconds 60     # flags go to the four SiLA mocks only
.venv/bin/python -m mock_servers.mir.server --drive-seconds 2   # one mock
```

The four SiLA mocks serve plaintext by default. `--tls` makes them serve TLS
with the dev certs, which is how the real instruments answer. The orchestrator
and the backend can use either.

Each mock is `python -m mock_servers.<name>.server`. Run each one in its own
process. Two SiLA mocks in one process break.

### Flags

Every mock takes `--host`, `--port` and `-v`.

| Flag | Mocks | Default | Meaning |
|---|---|---|---|
| `--run-seconds N` | x3dsc, sdt, gpc, ftir | 20 | how long a simulated run takes; minimum 5 |
| `--no-advertise` | x3dsc, sdt, gpc, ftir | off | stay off mDNS |
| `--tls` | x3dsc, sdt, gpc, ftir | off | serve TLS with the cert and key below |
| `--cert-chain PATH` | x3dsc, sdt, gpc, ftir | `certs/server.crt` | server certificate PEM for `--tls` |
| `--private-key PATH` | x3dsc, sdt, gpc, ftir | `certs/server.key` | server private key PEM for `--tls` |
| `--auto-generate-tls` | x3dsc, sdt, gpc, ftir | off | self-signed TLS; the orchestrator cannot verify it |
| `--instrument-root PATH` | x3dsc, sdt, gpc, ftir | `vendor/` | run against a live `marvel_digitalisation` checkout |
| `--instrument-id ID` | x3dsc, sdt | X3DSC, SDT | TRIOS instrument id the fake REST API answers for |
| `--auto-connect` | gpc, ftir | off | gpc starts in the Connect state; ftir opens the simulated OPUS session at startup |
| `--drive-seconds N` | mir | 8 | how long each mission drives |
| `--task-seconds N` | ur | 8 | how long each task runs |
| `--mir-url URL` | ur | `http://127.0.0.1:50065` | the MiR the arrival interlock asks |
| `--no-interlock` | ur | off | start tasks wherever the MiR is |

Do not set `--run-seconds` below 5. The real run control needs that long to see
a run start.

## Point the orchestrator at them

In the orchestrator's `lab.toml`:

```toml
[lab]
mir_host = "http://127.0.0.1:50065"
ur_host = "http://127.0.0.1:50066"
dsc_host = "http://127.0.0.1:50061"
tga_host = "http://127.0.0.1:50062"
ftir_host = "http://127.0.0.1:50064"

spm_waypoint = "mock-position-bench"
dsc_waypoint = "mock-position-dsc"
tga_waypoint = "mock-position-tga"
ftir_waypoint = "mock-position-ftir"
gpc_waypoint = "mock-position-gpc"
charger_waypoint = "mock-position-charger"
```

An env var `RMF2_TO__LAB__<NAME>` overrides the file value. If
`.env.development` sets any of these, remove them there first.

In `.env.development`, set `MIR_AUTH="Basic any"`. The mock MiR accepts any
`Authorization` header, but the orchestrator refuses to send the request if
`MIR_AUTH` is unset.

To test TLS, start the SiLA mocks with `--tls`, change the three SiLA hosts
above to `https://`, and point the orchestrator at the dev CA, in `config.toml`
or as an env var:

```toml
[grpc]
ca_cert = "mock_servers/certs/ca.crt"
```

```bash
RMF2_TO__GRPC__CA_CERT=mock_servers/certs/ca.crt
```

The host values carry the scheme, so the templates must use `${lab.<x>_host}`
as the whole URL base, with no `http://` of their own in front. An `https://`
value makes the orchestrator verify the server cert. The dev CA signed
`certs/server.crt` for `localhost` and `127.0.0.1` only, so keep `127.0.0.1`.

## Check and steer the robots

```bash
curl -s localhost:50065/api/v2.0.0/status | jq '{state_text, position}'
curl -s localhost:50066/task/status | jq '{ready, state, mir_safe_to_move}'
curl -s -X POST localhost:50065/mock/fail-next   # next MiR move ends Aborted
curl -s -X POST localhost:50066/mock/fail-next   # next UR task ends ERROR
```

The MiR starts parked at CHARGER. Positions: `mock-position-charger`,
`mock-position-bench`, `mock-position-dsc`, `mock-position-tga`,
`mock-position-ftir`, `mock-position-gpc`, `mock-position-storage`. The UR refuses a task with 412 unless
the MiR is parked within 0.35 m of that task's position, or `--no-interlock`
is set.

## Rules

- A run that is already running refuses a second start, like the hardware.
- With the backend: stop the older `backend/tests/sila/fixtures/x3_mock` before
  starting `x3dsc`. They claim the same feature identifiers and the backend
  returns 500 for whichever connects second.
- With the backend: mDNS is on by default. The backend keeps every mock it hears
  in `data/servers.json`. Remove stale rows with `DELETE /sila/servers/{uuid}`.
  The mock UUIDs are stable and end in the port, as in
  `00000000-0000-4000-8000-000000050061`.

## Test

```bash
.venv/bin/python -m pytest              # about 210 tests, about 80 s
.venv/bin/python -m pytest tests/unit   # fast, no servers
.venv/bin/python -m pytest tests/robots # MiR and UR over HTTP, a few seconds
```

Tests run one process per file on purpose. Do not drop `-n 4 --dist loadfile`
from `pyproject.toml`.

## Refresh the vendored instrument code

```bash
scripts/vendor_instruments.sh /path/to/marvel_digitalisation
.venv/bin/python -m pytest
```

`vendor/SOURCE` records the commit it came from. The mocks serve the real
features, commands and parameters as of that commit. If the e2e tests fail
after a refresh, a fake driver is missing something the new server code calls.
