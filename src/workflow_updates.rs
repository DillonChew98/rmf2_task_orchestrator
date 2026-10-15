use std::collections::HashMap;
use std::collections::HashSet;
use std::time::{SystemTime, UNIX_EPOCH};

use crossflow::{
    OperationRef, bevy_app, bevy_ecs,
    trace::{MessageSent, TracedEvent, TracedEventKind},
};
use serde::Serialize;
use tokio::sync::{broadcast, mpsc};

pub type WorkflowId = String;
pub type SectionId = String;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Default, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum SectionStatus {
    #[default]
    Pending,
    Running,
    Completed,
}

impl SectionStatus {
    fn as_str(&self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Running => "running",
            Self::Completed => "completed",
        }
    }

    pub fn from_db(s: &str) -> Self {
        match s {
            "running" => Self::Running,
            "completed" => Self::Completed,
            _ => Self::Pending,
        }
    }
}

#[derive(Debug, PartialEq)]
enum TargetKind {
    RegisteredSection(String),
    TopLevel,
    Internal,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkflowEvent {
    pub workflow_id: WorkflowId,
    pub section_id: SectionId,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_section_id: Option<SectionId>,
    pub status: SectionStatus,
    pub timestamp: u64,
}

#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct WorkflowSnapshot {
    pub workflow_id: WorkflowId,
    pub sections: HashMap<SectionId, SectionSnapshot>,
}

#[derive(Debug, Clone, Serialize, Default, utoipa::ToSchema)]
pub struct SectionSnapshot {
    pub status: SectionStatus,
    #[serde(skip_serializing_if = "HashMap::is_empty")]
    pub sub_sections: HashMap<SectionId, SectionStatus>,
}

pub struct WorkflowRegistration {
    pub workflow_id: WorkflowId,
    pub sections: Vec<SectionId>,
    pub sub_sections: HashMap<SectionId, Vec<SectionId>>,
}

struct WorkflowState {
    sections: HashMap<SectionId, SectionStatus>,
    sub_sections: HashMap<(SectionId, SectionId), SectionStatus>,
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

pub fn extract_sub_sections(
    diagram_json: &serde_json::Value,
) -> HashMap<SectionId, Vec<SectionId>> {
    let mut result: HashMap<SectionId, Vec<SectionId>> = HashMap::new();
    let Some(ops) = diagram_json.get("ops").and_then(|v| v.as_object()) else {
        return result;
    };
    let templates = diagram_json.get("templates").and_then(|v| v.as_object());
    for (section_name, op) in ops {
        if op.get("type").and_then(|v| v.as_str()) != Some("section") {
            continue;
        }
        let Some(template_name) = op.get("template").and_then(|v| v.as_str()) else {
            continue;
        };
        let Some(templates) = templates else {
            continue;
        };
        let Some(template) = templates.get(template_name) else {
            continue;
        };
        let Some(template_ops) = template.get("ops").and_then(|v| v.as_object()) else {
            continue;
        };
        let mut subs = Vec::new();
        for (sub_name, sub_op) in template_ops {
            if sub_op.get("type").and_then(|v| v.as_str()) == Some("section") {
                subs.push(sub_name.clone());
            }
        }
        if !subs.is_empty() {
            result.insert(section_name.clone(), subs);
        }
    }
    result
}

pub struct WorkflowTracker {
    workflows: HashMap<WorkflowId, WorkflowState>,
    section_name_to_workflow: HashMap<SectionId, WorkflowId>,
    sections_running: HashSet<SectionId>,
    sub_section_registry: HashSet<(SectionId, String)>,
    sub_sections_running: HashSet<(SectionId, String)>,
    broadcast_tx: broadcast::Sender<WorkflowEvent>,
    register_rx: mpsc::UnboundedReceiver<WorkflowRegistration>,
    db: Option<sqlx::PgPool>,
}

#[derive(Clone)]
pub struct WorkflowTrackerHandle {
    register_tx: mpsc::UnboundedSender<WorkflowRegistration>,
    broadcast_tx: broadcast::Sender<WorkflowEvent>,
    db: Option<sqlx::PgPool>,
}

impl WorkflowTrackerHandle {
    pub fn register(
        &self,
        workflow_id: WorkflowId,
        sections: Vec<SectionId>,
        sub_sections: HashMap<SectionId, Vec<SectionId>>,
    ) {
        let _ = self.register_tx.send(WorkflowRegistration {
            workflow_id,
            sections,
            sub_sections,
        });
    }

