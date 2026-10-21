# marvel/

`TemplateRegistry` (`src/template_registry.rs`) loads every `.json` file in this folder at
startup. Point `[app] template_directory` in `config.toml` here. For each diagram that
arrives over AMQP, the registry adds the templates the diagram names, plus the templates
those templates use.

Each file holds one template, as `{"templates": {"<name>": <body>}}`. The registry ignores
other keys.

The key inside `"templates"` must match the `builder` value the schedule adapter sends. The
adapter's `services/dag_converter/registry.json` maps each APS operation name to one `builder`.
For example, "Pick GPC vials from SPM" maps to `pick_gpc_vials_from_spm`, so a file here must
hold `{"templates": {"pick_gpc_vials_from_spm": ...}}`.

Name each file after the key it holds, as in `pick_gpc_vials_from_spm.json`. The registry reads
the key, not the file name.

FTIR workflow:
- `pick_ftir_pan_sample_tray_from_spm.json`
- `place_ftir_pan_sample_tray_on_ftir.json`
- `ftir_process.json`
- `pick_ftir_pan_sample_tray_from_ftir.json`
- `place_ftir_pan_sample_tray_on_workbench.json`

TGA workflow:
- `pick_tga_pan_sample_tray_from_spm.json`
- `place_tga_pan_sample_tray_on_tga.json`
- `tga_process.json`
- `pick_tga_pan_sample_tray_from_tga.json`
- `place_tga_pan_sample_tray_on_workbench.json`

DSC workflow:
- `pick_dsc_pan_sample_tray_from_spm.json`
- `place_dsc_pan_sample_tray_on_dsc.json`
- `dsc_process.json`
- `pick_dsc_pan_sample_tray_from_dsc.json`
- `place_dsc_pan_sample_tray_on_workbench.json`

DSC steps with a pause before the UR arm moves. To use one, point the adapter's registry at it:
- `pick_dsc_pan_sample_tray_from_spm_with_pause.json`
- `place_dsc_pan_sample_tray_on_dsc_with_pause.json`

GPC workflow. The lab has no GPC instrument box, so this folder has `mock_gpc_process` and no
`gpc_process`:
- `pick_gpc_vials_from_spm.json`
- `place_gpc_vials_at_gpc.json`
- `pick_gpc_vials_from_gpc.json`
- `place_gpc_vials_on_workbench.json`

Mocks (`mock_<x>_process.json`) wait 5 s, then report ok. Point the adapter's registry at
`mock_<x>_process` in place of `<x>_process` to skip the real run.

**Action Templates** (each drives one robot or instrument):

| Template | What it does |
|---|---|
| `mir_move_to_waypoint` | MiR: checks the arm is in safe position, drives to a waypoint and waits for arrival. On failure, clears the mission queue and drives to the charger. |
| `ur_perform_task` | UR arm: runs one task and waits for it to end. |
| `ftir_open_drawer` | FTIR: opens the sample drawer. |
| `ftir_measure_background` | FTIR: measures the background, with mocked parameters. |
| `ftir_measure_sample` | FTIR: measures the sample, with mocked parameters. |
| `ftir_close_drawer` | FTIR: closes the sample drawer. |
| `dsc_park_autosampler_cell` | X3 DSC: parks the autosampler. |
| `dsc_close_lid` | X3 DSC: closes the autosampler lid. |
| `dsc_add_experiment` | X3 DSC: queues an experiment, with mocked parameters. |
| `dsc_execute_experiment` | X3 DSC: starts the run and waits for it to end. |
| `tga_park_autosampler_furnace` | SDT (TGA): parks the autosampler. |
| `tga_close_furnace` | SDT (TGA): sends the close-furnace command and does not wait for it. |
| `tga_add_experiment` | SDT (TGA): queues an experiment, with mocked parameters. |
| `tga_execute_experiment` | SDT (TGA): starts the run and waits for it to end. |
| `pause_until_resume` | Sends `{"state": "waiting_for_resume"}` on MQTT topic `workflow/status`, then pauses the workflow until `{"state": "resume"}` arrives on `workflow/resume`. Fails after 1 hour. |

## DSC pause 

Two DSC diagrams has a pause. 

1. Watch the workflow topics:

   ```bash
   docker compose exec mosquitto mosquitto_sub -t 'workflow/#' -v
   ```

2. When `workflow/status {"state":"waiting_for_resume"}` shows, resume with:

   ```bash
   docker compose exec mosquitto mosquitto_pub -t workflow/resume -m '{"state":"resume"}'
   ```


