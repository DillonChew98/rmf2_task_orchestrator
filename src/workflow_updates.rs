use std::collections::HashMap;
use std::convert::Infallible;
use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::State;
use axum::response::sse::{Event, KeepAlive, Sse};
use axum::routing::get;
use std::collections::HashSet;

use crossflow::{
    OperationRef, bevy_app, bevy_ecs,
    trace::{MessageSent, TracedEvent, TracedEventKind},
};
use serde::Serialize;
use tokio::sync::{broadcast, mpsc};
use tokio_stream::StreamExt;
use tokio_stream::wrappers::BroadcastStream;

pub type WorkflowId = String;
pub type SectionId = String;

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SectionStatus {
    Pending,
    Running,
    Completed,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkflowEvent {
    pub workflow_id: WorkflowId,
    pub section_id: SectionId,
    pub status: SectionStatus,
    pub timestamp: u64,
}

pub struct WorkflowRegistration {
    pub workflow_id: WorkflowId,
    pub sections: Vec<SectionId>,
}

struct WorkflowState {
    sections: HashMap<SectionId, SectionStatus>,
}

#[derive(bevy_ecs::prelude::Resource)]
struct TraceSender(mpsc::UnboundedSender<TracedEvent>);

fn trace_forwarder(
    trigger: bevy_ecs::prelude::Trigger<TracedEvent>,
    sender: bevy_ecs::system::Res<TraceSender>,
) {
    let _ = sender.0.send(trigger.event().clone());
}

pub fn setup_trace_forwarding(
    app: &mut bevy_app::App,
    trace_tx: mpsc::UnboundedSender<TracedEvent>,
) {
    app.insert_resource(TraceSender(trace_tx));
    app.world_mut().add_observer(trace_forwarder);
}

pub fn extract_section_workflows(
    diagram_json: &serde_json::Value,
) -> HashMap<WorkflowId, Vec<SectionId>> {
    let mut map: HashMap<WorkflowId, Vec<SectionId>> = HashMap::new();
    let Some(ops) = diagram_json.get("ops").and_then(|v| v.as_object()) else {
        return map;
    };
    for (name, op) in ops {
        if op.get("type").and_then(|v| v.as_str()) != Some("section") {
            continue;
        }
        let Some(wf_id) = op.get("workflow_id").and_then(|v| v.as_str()) else {
            continue;
        };
        map.entry(wf_id.to_string()).or_default().push(name.clone());
    }
    map
}

pub struct WorkflowTracker {
    workflows: HashMap<WorkflowId, WorkflowState>,
    section_name_to_workflow: HashMap<SectionId, WorkflowId>,
    sections_running: HashSet<SectionId>,
    broadcast_tx: broadcast::Sender<WorkflowEvent>,
    register_rx: mpsc::UnboundedReceiver<WorkflowRegistration>,
}

#[derive(Clone)]
pub struct WorkflowTrackerHandle {
    register_tx: mpsc::UnboundedSender<WorkflowRegistration>,
    broadcast_tx: broadcast::Sender<WorkflowEvent>,
}

impl WorkflowTrackerHandle {
    pub fn register(&self, workflow_id: WorkflowId, sections: Vec<SectionId>) {
        let _ = self.register_tx.send(WorkflowRegistration {
            workflow_id,
            sections,
        });
    }

    pub fn subscribe(&self) -> broadcast::Receiver<WorkflowEvent> {
        self.broadcast_tx.subscribe()
    }
}

impl WorkflowTracker {
    pub fn new(broadcast_capacity: usize) -> (Self, WorkflowTrackerHandle) {
        let (broadcast_tx, _) = broadcast::channel(broadcast_capacity);
        let (register_tx, register_rx) = mpsc::unbounded_channel();

        let tracker = Self {
            workflows: HashMap::new(),
            section_name_to_workflow: HashMap::new(),
            sections_running: HashSet::new(),
            broadcast_tx: broadcast_tx.clone(),
            register_rx,
        };

        let handle = WorkflowTrackerHandle {
            register_tx,
            broadcast_tx,
        };

        (tracker, handle)
    }

    pub async fn run(mut self, mut trace_rx: mpsc::UnboundedReceiver<TracedEvent>) {
        loop {
            tokio::select! {
                Some(registration) = self.register_rx.recv() => {
                    self.handle_registration(registration);
                }
                Some(event) = trace_rx.recv() => {
                    self.handle_trace_event(event);
                }
                else => break,
            }
        }
        tracing::warn!("WorkflowTracker shutting down");
    }

    fn handle_registration(&mut self, registration: WorkflowRegistration) {
        tracing::debug!(
            "Registered workflow {} with sections: {:?}",
            registration.workflow_id,
            registration.sections
        );

        let mut sections = HashMap::new();
        for section_id in &registration.sections {
            sections.insert(section_id.clone(), SectionStatus::Pending);
            self.section_name_to_workflow
                .insert(section_id.clone(), registration.workflow_id.clone());
            self.broadcast_event(
                registration.workflow_id.clone(),
                section_id.clone(),
                SectionStatus::Pending,
            );
        }
        self.workflows
            .insert(registration.workflow_id, WorkflowState { sections });
    }

    fn handle_trace_event(&mut self, event: TracedEvent) {
        match &event.event {
            TracedEventKind::MessageSent(msg) => {
                self.handle_message_sent(msg);
            }
            _ => {}
        }
    }

    fn extract_section_from_ref<'a>(&self, op_ref: &'a OperationRef) -> Option<&'a str> {
        match op_ref {
            OperationRef::Named(named) => {
                let ns = named.namespaces.0.first()?;
                if self.section_name_to_workflow.contains_key(ns.as_ref()) {
                    Some(ns.as_ref())
                } else {
                    None
                }
            }
            _ => None,
        }
    }

    fn section_from_info(
        &self,
        info: &Option<std::sync::Arc<crossflow::trace::OperationInfo>>,
    ) -> Option<String> {
        let op_ref = info.as_ref()?.id().as_ref()?;
        self.extract_section_from_ref(op_ref).map(|s| s.to_string())
    }

    fn handle_message_sent(&mut self, msg: &MessageSent) {
        let input_section = self.section_from_info(&msg.input.info);

        // Running: first message entering a section
        if let Some(ref section_name) = input_section {
            if !self.sections_running.contains(section_name.as_str()) {
                if let Some(workflow_id) = self
                    .section_name_to_workflow
                    .get(section_name.as_str())
                    .cloned()
                {
                    self.sections_running.insert(section_name.clone());
                    if let Some(state) = self.workflows.get_mut(&workflow_id) {
                        state
                            .sections
                            .insert(section_name.clone(), SectionStatus::Running);
                    }
                    self.broadcast_event(workflow_id, section_name.clone(), SectionStatus::Running);
                }
            }
        }

        // Completed: message leaving a section (output source is in a section, input target is not in the same section)
        for source in &msg.output {
            let Some(source_section) = self.section_from_info(&source.info) else {
                continue;
            };
            if !self.sections_running.contains(source_section.as_str()) {
                continue;
            }
            let same_section = input_section.as_deref() == Some(source_section.as_str());
            if same_section {
                continue;
            }
            let Some(workflow_id) = self
                .section_name_to_workflow
                .get(source_section.as_str())
                .cloned()
            else {
                continue;
            };
            self.sections_running.remove(source_section.as_str());
            if let Some(state) = self.workflows.get_mut(&workflow_id) {
                state
                    .sections
                    .insert(source_section.clone(), SectionStatus::Completed);
            }
            self.broadcast_event(
                workflow_id,
                source_section.clone(),
                SectionStatus::Completed,
            );
        }
    }

    fn broadcast_event(
        &self,
        workflow_id: WorkflowId,
        section_id: SectionId,
        status: SectionStatus,
    ) {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        tracing::debug!(
            "[workflow:{}] section '{}' = {:?}",
            workflow_id,
            section_id,
            status
        );

        let event = WorkflowEvent {
            workflow_id,
            section_id,
            status,
            timestamp,
        };
        let _ = self.broadcast_tx.send(event);
    }
}

