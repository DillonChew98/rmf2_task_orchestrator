# marvel/

Section templates for diagrams that the schedule adapter sends.

## How templates load

- `TemplateRegistry` (`src/template_registry.rs`) loads every `.json` file in this folder at
  startup. Point `[app] template_directory` in `config.toml` here.
- For each diagram that arrives over AMQP, the registry adds the templates the diagram names,
  plus the templates those templates use.
- Each file holds one template, as `{"templates": {"<name>": <body>}}`. The registry ignores
  other keys.
- The key must match the `builder` value the schedule adapter sends. The adapter's
  `services/dag_converter/registry.json` maps each APS operation name to one `builder`. For
  example, "Pick GPC vials from SPM" maps to `pick_gpc_vials_from_spm`.
- Name each file after the template key, as in `pick_gpc_vials_from_spm.json`. The registry ignores the
  file name.

## Workflows

### FTIR, TGA and DSC

Each workflow runs five steps in order. Here `<x>` is `ftir`, `tga` or `dsc`.

| Step | From SPM, back to the workbench | From the storage buffer and back |
|---|---|---|
| 1 | `pick_<x>_pan_sample_tray_from_spm` | `pick_<x>_pan_sample_tray_from_storage` |
| 2 | `place_<x>_pan_sample_tray_on_<x>` | `place_<x>_pan_sample_tray_on_<x>` |
| 3 | `<x>_process` | `<x>_process` |
| 4 | `pick_<x>_pan_sample_tray_from_<x>` | `pick_<x>_pan_sample_tray_from_<x>` |
| 5 | `place_<x>_pan_sample_tray_on_workbench` | `place_<x>_pan_sample_tray_in_storage` |

