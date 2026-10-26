use std::collections::HashMap;
use std::collections::HashSet;
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use crossflow::{
    OperationRef, bevy_app, bevy_ecs,
    trace::{MessageSent, TracedEvent, TracedEventKind},
};
use dashmap::DashMap;
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
    Failed,
    Skipped,
}

impl SectionStatus {
    fn as_str(&self) -> &'static str {
        match self {
            Self::Pending => "pending",
            Self::Running => "running",
            Self::Completed => "completed",
            Self::Failed => "failed",
            Self::Skipped => "skipped",
        }
    }

    pub fn from_db(s: &str) -> Self {
        match s {
            "running" => Self::Running,
            "completed" => Self::Completed,
            "failed" => Self::Failed,
            "skipped" => Self::Skipped,
            _ => Self::Pending,
        }
    }

    fn from_message(message: &crossflow::trace::TracedMessage) -> Self {
        let status = match message {
            Some(Ok(value)) => match value.get("status").and_then(|v| v.as_str()) {
                Some("failed") => Self::Failed,
                Some("skipped") => Self::Skipped,
                _ => Self::Completed,
            },
            Some(Err(e)) => {
                tracing::debug!("Trace message error: {e:?} → Failed");
                Self::Failed
            }
            None => {
                tracing::debug!("Trace message absent (None) → defaulting to Completed");
                Self::Completed
            }
        };
        status
    }
}

#[derive(Debug, PartialEq)]
enum TargetKind {
    RegisteredSection(String),
    TopLevel,
    Terminate,
    Internal,
}

#[derive(Debug, Clone, Serialize)]
pub struct WorkflowEvent {
    pub workflow_id: WorkflowId,
    pub section_id: SectionId,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub parent_section_id: Option<SectionId>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub op_sequence: Option<u32>,
    pub status: SectionStatus,
    pub timestamp: u64,
}

#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct WorkflowSnapshot {
    pub workflow_id: WorkflowId,
    pub sections: Vec<SectionSnapshot>,
}

#[derive(Debug, Clone, Serialize, Default, utoipa::ToSchema)]
pub struct SubSectionSnapshot {
    pub id: SectionId,
    pub status: SectionStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
}

#[derive(Debug, Clone, Serialize, Default, utoipa::ToSchema)]
pub struct SectionSnapshot {
    pub id: SectionId,
    pub status: SectionStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub display_name: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub sub_sections: Vec<SubSectionSnapshot>,
}

#[derive(Debug, Clone, Default)]
pub struct SectionState {
    pub status: SectionStatus,
    pub display_name: Option<String>,
    pub op_sequence: Option<u32>,
    pub sub_sections: HashMap<SectionId, SubSectionState>,
}

#[derive(Debug, Clone, Default)]
pub struct SubSectionState {
    pub status: SectionStatus,
    pub display_name: Option<String>,
    pub op_sequence: Option<u32>,
}

pub fn sections_to_sorted_vec(sections: &HashMap<SectionId, SectionState>) -> Vec<SectionSnapshot> {
    let mut list: Vec<_> = sections
        .iter()
        .map(|(id, s)| {
            let mut subs: Vec<_> = s
                .sub_sections
                .iter()
                .map(|(sub_id, sub)| {
                    (
                        sub.op_sequence,
                        SubSectionSnapshot {
                            id: sub_id.clone(),
                            status: sub.status.clone(),
                            display_name: sub.display_name.clone(),
                        },
                    )
                })
                .collect();
            subs.sort_by(|(seq_a, a), (seq_b, b)| {
                seq_a
                    .unwrap_or(u32::MAX)
                    .cmp(&seq_b.unwrap_or(u32::MAX))
                    .then_with(|| a.id.cmp(&b.id))
            });
            (
                s.op_sequence,
                SectionSnapshot {
                    id: id.clone(),
                    status: s.status.clone(),
                    display_name: s.display_name.clone(),
                    sub_sections: subs.into_iter().map(|(_, snap)| snap).collect(),
                },
            )
        })
        .collect();
    list.sort_by(|(seq_a, a), (seq_b, b)| {
        seq_a
            .unwrap_or(u32::MAX)
            .cmp(&seq_b.unwrap_or(u32::MAX))
            .then_with(|| a.id.cmp(&b.id))
    });
    list.into_iter().map(|(_, snap)| snap).collect()
}

