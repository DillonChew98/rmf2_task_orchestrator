# marvel/

Section templates for diagrams that the schedule adapter sends.

A template can use other templates as its steps, and those templates can use more templates.

## How templates load

- `TemplateRegistry` (`src/template_registry.rs`) loads every `.json` file in this folder at
  startup. Point `[app] template_directory` in `config.toml` here.
- For each diagram that arrives over AMQP, the registry adds the templates the diagram names
  and the templates those templates use.
- Each template key must match a `builder` value that the schedule adapter sends.
- The adapter's `services/dag_converter/registry.json` maps each APS operation name to one
  `builder`. For example, "Pick GPC vials from SPM" maps to `pick_gpc_vials_from_spm`.
- Name each file after the template key, as in `pick_gpc_vials_from_spm.json`.

## Workflows

### FTIR, TGA, DSC and GPC

Each workflow runs five steps in order. Here `<x>` is `ftir`, `tga` or `dsc`.

| Step | `<x>`: SPM to workbench | `<x>`: storage buffer and back | GPC: SPM to workbench |
|---|---|---|---|
| 1 | `pick_<x>_pan_sample_tray_from_spm` | `pick_<x>_pan_sample_tray_from_storage` | `pick_gpc_vials_from_spm` |
| 2 | `place_<x>_pan_sample_tray_on_<x>` | `place_<x>_pan_sample_tray_on_<x>` | `place_gpc_vials_at_gpc` |
| 3 | `<x>_process` | `<x>_process` | `gpc_process` |
| 4 | `pick_<x>_pan_sample_tray_from_<x>` | `pick_<x>_pan_sample_tray_from_<x>` | `pick_gpc_vials_from_gpc` |
| 5 | `place_<x>_pan_sample_tray_on_workbench` | `place_<x>_pan_sample_tray_in_storage` | `place_gpc_vials_on_workbench` |

- `<x>_process` uses the section's `experiment_id` to fetch the experiment from
  `experiment_server_host` in `lab.toml`. The orchestrator refuses the schedule if an
  experiment is missing or not `ready`.
- `gpc_process` needs no `experiment_id`. Load the GPC sequence in WinGPC before it runs.
- See [Storage buffer](#storage-buffer) for the two storage steps.

### Other versions

To use one of these versions, put the template name in the adapter's registry in
`schedule_adapter/services/dag_converter/registry.json`.

- **Mock process.** `<x>_mock_process` waits 5 s, then reports ok. Use `<x>_mock_process` in
  place of `<x>_process` to skip the instrument run.
- **Fixed payload.** `<x>_process_with_fixed_payload` runs a fixed experiment and gets nothing
  from the experiment server. It needs no `experiment_id`.
- **DSC with a pause.** `pick_dsc_pan_sample_tray_from_spm_with_pause` and
  `place_dsc_pan_sample_tray_on_dsc_with_pause` pause before the UR arm moves. See
  [DSC pause](#dsc-pause).

## Storage buffer

Loading puts a tray into the storage buffer. Unloading takes a tray out of the storage buffer.

| Template | What |
|---|---|
| `place_<x>_pan_sample_tray_in_storage` | Drives the MiR to the storage buffer. The UR arm places the tray in it. |
| `pick_<x>_pan_sample_tray_from_storage` | Drives the MiR to the storage buffer. The UR arm takes the tray out. |
| `storage_start_loading`, `storage_finish_loading` | The PLC steps before and after a separate UR place step. |
| `storage_start_unloading`, `storage_finish_unloading` | The PLC steps before and after a separate UR pick step. |

Each `storage_*` template does one PLC step. Only the templates above use them.

## Action templates

These templates each drive one robot or instrument.

### Robots

| Template | What |
|---|---|
| `mir_move_to_waypoint` | Checks the arm is in safe position, drives to a waypoint and waits to arrive. |
| `ur_perform_task` | Runs one UR task and waits for the task to end. |

On failure, `mir_move_to_waypoint` clears the mission queue and drives the MiR to the charger.

### FTIR

| Template | What |
|---|---|
| `ftir_open_drawer` | Opens the FTIR drawer. |
| `ftir_measure_background` | Measures the background, with mocked parameters. |
| `ftir_measure_sample` | Measures the sample with the experiment fetched from the experiment server. |
| `ftir_measure_sample_with_fixed_payload` | Measures the sample, with mocked parameters. |
| `ftir_close_drawer` | Closes the FTIR drawer. |

### X3 DSC

| Template | What |
|---|---|
| `dsc_park_autosampler_cell` | Parks the autosampler. |
| `dsc_close_lid` | Closes the autosampler lid. |
| `dsc_add_experiment` | Queues the experiment fetched from the experiment server. |
| `dsc_add_experiment_with_fixed_payload` | Queues an experiment, with mocked parameters. |
| `dsc_execute_experiment` | Starts the run and waits for the run to end. |

### SDT (TGA)

| Template | What |
|---|---|
| `tga_park_autosampler_furnace` | Parks the autosampler. |
| `tga_close_furnace` | Sends the close-furnace command and does not wait for the furnace to close. |
| `tga_add_experiment` | Queues the experiment fetched from the experiment server. |
| `tga_add_experiment_with_fixed_payload` | Queues an experiment, with mocked parameters. |
| `tga_execute_experiment` | Starts the run and waits for the run to end. |

### GPC

| Template | What |
|---|---|
| `gpc_run_sequence` | Connects to WinGPC, clicks Start and waits for the run to end. Then it calls `Stop`. |

### Workflow control

| Template | What |
|---|---|
| `pause_until_resume` | Sends `waiting_for_resume` on MQTT topic `workflow/status`. Pauses until `resume` arrives on `workflow/resume`. Fails after 1 hour. |

## DSC pause

The two `_with_pause` DSC templates stop before the UR arm moves.

1. Watch the workflow topics:

   ```bash
   docker compose exec mosquitto mosquitto_sub -t 'workflow/#' -v
   ```

2. After `workflow/status {"state":"waiting_for_resume"}` shows, resume with:

   ```bash
   docker compose exec mosquitto mosquitto_pub -t workflow/resume -m '{"state":"resume"}'
   ```