    pub fn subscribe(&self) -> broadcast::Receiver<WorkflowEvent> {
        self.broadcast_tx.subscribe()
    }

    pub fn db(&self) -> Option<&sqlx::PgPool> {
        self.db.as_ref()
    }
}

impl WorkflowTracker {
    pub fn new(
        broadcast_capacity: usize,
        db: Option<sqlx::PgPool>,
    ) -> (Self, WorkflowTrackerHandle) {
        let (broadcast_tx, _) = broadcast::channel(broadcast_capacity);
        let (register_tx, register_rx) = mpsc::unbounded_channel();

        let tracker = Self {
            workflows: HashMap::new(),
            section_name_to_workflow: HashMap::new(),
            sections_running: HashSet::new(),
            sub_section_registry: HashSet::new(),
            sub_sections_running: HashSet::new(),
            broadcast_tx: broadcast_tx.clone(),
            register_rx,
            db: db.clone(),
        };

        let handle = WorkflowTrackerHandle {
            register_tx,
            broadcast_tx,
            db,
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
            "Registered workflow {} with sections: {:?}, sub_sections: {:?}",
            registration.workflow_id,
            registration.sections,
            registration.sub_sections,
        );

        let mut sections = HashMap::new();
        let mut sub_sections = HashMap::new();

        for section_id in &registration.sections {
            sections.insert(section_id.clone(), SectionStatus::Pending);
            self.section_name_to_workflow
                .insert(section_id.clone(), registration.workflow_id.clone());
            self.broadcast_event(
                registration.workflow_id.clone(),
                section_id.clone(),
                None,
                SectionStatus::Pending,
            );

            if let Some(subs) = registration.sub_sections.get(section_id) {
                for sub_id in subs {
                    self.sub_section_registry
                        .insert((section_id.clone(), sub_id.clone()));
                    sub_sections
                        .insert((section_id.clone(), sub_id.clone()), SectionStatus::Pending);
                    self.broadcast_event(
                        registration.workflow_id.clone(),
                        sub_id.clone(),
                        Some(section_id.clone()),
                        SectionStatus::Pending,
                    );
                }
            }
        }
        self.workflows.insert(
            registration.workflow_id,
            WorkflowState {
                sections,
                sub_sections,
            },
        );
    }

