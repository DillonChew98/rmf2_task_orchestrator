# gRPC examples

Crossflow diagrams make SiLA 2 calls to four lab instruments.
## Contents

| Folder | Instrument | `uri` |
|---|---|---|
| `sdt/` | SDT | `${lab.tga_host}` |
| `x3dsc/` | X3 DSC | `${lab.dsc_host}` |
| `ftir/` | FTIR | `${lab.ftir_host}` |
| `gpc/` | GPC | `${lab.gpc_host}` |

## Configuration

The orchestrator reads `lab.toml` and `config.toml` when it starts. Restart it after a
change.

### Mock servers

Start the mocks:

```bash
cd mock_servers
uv venv --python 3.12 .venv && uv pip install -e '.[dev]'   # first time only
./run_all.sh
```

`lab.toml`:

```toml
[lab]
dsc_host  = "http://127.0.0.1:50061"
tga_host  = "http://127.0.0.1:50062"
gpc_host  = "http://127.0.0.1:50063"
ftir_host = "http://127.0.0.1:50064"
```

### Real instruments

`lab.toml`:

```toml
[lab]
dsc_host  = "https://<x3dsc-ip>:<port>"
tga_host  = "https://<sdt-ip>:<port>"
gpc_host  = "https://<gpc-ip>:<port>"
ftir_host = "https://<ftir-ip>:<port>"
```

With `https://` hosts, the node uses TLS. Check that the CA cert is at
`assets/certs/ca.crt` and that `config.toml` points to it:

```toml
[grpc]
ca_cert = "assets/certs/ca.crt"
```

## Usage