The two storage steps are in [Storage buffer](#storage-buffer).

### GPC

The lab has no GPC instrument box, so this folder has `mock_gpc_process` and no
`gpc_process`. The storage buffer holds no GPC vials.

1. `pick_gpc_vials_from_spm`
2. `place_gpc_vials_at_gpc`
3. `mock_gpc_process`
4. `pick_gpc_vials_from_gpc`
5. `place_gpc_vials_on_workbench`

### Other versions

To use one of these versions, put the template name in the adapter's registry.

- **Mock process.** `mock_<x>_process` waits 5 s, then reports ok. Use `mock_<x>_process` in
  place of `<x>_process` to skip the real run.
- **DSC with a pause.** `pick_dsc_pan_sample_tray_from_spm_with_pause` and
  `place_dsc_pan_sample_tray_on_dsc_with_pause` pause before the UR arm moves. See
  [DSC pause](#dsc-pause).

## Storage buffer

Loading puts a tray into the storage buffer. Unloading takes a tray out of the storage buffer.
All storage buffer templates read the PLC address from `eq_opcua_host` in `lab.toml`.

The templates come in two sizes:

- **Big templates** are whole workflow steps. The adapter's registry can name a big template.
  Each one runs small templates in order.
- **Small templates** each do one PLC action. Only big templates use small templates.

### Loading

Big templates:

| Template | Runs |
|---|---|
| `place_<x>_pan_sample_tray_in_storage` | Drives the MiR to `storage_waypoint`, runs small steps 1 to 3, UR `storage_in` place, then small steps 4 and 5. |
| `start_storage_loading` | Small steps 1 to 3. |
| `finish_storage_loading` | Small steps 4 and 5. |

Use `start_storage_loading` and `finish_storage_loading` when a separate UR place step runs
between the start and the finish.

Small templates, in run order:

| Step | Template | Action |
|---|---|---|
| 1 | `storage_send_load_request` | Sets `OPC_AMR_Send_Request`. |
| 2 | `storage_wait_ready_to_receive` | Waits until `OPC_PLC_Ready_To_Receive` is true. Fails after 120 s. |
| 3 | `storage_set_send_busy` | Sets `OPC_AMR_Send_Busy`. |
| | | The UR arm places the tray in the storage buffer. |
| 4 | `storage_set_send_complete` | Sets `OPC_AMR_Send_Complete`. |
| 5 | `storage_wait_load_ack_and_clear` | Waits until `OPC_PLC_Send_Complete_Ack` is true, then clears the `OPC_AMR_Send_*` tags. Fails after 30 s. |

### Unloading

Big templates:

| Template | Runs | Tray ID |
|---|---|---|
| `pick_<x>_pan_sample_tray_from_storage` | Checks the instrument is up, drives the MiR to `storage_waypoint`, runs small steps 1 to 3, UR `storage_out` pick, then small steps 4 and 5. | None |
| `start_storage_unloading` | Small steps 1 to 3. | `TRAY_001` |
| `finish_storage_unloading` | Small steps 4 and 5. | `TRAY_001` |

Use `start_storage_unloading` and `finish_storage_unloading` when a separate UR pick step runs
between the start and the finish.

Small templates, in run order. Steps 1 and 5 each have two versions:

| Step | Template | Action |
|---|---|---|
| 1 | `storage_send_unload_request` | Writes tray ID `TRAY_001` and sets `OPC_AMR_Unload_Request`, then waits for the storage buffer to answer. Fails after 30 s, or if the storage buffer reports `OPC_PLC_TrayID_Not_Found`. |
| 1 | `storage_send_unload_request_no_trayid` | Sets `OPC_AMR_Unload_Request` with no tray ID, then waits for `OPC_PLC_Unload_Request_Ack`. Fails after 30 s. |
| 2 | `storage_wait_ready_to_unload` | Waits until `OPC_PLC_Ready_To_Unload` is true. Fails after 120 s. |
| 3 | `storage_set_unload_busy` | Sets `OPC_AMR_Unload_Busy`. |
| | | The UR arm takes the tray out of the storage buffer. |
| 4 | `storage_set_unload_complete` | Sets `OPC_AMR_Unload_Complete`. |
| 5 | `storage_wait_unload_ack_and_clear` | Waits until `OPC_PLC_Unload_Complete_Ack` is true, then clears the tray ID and the `OPC_AMR_Unload_*` tags. Fails after 30 s. |
| 5 | `storage_wait_unload_ack_and_clear_no_trayid` | Waits until `OPC_PLC_Unload_Complete_Ack` is true, then clears the `OPC_AMR_Unload_*` tags. Fails after 120 s. |

Use the two `_no_trayid` templates together, as `pick_<x>_pan_sample_tray_from_storage` does.

## Action templates

These small templates each drive one robot or instrument. The storage buffer's small templates
are in [Storage buffer](#storage-buffer).

### Robots

| Template | Action |
|---|---|
| `mir_move_to_waypoint` | MiR: checks the arm is in safe position, drives to a waypoint and waits for arrival. On failure, clears the mission queue and drives to the charger. |
| `ur_perform_task` | UR arm: runs one task and waits for the task to end. |

### FTIR

| Template | Action |
|---|---|
| `ftir_open_drawer` | Opens the sample drawer. |
| `ftir_measure_background` | Measures the background, with mocked parameters. |
| `ftir_measure_sample` | Measures the sample, with mocked parameters. |
| `ftir_close_drawer` | Closes the sample drawer. |

### X3 DSC

| Template | Action |
|---|---|
| `dsc_park_autosampler_cell` | Parks the autosampler. |
| `dsc_close_lid` | Closes the autosampler lid. |
| `dsc_add_experiment` | Queues an experiment, with mocked parameters. |
| `dsc_execute_experiment` | Starts the run and waits for the run to end. |

### SDT (TGA)

| Template | Action |
|---|---|
| `tga_park_autosampler_furnace` | Parks the autosampler. |
| `tga_close_furnace` | Sends the close-furnace command and does not wait for the furnace to close. |
| `tga_add_experiment` | Queues an experiment, with mocked parameters. |
| `tga_execute_experiment` | Starts the run and waits for the run to end. |

### Workflow control

| Template | Action |
|---|---|
| `pause_until_resume` | Sends `{"state": "waiting_for_resume"}` on MQTT topic `workflow/status`, then pauses the workflow until `{"state": "resume"}` arrives on `workflow/resume`. Fails after 1 hour. |

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