async fn sse_workflow_events(
    State(handle): State<WorkflowTrackerHandle>,
) -> Sse<impl futures_lite::Stream<Item = Result<Event, Infallible>>> {
    let rx = handle.subscribe();
    let stream = BroadcastStream::new(rx).filter_map(|result| match result {
        Ok(event) => {
            let data = serde_json::to_string(&event).ok()?;
            Some(Ok(Event::default().event("workflow_status").data(data)))
        }
        Err(_) => None,
    });
    Sse::new(stream).keep_alive(KeepAlive::default())
}

pub fn workflow_events_router(handle: WorkflowTrackerHandle) -> axum::Router {
    axum::Router::new()
        .route("/api/workflows/events", get(sse_workflow_events))
        .with_state(handle)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossflow::{
        bevy_ecs::prelude::Entity,
        diagram::{NamedOperationRef, NamespaceList},
        re_exports::smallvec,
        trace::{OperationInfo, TraceSource, TraceTarget},
    };
    use std::sync::Arc;

    fn make_entity(index: u32) -> Entity {
        Entity::from_raw(index)
    }

    fn namespaced_op_info(section: &str, op: &str) -> Option<Arc<OperationInfo>> {
        Some(Arc::new(OperationInfo::new(
            Some(OperationRef::Named(NamedOperationRef {
                namespaces: NamespaceList(smallvec![section.into()]),
                exposed_namespace: None,
                name: op.into(),
            })),
            None,
            None,
        )))
    }

    fn message_entering_section(section: &str, op: &str) -> TracedEvent {
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: namespaced_op_info(section, op),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![],
            input,
            message: None,
        }))
    }

    fn message_leaving_section(
        from_section: &str,
        from_op: &str,
        to_section: &str,
        to_op: &str,
    ) -> TracedEvent {
        let source = TraceSource {
            session_stack: smallvec![make_entity(1)],
            source: make_entity(98),
            seq: 0,
            port: smallvec![],
            labels: None,
            operation_type: "test".into(),
            info: namespaced_op_info(from_section, from_op),
        };
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: namespaced_op_info(to_section, to_op),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![source],
            input,
            message: None,
        }))
    }

    async fn collect_events(
        rx: &mut broadcast::Receiver<WorkflowEvent>,
        count: usize,
    ) -> Vec<WorkflowEvent> {
        let mut events = Vec::new();
        for _ in 0..count {
            match tokio::time::timeout(std::time::Duration::from_millis(100), rx.recv()).await {
                Ok(Ok(e)) => events.push(e),
                _ => break,
            }
        }
        events
    }

    #[tokio::test]
    async fn registration_broadcasts_pending() {
        let (tracker, handle) = WorkflowTracker::new(64);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register("wf-1".into(), vec!["pickup".into(), "dropoff".into()]);

        drop(trace_tx);

        let events = collect_events(&mut sub, 2).await;
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].workflow_id, "wf-1");
        assert_eq!(events[0].status, SectionStatus::Pending);
        assert_eq!(events[1].workflow_id, "wf-1");
        assert_eq!(events[1].status, SectionStatus::Pending);

        let section_ids: Vec<&str> = events.iter().map(|e| e.section_id.as_str()).collect();
        assert!(section_ids.contains(&"pickup"));
        assert!(section_ids.contains(&"dropoff"));
    }

    #[tokio::test]
    async fn full_section_lifecycle() {
        let (tracker, handle) = WorkflowTracker::new(64);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register("wf-1".into(), vec!["pickup".into()]);
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("pickup", "start"))
            .unwrap();
        trace_tx
            .send(message_leaving_section(
                "pickup", "done", "dropoff", "start",
            ))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 3).await;
        assert_eq!(events.len(), 3);
        assert_eq!(events[0].status, SectionStatus::Pending);
        assert_eq!(events[1].status, SectionStatus::Running);
        assert_eq!(events[1].section_id, "pickup");
        assert_eq!(events[2].status, SectionStatus::Completed);
        assert_eq!(events[2].section_id, "pickup");
    }

    #[tokio::test]
    async fn ignores_non_section_messages() {
        let (tracker, handle) = WorkflowTracker::new(64);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register("wf-1".into(), vec!["pickup".into()]);
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("unknown_section", "start"))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 2).await;
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].status, SectionStatus::Pending);
    }

    #[tokio::test]
    async fn multiple_workflows_tracked_independently() {
        let (tracker, handle) = WorkflowTracker::new(64);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register("wf-a".into(), vec!["wf_a_op1".into()]);
        handle.register("wf-b".into(), vec!["wf_b_op1".into()]);
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("wf_a_op1", "start"))
            .unwrap();
        trace_tx
            .send(message_entering_section("wf_b_op1", "start"))
            .unwrap();
        trace_tx
            .send(message_leaving_section(
                "wf_a_op1", "done", "wf_a_op2", "start",
            ))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 10).await;

        let wf_a_events: Vec<_> = events.iter().filter(|e| e.workflow_id == "wf-a").collect();
        let wf_b_events: Vec<_> = events.iter().filter(|e| e.workflow_id == "wf-b").collect();

        assert_eq!(wf_a_events.len(), 3);
        assert_eq!(wf_a_events[0].status, SectionStatus::Pending);
        assert_eq!(wf_a_events[1].status, SectionStatus::Running);
        assert_eq!(wf_a_events[2].status, SectionStatus::Completed);

        assert_eq!(wf_b_events.len(), 2);
        assert_eq!(wf_b_events[0].status, SectionStatus::Pending);
        assert_eq!(wf_b_events[1].status, SectionStatus::Running);
    }

    #[test]
    fn extract_section_workflows_groups_by_workflow_id() {
        let diagram = serde_json::json!({
            "ops": {
                "wo1_op1": { "type": "section", "template": "pick", "workflow_id": "wo1" },
                "wo1_op2": { "type": "section", "template": "place", "workflow_id": "wo1" },
                "wo2_op1": { "type": "section", "template": "pick", "workflow_id": "wo2" },
                "run_started": { "type": "transform", "cel": "message" },
                "some_buffer": { "type": "buffer" }
            }
        });

        let result = extract_section_workflows(&diagram);
        assert_eq!(result.len(), 2);
        assert_eq!(result["wo1"].len(), 2);
        assert!(result["wo1"].contains(&"wo1_op1".to_string()));
        assert!(result["wo1"].contains(&"wo1_op2".to_string()));
        assert_eq!(result["wo2"], vec!["wo2_op1".to_string()]);
    }

    #[test]
    fn extract_section_workflows_skips_sections_without_workflow_id() {
        let diagram = serde_json::json!({
            "ops": {
                "tagged": { "type": "section", "template": "x", "workflow_id": "wo1" },
                "untagged": { "type": "section", "template": "y" }
            }
        });

        let result = extract_section_workflows(&diagram);
        assert_eq!(result.len(), 1);
        assert_eq!(result["wo1"], vec!["tagged".to_string()]);
    }
}