    fn handle_trace_event(&mut self, event: TracedEvent) {
        if let TracedEventKind::MessageSent(msg) = &event.event {
            self.handle_message_sent(msg);
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

    fn classify_target(
        &self,
        info: &Option<std::sync::Arc<crossflow::trace::OperationInfo>>,
    ) -> TargetKind {
        let Some(op_info) = info.as_ref() else {
            return TargetKind::Internal;
        };
        let Some(op_ref) = op_info.id().as_ref() else {
            return TargetKind::Internal;
        };
        match op_ref {
            OperationRef::Named(named) => match named.namespaces.0.first() {
                Some(ns) if self.section_name_to_workflow.contains_key(ns.as_ref()) => {
                    TargetKind::RegisteredSection(ns.to_string())
                }
                Some(_) => TargetKind::Internal,
                None => TargetKind::TopLevel,
            },
            _ => TargetKind::Internal,
        }
    }

    fn sub_section_from_info(
        &self,
        info: &Option<std::sync::Arc<crossflow::trace::OperationInfo>>,
    ) -> Option<(String, String)> {
        let op_ref = info.as_ref()?.id().as_ref()?;
        match op_ref {
            OperationRef::Named(named) => {
                let ns = &named.namespaces.0;
                if ns.len() < 2 {
                    return None;
                }
                let parent = ns[0].as_ref().to_string();
                let child = ns[1].as_ref().to_string();
                if self
                    .sub_section_registry
                    .contains(&(parent.clone(), child.clone()))
                {
                    Some((parent, child))
                } else {
                    None
                }
            }
            _ => None,
        }
    }

    fn input_is_named(info: &Option<std::sync::Arc<crossflow::trace::OperationInfo>>) -> bool {
        info.as_ref()
            .and_then(|i| i.id().as_ref())
            .is_some_and(|r| matches!(r, OperationRef::Named(_)))
    }

    fn handle_message_sent(&mut self, msg: &MessageSent) {
        let input_kind = self.classify_target(&msg.input.info);

        if let TargetKind::RegisteredSection(ref section_name) = input_kind {
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
                    self.broadcast_event(
                        workflow_id,
                        section_name.clone(),
                        None,
                        SectionStatus::Running,
                    );
                }
            }
        }

        for source in &msg.output {
            let Some(source_section) = self.section_from_info(&source.info) else {
                continue;
            };
            if !self.sections_running.contains(source_section.as_str()) {
                continue;
            }
            let is_completion = match &input_kind {
                TargetKind::RegisteredSection(target) => target.as_str() != source_section.as_str(),
                TargetKind::TopLevel => true,
                TargetKind::Internal => false,
            };
            if !is_completion {
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
                None,
                SectionStatus::Completed,
            );
        }

        let input_sub = self.sub_section_from_info(&msg.input.info);

        if let Some(ref key) = input_sub {
            if !self.sub_sections_running.contains(key) {
                if let Some(workflow_id) =
                    self.section_name_to_workflow.get(key.0.as_str()).cloned()
                {
                    self.sub_sections_running.insert(key.clone());
                    if let Some(state) = self.workflows.get_mut(&workflow_id) {
                        state
                            .sub_sections
                            .insert(key.clone(), SectionStatus::Running);
                    }
                    self.broadcast_event(
                        workflow_id,
                        key.1.clone(),
                        Some(key.0.clone()),
                        SectionStatus::Running,
                    );
                }
            }
        }

        if !Self::input_is_named(&msg.input.info) {
            return;
        }
        for source in &msg.output {
            let Some(source_sub) = self.sub_section_from_info(&source.info) else {
                continue;
            };
            if !self.sub_sections_running.contains(&source_sub) {
                continue;
            }
            if input_sub.as_ref() == Some(&source_sub) {
                continue;
            }
            let Some(workflow_id) = self
                .section_name_to_workflow
                .get(source_sub.0.as_str())
                .cloned()
            else {
                continue;
            };
            self.sub_sections_running.remove(&source_sub);
            if let Some(state) = self.workflows.get_mut(&workflow_id) {
                state
                    .sub_sections
                    .insert(source_sub.clone(), SectionStatus::Completed);
            }
            self.broadcast_event(
                workflow_id,
                source_sub.1.clone(),
                Some(source_sub.0.clone()),
                SectionStatus::Completed,
            );
        }
    }

    fn broadcast_event(
        &self,
        workflow_id: WorkflowId,
        section_id: SectionId,
        parent_section_id: Option<SectionId>,
        status: SectionStatus,
    ) {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();

        tracing::debug!(
            "[workflow:{}] section '{}' (parent: {:?}) = {:?}",
            workflow_id,
            section_id,
            parent_section_id,
            status
        );

        self.persist_status(
            &workflow_id,
            &section_id,
            parent_section_id.as_deref().unwrap_or(""),
            &status,
        );

        let event = WorkflowEvent {
            workflow_id,
            section_id,
            parent_section_id,
            status,
            timestamp,
        };
        let _ = self.broadcast_tx.send(event);
    }

