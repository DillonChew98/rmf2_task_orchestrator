# Mock workflows

A mock workflow runs a full sample job but skips the experiment. The robot takes each sample to
the instrument and back. The instrument waits 5 seconds in place of a test.

| File | Runs |
|---|---|
| `dsc_mock_workflow.json` | One DSC sample |
| `tga_mock_workflow.json` | One TGA sample |
| `ftir_mock_workflow.json` | One FTIR sample |
| `ftir_storage_mock_workflow.json` | One FTIR sample, from the storage buffer and back |
| `dsc_storage_mock_workflow.json` | One DSC sample, from the storage buffer and back |
| `tga_storage_mock_workflow.json` | One TGA sample, from the storage buffer and back |
| `gpc_mock_workflow.json` | One GPC sample |
| `combined_mock_workflow.json` | All four samples in one job |

## Workflow steps

1. The robot picks up the sample from the bench.
2. The robot puts the sample into the instrument.
3. The instrument waits 5 seconds.
4. The robot takes the sample out.
5. The robot puts the sample back on the bench.

The `_storage_` workflows use the storage buffer in place of the bench in steps 1 and 5.

Before step 1, the workflow checks that the DSC, TGA or FTIR is ready. In steps 2 and 4, the
DSC and TGA autosamplers park, and the FTIR drawer opens. The FTIR drawer closes after each step.
After step 4, the DSC lid and the TGA furnace close. The GPC has no checks and no extra steps.

## Combined workflow

One robot serves the samples in this order: GPC, DSC, TGA, FTIR. The robot picks up all four,
puts each one in, takes each one out, then puts all four back. Each instrument starts the
5-second wait once the sample is in.

## Failure cases

- If a step fails, the workflow skips the remaining steps. The other workflows go on.
- If a drive fails, the robot drives back to the charger.

## Instructions

### Setup

Point `lab.toml` at the mock server addresses. The `mock_servers` README lists those addresses.
If `lab.toml` holds the lab addresses and the machines are on, the workflows move the real MiR,
UR arm, SiLA instruments and storage buffer.

### Running (requires pika)

Pass the file name without `.json`. `--list` prints the names.

```bash
python3 scripts/send_diagram.py dsc_mock_workflow
```

Send one workflow at a time. Workflows sent together share one robot, and some steps fail.
