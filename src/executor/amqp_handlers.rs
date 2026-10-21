/*
 * Copyright (C) 2026 ROS-Industrial Consortium Asia Pacific
 * Advanced Remanufacturing and Technology Centre
 * A*STAR Research Entities (Co. Registration No. 199702110H)
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

use std::collections::HashMap;
use std::sync::Arc;

use crossflow::diagram::TraceToggle;
use serde::Deserialize;

use crate::TemplateRegistry;
use crate::WorkflowTrackerHandle;
use crate::client::amqp::AmqpError;
use crate::config::{LabConfig, load_base_configuration};
use crate::executor::ExecutorHandle;
use crate::workflow_updates::{
    extract_display_names, extract_op_sequences, extract_section_workflows, extract_sub_sections,
};

// AMQP message containing a workflow execution request.
// The workflow diagram is embedded in the payload field.
#[derive(Deserialize)]
pub struct WorkflowExecuteMessage {
    #[serde(alias = "id")]
    pub task_id: String,
    #[serde(default, alias = "type")]
    pub task_type: String,
    #[serde(default)]
    pub payload: serde_json::Value,
}

pub async fn handle_workflow_execute(
    handle: ExecutorHandle,
    registry: Arc<TemplateRegistry>,
    tracker: WorkflowTrackerHandle,
    data: Vec<u8>,
) -> Result<(), AmqpError> {
    let message: WorkflowExecuteMessage =
        serde_json::from_slice(&data).map_err(|e| AmqpError::Parse(e.to_string()))?;

    // Only process Schedule messages
    if message.task_type != "Schedule" {
        tracing::debug!("Skipping non-Schedule message type: {}", message.task_type);
        return Ok(());
    }

    tracing::info!(
        "Received AMQP workflow execute: task_id={}, task_type={}",
        message.task_id,
        message.task_type
    );

    // The payload contains the diagram JSON (either as a string or object)
    let diagram_json: serde_json::Value = if let Some(s) = message.payload.as_str() {
        serde_json::from_str(s).map_err(|e| AmqpError::Parse(format!("Payload parse: {e}")))?
    } else {
        message.payload.clone()
    };

    // Extract from the original JSON before crossflow parsing (custom fields
    // like workflow_id and op_sequence are not preserved through round-trip)
    let workflow_sections = extract_section_workflows(&diagram_json);
    let display_names = extract_display_names(&diagram_json);
    let mut op_sequences = extract_op_sequences(&diagram_json);

    // Enrich with sub-section op_sequences from the raw template files,
    // scoped as "parent_section:sub_section" to avoid collisions.
    if let Some(ops) = diagram_json.get("ops").and_then(|v| v.as_object()) {
        for (op_name, op) in ops {
            if op.get("type").and_then(|v| v.as_str()) != Some("section") {
                continue;
            }
            if let Some(tmpl_name) = op.get("template").and_then(|v| v.as_str()) {
                for (sub_name, seq) in registry.sub_section_op_sequences(tmpl_name) {
                    op_sequences.insert(format!("{op_name}:{sub_name}"), seq);
                }
            }
        }
    }

    // Parse into Diagram, inject templates from registry, serialize back
    let mut diagram = crossflow::diagram::Diagram::from_json_str(
        &serde_json::to_string(&diagram_json)
            .map_err(|e| AmqpError::Parse(format!("Diagram serialize: {e}")))?,
    )
    .map_err(|e| AmqpError::Parse(format!("Diagram parse: {e}")))?;
    if let Err(e) = registry.inject(&mut diagram) {
        tracing::warn!("Skipping workflow {}: {e}", message.task_id);
        return Err(AmqpError::Workflow(e.to_string()));
    };

    diagram.default_trace = TraceToggle::Messages;

    let mut diagram_json = serde_json::to_value(&diagram)
        .map_err(|e| AmqpError::Parse(format!("Diagram re-serialize: {e}")))?;
    let lab = load_base_configuration::<LabConfig>()
        .map_err(|e| AmqpError::Parse(format!("lab.toml: {e}")))?
        .lab;
    if let Err(e) = fill_lab_placeholders(&mut diagram_json, &lab) {
        tracing::warn!("Skipping workflow {}: {e}", message.task_id);
        return Err(AmqpError::Workflow(e));
    }

    // Extract sub_sections after re-serialization so injected templates are visible
    let sub_sections = extract_sub_sections(&diagram_json);

    for (workflow_id, sections) in workflow_sections {
        let relevant_subs: HashMap<String, Vec<String>> = sections
            .iter()
            .filter_map(|s| sub_sections.get(s).map(|v| (s.clone(), v.clone())))
            .collect();
        tracker.register(
            workflow_id,
            sections,
            relevant_subs,
            display_names.clone(),
            op_sequences.clone(),
        );
    }

    // The request is the input value passed to the workflow's start node.
    // Nodes get their config from the diagram, so we pass task metadata as context.
    let request = serde_json::json!({
        "task_id": message.task_id,
        "task_type": message.task_type,
    });

    // Forward to crossflow's /api/executor/run endpoint
    let executor_url = format!("{}/api/executor/run", handle.executor_url);
    let body = serde_json::json!({
        "diagram": diagram_json,
        "request": request,
    });

    tracing::debug!("Forwarding AMQP workflow to {}", executor_url);

    let response = reqwest::Client::new()
        .post(&executor_url)
        .json(&body)
        .send()
        .await
        .map_err(|e| AmqpError::Workflow(format!("Failed to forward to executor: {e}")))?;

    if response.status().is_success() {
        let result = response.json::<serde_json::Value>().await.ok();
        tracing::info!(
            "Workflow {} completed: {}",
            message.task_id,
            serde_json::to_string_pretty(&result).unwrap_or_default()
        );
        Ok(())
    } else {
        let status = response.status();
        let body = response.text().await.unwrap_or_default();
        tracing::error!(
            "Workflow {} failed to execute: {} - {}",
            message.task_id,
            status,
            body
        );
        let pretty = serde_json::from_str::<serde_json::Value>(&body)
            .and_then(|v| serde_json::to_string_pretty(&v))
            .unwrap_or(body);
        Err(AmqpError::Workflow(format!("{status}: {pretty}")))
    }
}

// Swaps each `${lab.NAME}` in the diagram's strings for NAME from lab.toml.
fn fill_lab_placeholders(
    json: &mut serde_json::Value,
    lab: &HashMap<String, String>,
) -> Result<(), String> {
    match json {
        serde_json::Value::String(text) => {
            while let Some(start) = text.find("${lab.") {
                let (name, _) = text[start + 6..]
                    .split_once('}')
                    .ok_or_else(|| format!("unclosed ${{lab. in {text}"))?;
                let value = lab
                    .get(name)
                    .ok_or_else(|| format!("lab.toml has no {name}"))?;
                *text = text.replace(&format!("${{lab.{name}}}"), value);
            }
        }
        serde_json::Value::Array(items) => {
            for child in items {
                fill_lab_placeholders(child, lab)?;
            }
        }
        serde_json::Value::Object(fields) => {
            for child in fields.values_mut() {
                fill_lab_placeholders(child, lab)?;
            }
        }
        _ => {}
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_fill_lab_placeholders() {
        let lab = HashMap::from([("mir_host".to_string(), "10.0.0.5".to_string())]);
        let mut diagram = json!({
            "ops": { "move": { "config": {
                "uri": "http://${lab.mir_host}/api",
                "headers": { "Authorization": "${MIR_AUTH}" }
            }}}
        });

        fill_lab_placeholders(&mut diagram, &lab).unwrap();

        let config = &diagram["ops"]["move"]["config"];
        assert_eq!(config["uri"], "http://10.0.0.5/api");
        assert_eq!(config["headers"]["Authorization"], "${MIR_AUTH}");
    }

    #[test]
    fn test_fill_lab_placeholders_rejects_unknown_name() {
        let mut diagram = json!({ "cel": "{'waypoint': '${lab.spn_waypoint}'}" });

        let err = fill_lab_placeholders(&mut diagram, &HashMap::new()).unwrap_err();

        assert_eq!(err, "lab.toml has no spn_waypoint");
    }
}