Start the orchestrator as in [Getting Started](../../README.md#getting-started).

In a second terminal, send a diagram:

```bash
pip install pika   # first time only
python scripts/send_diagram.py diagrams/grpc_examples/sdt/check_health.json
```

```text
Sent check_health.json (<n> bytes)

Done — sent 1 diagram(s)
```

The orchestrator logs the result:

```text
INFO rmf2_task_orchestrator::executor::amqp_handlers: Received AMQP workflow execute: task_id=test-schedule-check_health, task_type=Schedule
INFO rmf2_task_orchestrator::executor::amqp_handlers: Workflow test-schedule-check_health completed: {
  "reply": {
    "Result": {
      "Envelope": {
        "Code": {
          "value": "0"
        },
        ...
        "Outcome": {
          "value": "Succeeded"
        },
        ...
      }
    }
  },
  "result": "succeeded"
}
```

A failed run also logs `completed`. Its output has `"result": "failed"`, the `step` that
failed and a `detail`.

## Diagrams

Each observable diagram makes these three calls. E.g. `ftir/open_drawer.json`:

```text
StageControlService.OpenDrawer          start it, get a command UUID
StageControlService.OpenDrawer_Info     watch until it ends
StageControlService.OpenDrawer_Result   read the result
```

### SDT

| File | Call | Observable | Notes |
|---|---|---|---|
| `check_health` | `Discovery.GetHealthJson` | | Ok if Outcome is Succeeded |
| `check_instrument_idle` | `InstrumentStatus.Subscribe_CurrentInstrumentStatus` | | Ok if idle, Err if busy |
| `park_autosampler_tray` | `AutosamplerControl.ParkAutosampler` | Yes | Position = TraySide |
| `park_autosampler_furnace` | `AutosamplerControl.ParkAutosampler` | Yes | Position = FurnaceSide |
| `open_furnace` | `FurnaceControl.OpenFurnace` | | |
| `close_furnace` | `FurnaceControl.CloseFurnace` | | |
| `add_experiment` | `QueueManagement.AddNewExperiments` | | |
| `execute_experiment` | `RunControl.ControlStart` | Yes | |

### X3 DSC

| File | Call | Observable | Notes |
|---|---|---|---|
| `check_health` | `Discovery.GetHealthJson` | | Ok if Outcome is Succeeded |
| `check_instrument_idle` | `InstrumentStatus.Subscribe_CurrentInstrumentStatus` | | Ok if idle, Err if busy |
| `park_autosampler_tray` | `AutosamplerControl.ParkAutosampler` | Yes | Position = TraySide |
| `park_autosampler_cell` | `AutosamplerControl.ParkAutosampler` | Yes | Position = CellSide |
| `open_lid` | `AutosamplerControl.OpenAutosamplerLid` | Yes | |
| `close_lid` | `AutosamplerControl.CloseAutosamplerLid` | Yes | |
| `load_sample_pan` | `AutosamplerControl.LoadSamplePan` | Yes | |
| `add_experiment` | `QueueManagement.AddNewExperiments` | | |
| `get_queue_status` | `QueueManagement.GetQueueStatusJson` | | QueueType = running |
| `execute_experiment` | `RunControl.ControlStart` | Yes | |
| `export_results` | `ResultsExport.ExportResultsJson` | | Path = `C:/Users/User/Desktop/experiment_output` |

### FTIR

| File | Call | Observable | Notes |
|---|---|---|---|
| `check_health` | `InstrumentStatus.Subscribe_CurrentInstrumentStatus` | | Ok if OPUS is reachable, Err if status is Unreachable |
| `open_drawer` | `StageControlService.OpenDrawer` | Yes | |
| `close_drawer` | `StageControlService.CloseDrawer` | Yes | |
| `measure_sample` | `MeasurementService.MeasureSample` | Yes | The settings are placeholders. Edit them before real use |

### GPC

| File | Call | Observable | Notes |
|---|---|---|---|
| `get_status` | `InstrumentStatus.GetInstrumentSnapshot` | | Status LEDs, sequence progress and modules. GPC must be connected |
| `run_sequence` | `InstrumentLifecycle.Connect`, `InstrumentStatus.GetStartButtonState`, `SequenceControl.ClickStart`, `InstrumentLifecycle.Stop` | | Flow below |

```text
Connect
GetStartButtonState   must be off
ClickStart
GetStartButtonState   poll until off
Stop
```

For real servers, set up the sequence in WinGPC first.

## Node reference

`grpc_request` makes one unary or server-streaming call. From `sdt/check_health.json`:

```json
"ask_health": {
  "type": "node",
  "builder": "grpc_request",
  "config": {
    "service": "sila2.org.tainstruments.sdt.discovery.v2.Discovery",
    "method": "GetHealthJson",
    "uri": "${lab.tga_host}",
    "timeout": 20,
    "request": { "Request": { "Request": { "Version": { "value": 2 }, ... } } }
  },
  "stream_out": { "out": "check_outcome" },
  "next": "if_answered"
}
```

### Config

| Field | Required | Notes |
|---|---|---|
| `service` | yes | Full name, package included. Its `.proto` must sit under `assets/protos`. The build compiles the protos into the binary, so rebuild after you add one |
| `method` | no | Method name, or its index in the service. If unset, the first method |
| `uri` | yes | `http://` is plain. `https://` uses TLS, with the CA from `[grpc] ca_cert` |
| `timeout` | no | Seconds. See Timeout. If unset, the node waits forever |
| `request` | no | The method's input message as JSON. The node sends it on every run and ignores its input message. If unset, the node sends its input message |

### Timeout

`timeout` limits the wait for the call to start, and then the wait for each reply. The
connection has no timeout.

### Output

The node sends each reply on its `out` stream. Connect that stream with `stream_out`.

| Call | `out` |
|---|---|
| Unary | The reply, once |
| Server-streaming | Each reply, until the stream closes |

Replies use the proto field names and include default values.

The node output is a `Result`, so follow it with a `fork_result`.

| Branch | When |
|---|---|
| `ok` | The call ended. It carries no data |
| `err` | A connection failure, a gRPC error or a timeout. It carries one string |