pub struct WorkflowRegistration {
    pub workflow_id: WorkflowId,
    pub sections: Vec<SectionId>,
    pub sub_sections: HashMap<SectionId, Vec<SectionId>>,
    pub display_names: HashMap<SectionId, String>,
    pub op_sequences: HashMap<SectionId, u32>,
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

pub fn extract_display_names(diagram_json: &serde_json::Value) -> HashMap<SectionId, String> {
    let mut names = HashMap::new();
    let Some(ops) = diagram_json.get("ops").and_then(|v| v.as_object()) else {
        return names;
    };
    for (op_id, op) in ops {
        if op.get("type").and_then(|v| v.as_str()) != Some("section") {
            continue;
        }
        if let Some(display) = op.get("display_text").and_then(|v| v.as_str()) {
            names.insert(op_id.clone(), display.to_string());
        }
    }
    names
}

pub fn extract_op_sequences(diagram_json: &serde_json::Value) -> HashMap<SectionId, u32> {
    let mut seqs = HashMap::new();
    let Some(ops) = diagram_json.get("ops").and_then(|v| v.as_object()) else {
        return seqs;
    };
    let templates = diagram_json.get("templates").and_then(|v| v.as_object());
    for (op_id, op) in ops {
        if op.get("type").and_then(|v| v.as_str()) != Some("section") {
            continue;
        }
        if let Some(seq) = op.get("op_sequence").and_then(|v| v.as_u64()) {
            seqs.insert(op_id.clone(), seq as u32);
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
        for (sub_name, sub_op) in template_ops {
            if sub_op.get("type").and_then(|v| v.as_str()) != Some("section") {
                continue;
            }
            if let Some(seq) = sub_op.get("op_sequence").and_then(|v| v.as_u64()) {
                seqs.insert(format!("{op_id}:{sub_name}"), seq as u32);
            }
        }
    }
    seqs
}

pub fn extract_experiment_ids(diagram_json: &serde_json::Value) -> HashMap<SectionId, String> {
    let mut ids = HashMap::new();
    let Some(ops) = diagram_json.get("ops").and_then(|v| v.as_object()) else {
        return ids;
    };
    for (op_id, op) in ops {
        if op.get("type").and_then(|v| v.as_str()) != Some("section") {
            continue;
        }
        if let Some(id) = op.get("experiment_id").and_then(|v| v.as_str()) {
            ids.insert(op_id.clone(), id.to_string());
        }
    }
    ids
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

enum DbCommand {
    Clear(String),
    Upsert {
        workflow_id: String,
        section_id: String,
        parent_section_id: String,
        status: String,
        op_sequence: Option<u32>,
    },
}

pub struct WorkflowTracker {
    workflows: HashMap<WorkflowId, WorkflowState>,
    section_name_to_workflow: HashMap<SectionId, WorkflowId>,
    sections_running: HashSet<SectionId>,
    section_terminal_hints: HashMap<SectionId, SectionStatus>,
    sub_section_registry: HashSet<(SectionId, String)>,
    sub_sections_running: HashSet<(SectionId, String)>,
    display_names: HashMap<SectionId, String>,
    op_sequences: HashMap<SectionId, u32>,
    broadcast_tx: broadcast::Sender<WorkflowEvent>,
    register_rx: mpsc::UnboundedReceiver<WorkflowRegistration>,
    db_cmd_tx: Option<mpsc::UnboundedSender<DbCommand>>,
}

#[derive(Clone)]
pub struct WorkflowTrackerHandle {
    register_tx: mpsc::UnboundedSender<WorkflowRegistration>,
    broadcast_tx: broadcast::Sender<WorkflowEvent>,
    db: Option<sqlx::PgPool>,
    shared_display_names: Arc<DashMap<SectionId, String>>,
    shared_op_sequences: Arc<DashMap<SectionId, u32>>,
}

impl WorkflowTrackerHandle {
    pub fn register(
        &self,
        workflow_id: WorkflowId,
        sections: Vec<SectionId>,
        sub_sections: HashMap<SectionId, Vec<SectionId>>,
        display_names: HashMap<SectionId, String>,
        op_sequences: HashMap<SectionId, u32>,
    ) {
        for (id, name) in &display_names {
            self.shared_display_names.insert(id.clone(), name.clone());
        }
        for (id, seq) in &op_sequences {
            self.shared_op_sequences.insert(id.clone(), *seq);
        }
        let _ = self.register_tx.send(WorkflowRegistration {
            workflow_id,
            sections,
            sub_sections,
            display_names,
            op_sequences,
        });
    }

    pub fn subscribe(&self) -> broadcast::Receiver<WorkflowEvent> {
        self.broadcast_tx.subscribe()
    }

    pub fn db(&self) -> Option<&sqlx::PgPool> {
        self.db.as_ref()
    }

    pub fn display_name(&self, section_id: &str) -> Option<String> {
        self.shared_display_names.get(section_id).map(|v| v.clone())
    }

    pub fn op_sequence(&self, section_id: &str) -> Option<u32> {
        self.shared_op_sequences.get(section_id).map(|v| *v)
    }
}

impl WorkflowTracker {
    pub fn new(
        broadcast_capacity: usize,
        db: Option<sqlx::PgPool>,
    ) -> (Self, WorkflowTrackerHandle) {
        let (broadcast_tx, _) = broadcast::channel(broadcast_capacity);
        let (register_tx, register_rx) = mpsc::unbounded_channel();

        let db_cmd_tx = db.as_ref().map(|pool| {
            let (tx, rx) = mpsc::unbounded_channel();
            let pool = pool.clone();
            tokio::spawn(Self::db_writer(pool, rx));
            tx
        });

        let tracker = Self {
            workflows: HashMap::new(),
            section_name_to_workflow: HashMap::new(),
            sections_running: HashSet::new(),
            section_terminal_hints: HashMap::new(),
            sub_section_registry: HashSet::new(),
            sub_sections_running: HashSet::new(),
            display_names: HashMap::new(),
            op_sequences: HashMap::new(),
            broadcast_tx: broadcast_tx.clone(),
            register_rx,
            db_cmd_tx,
        };

        let handle = WorkflowTrackerHandle {
            register_tx,
            broadcast_tx,
            db,
            shared_display_names: Arc::new(DashMap::new()),
            shared_op_sequences: Arc::new(DashMap::new()),
        };

        (tracker, handle)
    }

    async fn db_writer(pool: sqlx::PgPool, mut rx: mpsc::UnboundedReceiver<DbCommand>) {
        while let Some(cmd) = rx.recv().await {
            match cmd {
                DbCommand::Clear(workflow_id) => {
                    if let Err(e) =
                        sqlx::query("DELETE FROM workflow_sections WHERE workflow_id = $1")
                            .bind(&workflow_id)
                            .execute(&pool)
                            .await
                    {
                        tracing::error!("Failed to clear workflow {workflow_id} from DB: {e}");
                    }
                }
                DbCommand::Upsert {
                    workflow_id,
                    section_id,
                    parent_section_id,
                    status,
                    op_sequence,
                } => {
                    let op_seq_i32 = op_sequence.map(|v| v as i32);
                    if let Err(e) = sqlx::query(
                        "INSERT INTO workflow_sections \
                             (workflow_id, section_id, parent_section_id, status, op_sequence, updated_at) \
                         VALUES ($1, $2, $3, $4, $5, NOW()) \
                         ON CONFLICT (workflow_id, parent_section_id, section_id) \
                         DO UPDATE SET status = $4, op_sequence = COALESCE($5, workflow_sections.op_sequence), updated_at = NOW()",
                    )
                    .bind(&workflow_id)
                    .bind(&section_id)
                    .bind(&parent_section_id)
                    .bind(&status)
                    .bind(op_seq_i32)
                    .execute(&pool)
                    .await
                    {
                        tracing::error!("Failed to persist workflow status: {e}");
                    }
                }
            }
        }
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

        self.clear_workflow_from_db(&registration.workflow_id);
        self.display_names.extend(registration.display_names);
        self.op_sequences.extend(registration.op_sequences);

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

    // Resolve a section's terminal status from the trace message, falling
    // back to sub-section outcomes then the section's terminal_hint when
    // the trace data is absent.
    fn resolve_terminal(
        &self,
        section: &str,
        workflow_id: &str,
        msg: &crossflow::trace::TracedMessage,
    ) -> SectionStatus {
        let from_msg = SectionStatus::from_message(msg);
        if from_msg != SectionStatus::Completed {
            tracing::debug!("resolve '{}': msg={:?}", section, from_msg);
            return from_msg;
        }
        let from_subs = self.status_from_sub_sections(section, workflow_id);
        let from_hint = self
            .section_terminal_hints
            .get(section)
            .filter(|h| matches!(h, SectionStatus::Failed | SectionStatus::Skipped))
            .cloned();
        let resolved = from_subs
            .clone()
            .or(from_hint.clone())
            .unwrap_or(SectionStatus::Completed);
        tracing::debug!(
            "resolve '{}': msg=Completed, subs={:?}, hint={:?} → {:?}",
            section,
            from_subs,
            from_hint,
            resolved,
        );
        resolved
    }

    fn status_from_sub_sections(&self, section: &str, workflow_id: &str) -> Option<SectionStatus> {
        let subs: Vec<_> = self
            .sub_section_registry
            .iter()
            .filter(|(p, _)| p == section)
            .cloned()
            .collect();
        if subs.is_empty() {
            return None;
        }
        let state = self.workflows.get(workflow_id)?;
        let mut any_ran = false;
        let mut any_failed = false;
        let mut any_skipped = false;
        for key in &subs {
            match state.sub_sections.get(key) {
                Some(SectionStatus::Failed) => {
                    any_ran = true;
                    any_failed = true;
                }
                Some(SectionStatus::Skipped) => {
                    any_ran = true;
                    any_skipped = true;
                }
                Some(SectionStatus::Running) | Some(SectionStatus::Completed) => {
                    any_ran = true;
                }
                _ => {}
            }
        }
        if !any_ran {
            return None;
        }
        if any_failed {
            Some(SectionStatus::Failed)
        } else if any_skipped {
            Some(SectionStatus::Skipped)
        } else {
            None
        }
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
            OperationRef::Terminate(_) => TargetKind::Terminate,
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

        // When a new section starts Running, complete predecessor sections
        // in the same workflow (data exited them to reach this section).
        if let TargetKind::RegisteredSection(ref section_name) = input_kind {
            if !self.sections_running.contains(section_name.as_str()) {
                if let Some(workflow_id) = self
                    .section_name_to_workflow
                    .get(section_name.as_str())
                    .cloned()
                {
                    let predecessors: Vec<String> = self
                        .sections_running
                        .iter()
                        .filter(|s| {
                            self.section_name_to_workflow.get(s.as_str()) == Some(&workflow_id)
                        })
                        .cloned()
                        .collect();
                    let mut worst_predecessor = SectionStatus::Completed;
                    for prev in &predecessors {
                        let prev_terminal = self.resolve_terminal(prev, &workflow_id, &msg.message);
                        if matches!(
                            prev_terminal,
                            SectionStatus::Failed | SectionStatus::Skipped
                        ) {
                            worst_predecessor = prev_terminal.clone();
                        }
                        self.sections_running.remove(prev);
                        if let Some(state) = self.workflows.get_mut(&workflow_id) {
                            state.sections.insert(prev.clone(), prev_terminal.clone());
                        }
                        self.broadcast_event(
                            workflow_id.clone(),
                            prev.clone(),
                            None,
                            prev_terminal,
                        );
                    }

                    // If a predecessor failed/skipped and the trace message
                    // was absent, the successor was skipped (not completed).
                    let msg_terminal = SectionStatus::from_message(&msg.message);
                    let hint = match worst_predecessor {
                        SectionStatus::Failed | SectionStatus::Skipped
                            if msg_terminal == SectionStatus::Completed =>
                        {
                            SectionStatus::Skipped
                        }
                        _ => msg_terminal,
                    };
                    tracing::debug!("Section '{}' terminal_hint={:?}", section_name, hint);

                    self.sections_running.insert(section_name.clone());
                    self.section_terminal_hints
                        .insert(section_name.clone(), hint);
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

        // Source-based completion (secondary mechanism when source trace info
        // is available).
        for source in &msg.output {
            let Some(source_section) = self.section_from_info(&source.info) else {
                continue;
            };
            if !self.sections_running.contains(source_section.as_str()) {
                continue;
            }
            let is_completion = match &input_kind {
                TargetKind::RegisteredSection(target) => target.as_str() != source_section.as_str(),
                TargetKind::TopLevel | TargetKind::Terminate => true,
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
            let terminal = self.resolve_terminal(&source_section, &workflow_id, &msg.message);
            self.sections_running.remove(source_section.as_str());
            if let Some(state) = self.workflows.get_mut(&workflow_id) {
                state
                    .sections
                    .insert(source_section.clone(), terminal.clone());
            }
            self.broadcast_event(workflow_id, source_section.clone(), None, terminal);
        }

        // Terminate fallback: when the workflow terminates and source info is
        // unavailable, complete all remaining running sections.
        if input_kind == TargetKind::Terminate {
            let remaining: Vec<(String, String)> = self
                .sections_running
                .iter()
                .filter_map(|s| {
                    let wf = self.section_name_to_workflow.get(s.as_str())?.clone();
                    Some((s.clone(), wf))
                })
                .collect();
            for (section, workflow_id) in remaining {
                self.sections_running.remove(&section);
                let terminal = self.resolve_terminal(&section, &workflow_id, &msg.message);
                if let Some(state) = self.workflows.get_mut(&workflow_id) {
                    state.sections.insert(section.clone(), terminal.clone());
                }
                self.broadcast_event(workflow_id, section, None, terminal);
            }
        }

        let input_sub = self.sub_section_from_info(&msg.input.info);

        if let Some(ref key) = input_sub {
            if !self.sub_sections_running.contains(key) {
                if let Some(workflow_id) =
                    self.section_name_to_workflow.get(key.0.as_str()).cloned()
                {
                    if !self.sections_running.contains(key.0.as_str()) {
                        self.sections_running.insert(key.0.clone());
                        if let Some(state) = self.workflows.get_mut(&workflow_id) {
                            state.sections.insert(key.0.clone(), SectionStatus::Running);
                        }
                        self.broadcast_event(
                            workflow_id.clone(),
                            key.0.clone(),
                            None,
                            SectionStatus::Running,
                        );
                    }
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
            let terminal = SectionStatus::from_message(&msg.message);
            self.sub_sections_running.remove(&source_sub);
            if let Some(state) = self.workflows.get_mut(&workflow_id) {
                state
                    .sub_sections
                    .insert(source_sub.clone(), terminal.clone());
            }
            self.broadcast_event(
                workflow_id.clone(),
                source_sub.1.clone(),
                Some(source_sub.0.clone()),
                terminal.clone(),
            );

            // When all sub-sections of a parent reach a terminal state,
            // derive the parent status from the sub-section outcomes — not
            // from the raw message, which carries intermediate data whose
            // "status" field can mean something completely different.
            let parent = source_sub.0.clone();
            let all_subs: Vec<_> = self
                .sub_section_registry
                .iter()
                .filter(|(p, _)| p == &parent)
                .cloned()
                .collect();
            let (all_terminal, any_failed, any_skipped) =
                if let Some(state) = self.workflows.get(&workflow_id) {
                    let mut all = true;
                    let mut failed = false;
                    let mut skipped = false;
                    for key in &all_subs {
                        match state.sub_sections.get(key) {
                            Some(SectionStatus::Failed) => failed = true,
                            Some(SectionStatus::Skipped) => skipped = true,
                            Some(SectionStatus::Completed) => {}
                            _ => {
                                all = false;
                                break;
                            }
                        }
                    }
                    (all, failed, skipped)
                } else {
                    (false, false, false)
                };
            if all_terminal && self.sections_running.contains(&parent) {
                self.sections_running.remove(&parent);
                let parent_terminal = if any_failed {
                    SectionStatus::Failed
                } else if any_skipped {
                    SectionStatus::Skipped
                } else {
                    SectionStatus::Completed
                };
                if let Some(state) = self.workflows.get_mut(&workflow_id) {
                    state
                        .sections
                        .insert(parent.clone(), parent_terminal.clone());
                }
                self.broadcast_event(workflow_id, parent, None, parent_terminal);
            }
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

        let display_name = self.display_names.get(&section_id).cloned();
        let op_sequence = match &parent_section_id {
            Some(parent) => {
                let scoped = format!("{parent}:{section_id}");
                self.op_sequences.get(&scoped).copied()
            }
            None => self.op_sequences.get(&section_id).copied(),
        };
        let event = WorkflowEvent {
            workflow_id,
            section_id,
            parent_section_id,
            display_name,
            op_sequence,
            status,
            timestamp,
        };
        let _ = self.broadcast_tx.send(event);
    }

    fn clear_workflow_from_db(&self, workflow_id: &str) {
        if let Some(ref tx) = self.db_cmd_tx {
            let _ = tx.send(DbCommand::Clear(workflow_id.to_string()));
        }
    }

    fn persist_status(
        &self,
        workflow_id: &str,
        section_id: &str,
        parent_section_id: &str,
        status: &SectionStatus,
    ) {
        if let Some(ref tx) = self.db_cmd_tx {
            let op_sequence = if parent_section_id.is_empty() {
                self.op_sequences.get(section_id).copied()
            } else {
                let scoped = format!("{parent_section_id}:{section_id}");
                self.op_sequences.get(&scoped).copied()
            };
            let _ = tx.send(DbCommand::Upsert {
                workflow_id: workflow_id.to_string(),
                section_id: section_id.to_string(),
                parent_section_id: parent_section_id.to_string(),
                status: status.as_str().to_string(),
                op_sequence,
            });
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossflow::{
        bevy_ecs::prelude::Entity,
        diagram::{NamedOperationRef, NamespaceList},
        re_exports::smallvec,
        trace::{OperationInfo, TraceSource, TraceTarget, TracedMessage},
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
        message_leaving_section_with(from_section, from_op, to_section, to_op, None)
    }

    fn message_leaving_section_with(
        from_section: &str,
        from_op: &str,
        to_section: &str,
        to_op: &str,
        message: TracedMessage,
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
            message,
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
            HashMap::new(),
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
            HashMap::new(),
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

        handle.register(
            "wf-1".into(),
            vec!["pickup".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
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
            HashMap::new(),
            HashMap::new(),
        );
        handle.register(
            "wf-b".into(),
            vec!["wf_b_op1".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
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

        handle.register(
            "wf-1".into(),
            vec!["pickup".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
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

        handle.register(
            "wf-1".into(),
            vec!["pickup".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
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

    #[tokio::test]
    async fn failed_message_sets_failed_status() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["pickup".into(), "dropoff".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("pickup", "start"))
            .unwrap();
        trace_tx
            .send(message_leaving_section_with(
                "pickup",
                "done",
                "dropoff",
                "start",
                Some(Ok(
                    serde_json::json!({"status": "failed", "detail": "boom"}),
                )),
            ))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 10).await;
        let pickup: Vec<_> = events.iter().filter(|e| e.section_id == "pickup").collect();
        assert_eq!(pickup.len(), 3);
        assert_eq!(pickup[0].status, SectionStatus::Pending);
        assert_eq!(pickup[1].status, SectionStatus::Running);
        assert_eq!(pickup[2].status, SectionStatus::Failed);
    }

    #[tokio::test]
    async fn skipped_message_sets_skipped_status() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["pickup".into(), "dropoff".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("pickup", "start"))
            .unwrap();
        trace_tx
            .send(message_leaving_section_with(
                "pickup",
                "done",
                "dropoff",
                "start",
                Some(Ok(
                    serde_json::json!({"status": "skipped", "because": "input not ok"}),
                )),
            ))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 10).await;
        let pickup: Vec<_> = events.iter().filter(|e| e.section_id == "pickup").collect();
        assert_eq!(pickup.len(), 3);
        assert_eq!(pickup[0].status, SectionStatus::Pending);
        assert_eq!(pickup[1].status, SectionStatus::Running);
        assert_eq!(pickup[2].status, SectionStatus::Skipped);
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

    fn message_no_source_info_entering_section_with(
        section: &str,
        op: &str,
        message: TracedMessage,
    ) -> TracedEvent {
        let source = TraceSource {
            session_stack: smallvec![make_entity(1)],
            source: make_entity(98),
            seq: 0,
            port: smallvec![],
            labels: None,
            operation_type: "test".into(),
            info: None,
        };
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: namespaced_op_info(section, op),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![source],
            input,
            message,
        }))
    }

    fn message_no_source_info_entering_section(section: &str, op: &str) -> TracedEvent {
        let source = TraceSource {
            session_stack: smallvec![make_entity(1)],
            source: make_entity(98),
            seq: 0,
            port: smallvec![],
            labels: None,
            operation_type: "test".into(),
            info: None,
        };
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: namespaced_op_info(section, op),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![source],
            input,
            message: None,
        }))
    }

    fn message_to_terminate(from_section: &str, from_op: &str) -> TracedEvent {
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
                Some(OperationRef::Terminate(
                    crossflow::diagram::NamespaceList::default(),
                )),
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

    fn message_to_terminate_no_source_info() -> TracedEvent {
        let source = TraceSource {
            session_stack: smallvec![make_entity(1)],
            source: make_entity(98),
            seq: 0,
            port: smallvec![],
            labels: None,
            operation_type: "test".into(),
            info: None,
        };
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: Some(Arc::new(OperationInfo::new(
                Some(OperationRef::Terminate(
                    crossflow::diagram::NamespaceList::default(),
                )),
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

    fn sub_namespaced_op_info(parent: &str, child: &str, op: &str) -> Option<Arc<OperationInfo>> {
        Some(Arc::new(OperationInfo::new(
            Some(OperationRef::Named(NamedOperationRef {
                namespaces: NamespaceList(smallvec![parent.into(), child.into()]),
                exposed_namespace: None,
                name: op.into(),
            })),
            None,
            None,
        )))
    }

    fn message_entering_sub_section(parent: &str, child: &str, op: &str) -> TracedEvent {
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: sub_namespaced_op_info(parent, child, op),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![],
            input,
            message: None,
        }))
    }

    fn message_leaving_sub_section(
        from_parent: &str,
        from_child: &str,
        from_op: &str,
        to_parent: &str,
        to_op: &str,
    ) -> TracedEvent {
        let source = TraceSource {
            session_stack: smallvec![make_entity(1)],
            source: make_entity(98),
            seq: 0,
            port: smallvec![],
            labels: None,
            operation_type: "test".into(),
            info: sub_namespaced_op_info(from_parent, from_child, from_op),
        };
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: namespaced_op_info(to_parent, to_op),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![source],
            input,
            message: None,
        }))
    }

    #[tokio::test]
    async fn predecessor_completes_when_next_section_starts() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["sec_a".into(), "sec_b".into(), "sec_c".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
        tokio::task::yield_now().await;

        // Source info is None (simulates fork_result output entity with no Trace)
        trace_tx
            .send(message_no_source_info_entering_section("sec_a", "start"))
            .unwrap();
        trace_tx
            .send(message_no_source_info_entering_section("sec_b", "start"))
            .unwrap();
        trace_tx
            .send(message_no_source_info_entering_section("sec_c", "start"))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 20).await;

        let sec_a: Vec<_> = events.iter().filter(|e| e.section_id == "sec_a").collect();
        assert_eq!(sec_a.len(), 3, "sec_a events: {sec_a:?}");
        assert_eq!(sec_a[0].status, SectionStatus::Pending);
        assert_eq!(sec_a[1].status, SectionStatus::Running);
        assert_eq!(sec_a[2].status, SectionStatus::Completed);

        let sec_b: Vec<_> = events.iter().filter(|e| e.section_id == "sec_b").collect();
        assert_eq!(sec_b.len(), 3, "sec_b events: {sec_b:?}");
        assert_eq!(sec_b[0].status, SectionStatus::Pending);
        assert_eq!(sec_b[1].status, SectionStatus::Running);
        assert_eq!(sec_b[2].status, SectionStatus::Completed);

        let sec_c: Vec<_> = events.iter().filter(|e| e.section_id == "sec_c").collect();
        assert_eq!(sec_c.len(), 2, "sec_c events: {sec_c:?}");
        assert_eq!(sec_c[0].status, SectionStatus::Pending);
        assert_eq!(sec_c[1].status, SectionStatus::Running);
    }

    #[tokio::test]
    async fn terminate_completes_last_section() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["sec_a".into(), "sec_b".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
        tokio::task::yield_now().await;

        trace_tx
            .send(message_entering_section("sec_a", "start"))
            .unwrap();
        trace_tx
            .send(message_no_source_info_entering_section("sec_b", "start"))
            .unwrap();
        trace_tx
            .send(message_to_terminate_no_source_info())
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 20).await;

        let sec_b: Vec<_> = events.iter().filter(|e| e.section_id == "sec_b").collect();
        assert_eq!(sec_b.len(), 3, "sec_b events: {sec_b:?}");
        assert_eq!(sec_b[0].status, SectionStatus::Pending);
        assert_eq!(sec_b[1].status, SectionStatus::Running);
        assert_eq!(sec_b[2].status, SectionStatus::Completed);
    }

    #[tokio::test]
    async fn terminate_uses_incoming_status_hint_for_last_section() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["sec_a".into(), "sec_b".into()],
            HashMap::new(),
            HashMap::new(),
            HashMap::new(),
        );
        tokio::task::yield_now().await;

        // sec_a starts running
        trace_tx
            .send(message_entering_section("sec_a", "start"))
            .unwrap();
        // sec_b enters with a "skipped" message (predecessor was skipped)
        trace_tx
            .send(message_no_source_info_entering_section_with(
                "sec_b",
                "start",
                Some(Ok(
                    serde_json::json!({"status": "skipped", "because": "input not ok"}),
                )),
            ))
            .unwrap();
        // Terminate fires — sec_b should use its hint (skipped), not the
        // terminate message
        trace_tx
            .send(message_to_terminate_no_source_info())
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 20).await;

        let sec_a: Vec<_> = events.iter().filter(|e| e.section_id == "sec_a").collect();
        assert_eq!(sec_a.len(), 3, "sec_a events: {sec_a:?}");
        assert_eq!(sec_a[2].status, SectionStatus::Skipped);

        let sec_b: Vec<_> = events.iter().filter(|e| e.section_id == "sec_b").collect();
        assert_eq!(sec_b.len(), 3, "sec_b events: {sec_b:?}");
        assert_eq!(sec_b[0].status, SectionStatus::Pending);
        assert_eq!(sec_b[1].status, SectionStatus::Running);
        assert_eq!(sec_b[2].status, SectionStatus::Skipped);
    }

    fn message_leaving_sub_section_with(
        from_parent: &str,
        from_child: &str,
        from_op: &str,
        to_parent: &str,
        to_op: &str,
        message: TracedMessage,
    ) -> TracedEvent {
        let source = TraceSource {
            session_stack: smallvec![make_entity(1)],
            source: make_entity(98),
            seq: 0,
            port: smallvec![],
            labels: None,
            operation_type: "test".into(),
            info: sub_namespaced_op_info(from_parent, from_child, from_op),
        };
        let input = TraceTarget {
            session_stack: smallvec![make_entity(1)],
            target: make_entity(99),
            seq: 0,
            labels: None,
            operation_type: "test".into(),
            info: namespaced_op_info(to_parent, to_op),
        };
        TracedEvent::now(TracedEventKind::MessageSent(MessageSent {
            output: smallvec![source],
            input,
            message,
        }))
    }

    // Reproduces the DSC bug: first sub-section (health) fails, remaining
    // sub-sections never run, and the predecessor completion path has no
    // trace message data. Without the fix, op1-op5 all show as Completed.
    #[tokio::test]
    async fn sub_section_failure_cascades_to_successor_sections() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        let sub_sections: HashMap<String, Vec<String>> = [
            (
                "op1".into(),
                vec!["health".into(), "move".into(), "pick".into()],
            ),
            (
                "op2".into(),
                vec!["health".into(), "move".into(), "pick".into()],
            ),
        ]
        .into_iter()
        .collect();

        handle.register(
            "wf-1".into(),
            vec!["op1".into(), "op2".into(), "op3".into()],
            sub_sections,
            HashMap::new(),
            HashMap::new(),
        );
        tokio::task::yield_now().await;

        // op1 starts: health sub-section enters
        trace_tx
            .send(message_entering_sub_section("op1", "health", "start"))
            .unwrap();
        // health fails → exits to parent's internal node (not to move/pick)
        trace_tx
            .send(message_leaving_sub_section_with(
                "op1",
                "health",
                "done",
                "op1",
                "status_failed",
                Some(Ok(serde_json::json!({"status": "failed"}))),
            ))
            .unwrap();
        // op2 starts (data from op1→op2 via precedence) with NO trace message
        // (simulates missing value serialization at section boundary)
        trace_tx
            .send(message_no_source_info_entering_section("op2", "start"))
            .unwrap();
        // op3 starts (data from op2→op3) with NO trace message
        trace_tx
            .send(message_no_source_info_entering_section("op3", "start"))
            .unwrap();
        // Terminate
        trace_tx
            .send(message_to_terminate_no_source_info())
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 30).await;

        // op1 should be Failed (derived from health sub-section failure)
        let op1: Vec<_> = events
            .iter()
            .filter(|e| e.section_id == "op1" && e.parent_section_id.is_none())
            .collect();
        let op1_terminal = op1.last().expect("op1 should have events");
        assert_eq!(
            op1_terminal.status,
            SectionStatus::Failed,
            "op1 should be Failed (health sub-section failed), got: {:?}",
            op1_terminal.status
        );

        // op2 should be Skipped (predecessor failed, no trace data)
        let op2: Vec<_> = events
            .iter()
            .filter(|e| e.section_id == "op2" && e.parent_section_id.is_none())
            .collect();
        let op2_terminal = op2.last().expect("op2 should have events");
        assert_eq!(
            op2_terminal.status,
            SectionStatus::Skipped,
            "op2 should be Skipped (predecessor failed), got: {:?}",
            op2_terminal.status
        );

        // op3 should be Skipped (via terminal_hint cascade)
        let op3: Vec<_> = events
            .iter()
            .filter(|e| e.section_id == "op3" && e.parent_section_id.is_none())
            .collect();
        let op3_terminal = op3.last().expect("op3 should have events");
        assert_eq!(
            op3_terminal.status,
            SectionStatus::Skipped,
            "op3 should be Skipped (cascade from op1 failure), got: {:?}",
            op3_terminal.status
        );
    }

    #[tokio::test]
    async fn all_sub_sections_completed_completes_parent() {
        let (tracker, handle) = WorkflowTracker::new(64, None);
        let (trace_tx, trace_rx) = mpsc::unbounded_channel();
        let mut sub = handle.subscribe();

        tokio::spawn(tracker.run(trace_rx));

        handle.register(
            "wf-1".into(),
            vec!["parent_sec".into()],
            [("parent_sec".into(), vec!["sub_a".into(), "sub_b".into()])]
                .into_iter()
                .collect(),
            HashMap::new(),
            HashMap::new(),
        );
        tokio::task::yield_now().await;

        // Enter sub_a (starts parent + sub_a Running)
        trace_tx
            .send(message_entering_sub_section("parent_sec", "sub_a", "start"))
            .unwrap();
        // sub_a exits to a non-sub-section op in parent
        trace_tx
            .send(message_leaving_sub_section(
                "parent_sec",
                "sub_a",
                "done",
                "parent_sec",
                "bridge",
            ))
            .unwrap();
        // Enter sub_b
        trace_tx
            .send(message_entering_sub_section("parent_sec", "sub_b", "start"))
            .unwrap();
        // sub_b exits to parent's output
        trace_tx
            .send(message_leaving_sub_section(
                "parent_sec",
                "sub_b",
                "done",
                "parent_sec",
                "output",
            ))
            .unwrap();

        drop(trace_tx);

        let events = collect_events(&mut sub, 20).await;

        let parent: Vec<_> = events
            .iter()
            .filter(|e| e.section_id == "parent_sec" && e.parent_section_id.is_none())
            .collect();
        assert!(
            parent.iter().any(|e| e.status == SectionStatus::Running),
            "parent should have Running event"
        );
        assert!(
            parent.iter().any(|e| e.status == SectionStatus::Completed),
            "parent should have Completed event after all subs done: {parent:?}"
        );

        let sub_a: Vec<_> = events.iter().filter(|e| e.section_id == "sub_a").collect();
        assert!(
            sub_a.iter().any(|e| e.status == SectionStatus::Completed),
            "sub_a should complete"
        );

        let sub_b: Vec<_> = events.iter().filter(|e| e.section_id == "sub_b").collect();
        assert!(
            sub_b.iter().any(|e| e.status == SectionStatus::Completed),
            "sub_b should complete"
        );
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
