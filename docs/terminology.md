# Terminology



| Term | What | Scheduler | Crossflow | Example |
|---|---|---|---|---|
| **Schedule** | The entire plan. It holds many Work Orders. | Schedule, with one `ScheduleID` | One diagram | [`combined_mock_workflow.json`](../diagrams/mock_workflows/combined_mock_workflow.json) |
| **Work Order** | One piece of work with a due date. Its steps run in a fixed order. | Work Order | A chain of sections | [`wo2026100001`](../diagrams/mock_workflows/combined_mock_workflow.json) |
| **Operation** | One step, such as a pick, a place or an instrument run | Operation | Section | [`place_dsc_pan_sample_tray_on_dsc`](../diagrams/marvel/place_dsc_pan_sample_tray_on_dsc.json) |
| **Action** | A smaller step inside an Operation, made of Nodes | None | Section | [`mir_move_to_waypoint`](../diagrams/marvel/mir_move_to_waypoint.json) |
| **Node** | The smallest step, such as one HTTP or gRPC call | None | Node | [`http_request`](../src/node/http.rs) |



## Schedule

[`combined_mock_workflow.json`](../diagrams/mock_workflows/combined_mock_workflow.json) has 4
Work Orders, one for each lab instrument: `wo2026100001` (DSC), `wo2026100002` (FTIR),
`wo2026100003` (TGA) and `wo2026100004` (GPC).

```
┌─ Schedule  combined_mock_workflow.json ──────────────────────────────┐
│ ┌─ Work Order  wo2026100001  DSC ──────────────────────────────────┐ │
│ │ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │ │
│ │ │ op1 pick │ │ op2 place│ │ op3 run  │ │ op4 pick │ │ op5 place│ │ │
│ │ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘ │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│ ┌─ Work Order  wo2026100002  FTIR ─────────────────────────────────┐ │
│ │ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │ │
│ │ │ op1 pick │ │ op2 place│ │ op3 run  │ │ op4 pick │ │ op5 place│ │ │
│ │ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘ │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│ ┌─ Work Order  wo2026100003  TGA ──────────────────────────────────┐ │
│ │ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │ │
│ │ │ op1 pick │ │ op2 place│ │ op3 run  │ │ op4 pick │ │ op5 place│ │ │
│ │ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘ │ │
│ └──────────────────────────────────────────────────────────────────┘ │
│ ┌─ Work Order  wo2026100004  GPC ──────────────────────────────────┐ │
│ │ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ │ │
│ │ │ op1 pick │ │ op2 place│ │ op3 run  │ │ op4 pick │ │ op5 place│ │ │
│ │ └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘ │ │
│ └──────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────────┘
```

## Work Order

Work Order `wo2026100001` takes a DSC sample tray from the tray station to the DSC, runs a mock
DSC process, and brings the tray back.

```
           step  section template
Operation  op1   pick_dsc_pan_sample_tray_from_tray_station
 ▼
Operation  op2   place_dsc_pan_sample_tray_on_dsc
 ▼
Operation  op3   dsc_mock_process
 ▼
Operation  op4   pick_dsc_pan_sample_tray_from_dsc
 ▼
Operation  op5   place_dsc_pan_sample_tray_on_tray_station
```

## Operation

Operation `op2` (`place_dsc_pan_sample_tray_on_dsc`) drives the MiR (mobile robot) to the DSC,
parks the DSC's autosampler (sample loader), then the UR (robot arm) places the tray on the DSC.

```
        name   action template
Action  move   mir_move_to_waypoint
 ▼
Action  park   dsc_park_autosampler_cell
 ▼
Action  place  ur_perform_task
```

## Action

Action `move` (`mir_move_to_waypoint`) checks that the MiR is free, sends it to the waypoint,
and waits until it arrives.

```
            name                  builder        what
      Node  read_robot_status     http_request   query MiR status
       ▼
      Node  is_robot_free         cel_condition  is the MiR idle or at charger?
       ⋮
      Node  send_move             http_request   create mission to send the MiR to a waypoint
       ▼
  ┌─▶ Node  read_mission_state    http_request   check on the progress of the mission
  │    ▼
  │   Node  is_mission_finished   cel_condition  has the mission ended? ── yes ──▶ done
  │    ▼ not yet
  └── Node  wait_then_poll_again  timeout        wait 5 s, then check again (stop after 2 min)
```

## Node

Node `send_move` (`http_request`) creates a mission to send the MiR to the waypoint. It sits in
[`mir_move_to_waypoint.json`](../diagrams/marvel/mir_move_to_waypoint.json).

```jsonc
"send_move": {
  "type": "node",                                        // a Node
  "builder": "http_request",                             // the code it runs: src/node/http.rs
  "display_text": "POST /mission_queue: send the Move",
  "config": {                                            // the request
    "method": "POST",
    "uri": "${lab.mir_host}/api/v2.0.0/mission_queue",   // mir_host from lab.toml
    "headers": {
      "Accept": "application/json",
      "Authorization": "${MIR_AUTH}",                    // from an env var
      "Accept-Language": "en_US"
    },
    "retry": {}                                          // default retry settings
  },
  "next": "if_move_sent"                                 // gets the reply
}
```

