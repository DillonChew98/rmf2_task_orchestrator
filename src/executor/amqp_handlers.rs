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

use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use std::time::Duration;

use crossflow::diagram::TraceToggle;
use serde::Deserialize;

use crate::TemplateRegistry;
use crate::WorkflowTrackerHandle;
use crate::client::amqp::AmqpError;
use crate::config::{LabConfig, load_base_configuration};
use crate::executor::ExecutorHandle;
use crate::workflow_updates::{
    extract_display_names, extract_experiment_ids, extract_op_sequences, extract_section_workflows,
    extract_sub_sections,
};

const EXPERIMENT_ID_PLACEHOLDER: &str = "${section.experiment_id}";
const EXPERIMENT_CHECK_TIMEOUT: Duration = Duration::from_secs(10);

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
    let experiment_ids = extract_experiment_ids(&diagram_json);

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
    let used_ids = match fill_section_placeholders(&mut diagram_json, &experiment_ids) {
        Ok(ids) => ids,
        Err(e) => {
            tracing::warn!("Skipping workflow {}: {e}", message.task_id);
            return Err(AmqpError::Workflow(e));
        }
    };
    if let Err(e) = check_experiments(&lab, &used_ids).await {
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

// Fills `${section.experiment_id}` in a copy of each section's template.
// Copies, because sections share templates by name. Returns the IDs used.
fn fill_section_placeholders(
    json: &mut serde_json::Value,
    experiment_ids: &HashMap<String, String>,
) -> Result<HashSet<String>, String> {
    let sections: Vec<(String, String)> = json["ops"]
        .as_object()
        .into_iter()
        .flatten()
        .filter(|(_, op)| op["type"] == "section")
        .filter_map(|(name, op)| Some((name.clone(), op["template"].as_str()?.to_string())))
        .collect();
    let mut used = HashSet::new();
    for (section, template) in sections {
        let mut body = json["templates"][&template].clone();
        if !body.to_string().contains(EXPERIMENT_ID_PLACEHOLDER) {
            continue;
        }
        let id = experiment_ids
            .get(&section)
            .ok_or_else(|| format!("section {section} has no experiment_id"))?;
        replace_in_strings(&mut body, EXPERIMENT_ID_PLACEHOLDER, id);
        let copy = format!("{template}__{section}");
        json["templates"][&copy] = body;
        json["ops"][&section]["template"] = copy.into();
        used.insert(id.clone());
    }
    Ok(used)
}

fn replace_in_strings(json: &mut serde_json::Value, from: &str, to: &str) {
    match json {
        serde_json::Value::String(text) => *text = text.replace(from, to),
        serde_json::Value::Array(items) => {
            for child in items {
                replace_in_strings(child, from, to);
            }
        }
        serde_json::Value::Object(fields) => {
            for child in fields.values_mut() {
                replace_in_strings(child, from, to);
            }
        }
        _ => {}
    }
}

// Fails unless every experiment is ready.
async fn check_experiments(
    lab: &HashMap<String, String>,
    ids: &HashSet<String>,
) -> Result<(), String> {
    if ids.is_empty() {
        return Ok(());
    }
    let host = lab
        .get("experiment_server_host")
        .ok_or("lab.toml has no experiment_server_host")?;
    let client = reqwest::Client::new();
    for id in ids {
        let response = client
            .get(format!("{host}/api/experiments/{id}"))
            .timeout(EXPERIMENT_CHECK_TIMEOUT)
            .send()
            .await
            .map_err(|e| format!("experiment server: {e}"))?;
        let status = response.status();
        if !status.is_success() {
            return Err(format!("experiment {id}: {status}"));
        }
        let body: serde_json::Value = response
            .json()
            .await
            .map_err(|e| format!("experiment {id}: {e}"))?;
        if body["status"] != "ready" {
            return Err(format!("experiment {id} is {}, not ready", body["status"]));
        }
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

    #[test]
    fn test_fill_section_placeholders_copies_template_per_section() {
        let mut diagram = json!({
            "ops": {
                "dsc_a": { "type": "section", "template": "dsc_process" },
                "dsc_b": { "type": "section", "template": "dsc_process" },
                "move": { "type": "section", "template": "move" }
            },
            "templates": {
                "dsc_process": { "ops": { "fetch": { "config": {
                    "uri": "http://host/api/experiments/${section.experiment_id}"
                }}}},
                "move": { "ops": {} }
            }
        });
        let ids = HashMap::from([
            ("dsc_a".to_string(), "EXP-1".to_string()),
            ("dsc_b".to_string(), "EXP-2".to_string()),
            ("move".to_string(), "EXP-3".to_string()),
        ]);

        let used = fill_section_placeholders(&mut diagram, &ids).unwrap();

        assert_eq!(
            used,
            HashSet::from(["EXP-1".to_string(), "EXP-2".to_string()])
        );
        assert_eq!(diagram["ops"]["dsc_a"]["template"], "dsc_process__dsc_a");
        assert_eq!(diagram["ops"]["dsc_b"]["template"], "dsc_process__dsc_b");
        assert_eq!(diagram["ops"]["move"]["template"], "move");
        let uri = |copy: &str| diagram["templates"][copy]["ops"]["fetch"]["config"]["uri"].clone();
        assert_eq!(
            uri("dsc_process__dsc_a"),
            "http://host/api/experiments/EXP-1"
        );
        assert_eq!(
            uri("dsc_process__dsc_b"),
            "http://host/api/experiments/EXP-2"
        );
    }

    #[tokio::test]
    async fn test_check_experiments_needs_every_experiment_ready() {
        let app = axum::Router::new().route(
            "/api/experiments/{id}",
            axum::routing::get(
                |axum::extract::Path(id): axum::extract::Path<String>| async move {
                    match id.as_str() {
                        "EXP-1" => (axum::http::StatusCode::OK, r#"{"status":"ready"}"#),
                        "EXP-2" => (axum::http::StatusCode::OK, r#"{"status":"draft"}"#),
                        _ => (axum::http::StatusCode::NOT_FOUND, "not found"),
                    }
                },
            ),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let lab = HashMap::from([(
            "experiment_server_host".to_string(),
            format!("http://{}", listener.local_addr().unwrap()),
        )]);
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let ids = |id: &str| HashSet::from([id.to_string()]);

        assert_eq!(check_experiments(&lab, &ids("EXP-1")).await, Ok(()));
        assert_eq!(
            check_experiments(&lab, &ids("EXP-2")).await.unwrap_err(),
            r#"experiment EXP-2 is "draft", not ready"#
        );
        assert_eq!(
            check_experiments(&lab, &ids("EXP-3")).await.unwrap_err(),
            "experiment EXP-3: 404 Not Found"
        );
    }
}
