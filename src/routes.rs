use std::collections::HashMap;
use std::convert::Infallible;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::routing::get;
use serde::Deserialize;
use tokio_stream::StreamExt;
use tokio_stream::wrappers::BroadcastStream;
use utoipa::OpenApi;

use crate::workflow_updates::{
    SectionSnapshot, SectionState, SectionStatus, SubSectionSnapshot, WorkflowId, WorkflowSnapshot,
    WorkflowTrackerHandle, sections_to_sorted_vec,
};

fn apply_event_to_state(
    state: &mut HashMap<WorkflowId, HashMap<String, SectionState>>,
    event: &crate::workflow_updates::WorkflowEvent,
) {
    let workflow = state.entry(event.workflow_id.clone()).or_default();
    match &event.parent_section_id {
        Some(parent) => {
            let section = workflow.entry(parent.clone()).or_default();
            let sub = section
                .sub_sections
                .entry(event.section_id.clone())
                .or_default();
            sub.status = event.status.clone();
            if event.display_name.is_some() {
                sub.display_name = event.display_name.clone();
            }
            if event.op_sequence.is_some() {
                sub.op_sequence = event.op_sequence;
            }
        }
        None => {
            let section = workflow.entry(event.section_id.clone()).or_default();
            section.status = event.status.clone();
            if event.display_name.is_some() {
                section.display_name = event.display_name.clone();
            }
            if event.op_sequence.is_some() {
                section.op_sequence = event.op_sequence;
            }
        }
    }
}

#[utoipa::path(
    get,
    path = "/health_check",
    responses((status = 200, description = "Service is up"))
)]
async fn health_check() -> StatusCode {
    StatusCode::OK
}

#[utoipa::path(
    get,
    path = "/api/workflows/events",
    responses(
        (status = 200, content_type = "text/event-stream", body = WorkflowSnapshot,
         description = "SSE stream of `workflow_status` events, each carrying a WorkflowSnapshot as JSON"),
    )
)]
async fn sse_workflow_events(
    State(handle): State<WorkflowTrackerHandle>,
) -> Sse<impl futures_lite::Stream<Item = Result<Event, Infallible>>> {
    let rx = handle.subscribe();
    let mut internal_state: HashMap<WorkflowId, HashMap<String, SectionState>> = HashMap::new();
    let stream = BroadcastStream::new(rx).filter_map(move |result| match result {
        Ok(event) => {
            apply_event_to_state(&mut internal_state, &event);
            let sections = internal_state
                .get(&event.workflow_id)
                .map(|s| sections_to_sorted_vec(s))
                .unwrap_or_default();
            let snapshot = WorkflowSnapshot {
                workflow_id: event.workflow_id.clone(),
                sections,
            };
            let data = serde_json::to_string(&snapshot).ok()?;
            Some(Ok(Event::default().event("workflow_status").data(data)))
        }
        Err(_) => None,
    });
    Sse::new(stream).keep_alive(KeepAlive::default())
}

#[derive(Deserialize, utoipa::IntoParams)]
struct WorkflowStatusParams {
    workflow_id: Option<String>,
}

#[utoipa::path(
    get,
    path = "/api/workflows/status",
    params(WorkflowStatusParams),
    responses(
        (status = 200, body = Vec<WorkflowSnapshot>),
        (status = 503, description = "No database configured"),
    )   
)]
async fn get_workflow_status(
    State(handle): State<WorkflowTrackerHandle>,
    Query(params): Query<WorkflowStatusParams>,
) -> Result<axum::Json<Vec<WorkflowSnapshot>>, StatusCode> {
    let Some(db) = handle.db() else {
        return Err(StatusCode::SERVICE_UNAVAILABLE);
    };

    let rows: Vec<(String, String, String, String)> = if let Some(ref wf_id) = params.workflow_id {
        sqlx::query_as(
            "SELECT workflow_id, section_id, parent_section_id, status \
             FROM workflow_sections WHERE workflow_id = $1 \
             ORDER BY workflow_id, parent_section_id, section_id",
        )
        .bind(wf_id)
        .fetch_all(db)
        .await
    } else {
        sqlx::query_as(
            "SELECT workflow_id, section_id, parent_section_id, status \
             FROM workflow_sections \
             ORDER BY workflow_id, parent_section_id, section_id",
        )
        .fetch_all(db)
        .await
    }
    .map_err(|e| {
        tracing::error!("Failed to query workflow status: {e}");
        StatusCode::INTERNAL_SERVER_ERROR
    })?;

    let mut workflows: HashMap<String, HashMap<String, SectionState>> = HashMap::new();
    for (wf_id, sec_id, parent_id, status) in rows {
        let workflow = workflows.entry(wf_id).or_default();
        let parsed = SectionStatus::from_db(&status);
        if parent_id.is_empty() {
            let section = workflow.entry(sec_id.clone()).or_default();
            section.status = parsed;
            section.display_name = handle.display_name(&sec_id);
            section.op_sequence = handle.op_sequence(&sec_id);
        } else {
            let section = workflow.entry(parent_id).or_default();
            let sub = section.sub_sections.entry(sec_id.clone()).or_default();
            sub.status = parsed;
            sub.display_name = handle.display_name(&sec_id);
            sub.op_sequence = handle.op_sequence(&sec_id);
        }
    }

    let snapshots: Vec<WorkflowSnapshot> = workflows
        .into_iter()
        .map(|(workflow_id, sections)| WorkflowSnapshot {
            workflow_id,
            sections: sections_to_sorted_vec(&sections),
        })
        .collect();

    Ok(axum::Json(snapshots))
}

#[derive(OpenApi)]
#[openapi(
    paths(health_check, sse_workflow_events, get_workflow_status),
    components(schemas(WorkflowSnapshot, SectionSnapshot, SubSectionSnapshot, SectionStatus))
)]
struct WorkflowApi;

pub fn workflow_events_router(handle: WorkflowTrackerHandle) -> axum::Router {
    let swagger = utoipa_swagger_ui::SwaggerUi::new("/swagger-ui")
        .url("/api-docs/openapi.json", WorkflowApi::openapi());

    axum::Router::new()
        .route("/api/workflows/events", get(sse_workflow_events))
        .route("/api/workflows/status", get(get_workflow_status))
        .route("/health_check", get(health_check))
        .merge(swagger)
        .with_state(handle)
}