    fn persist_status(
        &self,
        workflow_id: &str,
        section_id: &str,
        parent_section_id: &str,
        status: &SectionStatus,
    ) {
        let Some(ref db) = self.db else {
            return;
        };
        let db = db.clone();
        let wf = workflow_id.to_string();
        let sec = section_id.to_string();
        let par = parent_section_id.to_string();
        let st = status.as_str().to_string();
        tokio::spawn(async move {
            if let Err(e) = sqlx::query(
                "INSERT INTO workflow_sections (workflow_id, section_id, parent_section_id, status, updated_at) \
                 VALUES ($1, $2, $3, $4, NOW()) \
                 ON CONFLICT (workflow_id, parent_section_id, section_id) \
                 DO UPDATE SET status = $4, updated_at = NOW()",
            )
            .bind(&wf)
            .bind(&sec)
            .bind(&par)
            .bind(&st)
            .execute(&db)
            .await
            {
                tracing::error!("Failed to persist workflow status: {e}");
            }
        });
    }
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

    fn message_to_top_level(from_section: &str, from_op: &str, to_op: &str) -> TracedEvent {
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
            info: Some(Arc::new(OperationInfo::new(
                Some(OperationRef::Named(NamedOperationRef {
                    namespaces: NamespaceList(smallvec![]),
                    exposed_namespace: None,
                    name: to_op.into(),
                })),
                None,
                None,
            ))),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![source],
            input,
            message: None,
        }))
    }

    fn message_to_dispose(from_section: &str, from_op: &str) -> TracedEvent {
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
            info: Some(Arc::new(OperationInfo::new(
                Some(OperationRef::Dispose),
                None,
                None,
            ))),
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
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["pickup".into(), "dropoff".into()],
            HashMap::new(),
        );

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
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["pickup".into(), "dropoff".into()],
            HashMap::new(),
        );
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

        let events = collect_events(&mut sub, 10).await;
        let pickup: Vec<_> = events.iter().filter(|e| e.section_id == "pickup").collect();
        assert_eq!(pickup.len(), 3);
        assert_eq!(pickup[0].status, SectionStatus::Pending);
        assert_eq!(pickup[1].status, SectionStatus::Running);
        assert_eq!(pickup[2].status, SectionStatus::Completed);
    }

    #[tokio::test]
    async fn ignores_non_section_messages() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register("wf-1".into(), vec!["pickup".into()], HashMap::new());
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
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-a".into(),
            vec!["wf_a_op1".into(), "wf_a_op2".into()],
            HashMap::new(),
        );
        handle.register("wf-b".into(), vec!["wf_b_op1".into()], HashMap::new());
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

        let wf_a_op1: Vec<_> = events
            .iter()
            .filter(|e| e.workflow_id == "wf-a" && e.section_id == "wf_a_op1")
            .collect();
        let wf_b_events: Vec<_> = events.iter().filter(|e| e.workflow_id == "wf-b").collect();

        assert_eq!(wf_a_op1.len(), 3);
        assert_eq!(wf_a_op1[0].status, SectionStatus::Pending);
        assert_eq!(wf_a_op1[1].status, SectionStatus::Running);
        assert_eq!(wf_a_op1[2].status, SectionStatus::Completed);

        assert_eq!(wf_b_events.len(), 2);
        assert_eq!(wf_b_events[0].status, SectionStatus::Pending);
        assert_eq!(wf_b_events[1].status, SectionStatus::Running);
    }

    #[tokio::test]
    async fn dispose_does_not_trigger_completion() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register("wf-1".into(), vec!["pickup".into()], HashMap::new());
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("pickup", "start"))
            .unwrap();
        trace_tx
            .send(message_to_dispose("pickup", "if_requested"))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 3).await;
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].status, SectionStatus::Pending);
        assert_eq!(events[1].status, SectionStatus::Running);
    }

    #[tokio::test]
    async fn top_level_target_triggers_completion() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register("wf-1".into(), vec!["pickup".into()], HashMap::new());
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("pickup", "start"))
            .unwrap();
        trace_tx
            .send(message_to_top_level("pickup", "status_report", "join__end"))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 3).await;
        assert_eq!(events.len(), 3);
        assert_eq!(events[0].status, SectionStatus::Pending);
        assert_eq!(events[1].status, SectionStatus::Running);
        assert_eq!(events[2].status, SectionStatus::Completed);
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
