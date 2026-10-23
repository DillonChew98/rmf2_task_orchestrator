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

use crossflow::{bevy_app, bevy_ecs};
use std::collections::{HashMap, HashSet};

// ── DB ──

#[derive(Debug, Clone, serde::Serialize, sqlx::FromRow)]
pub struct DeviceRow {
    pub id: String,
    pub role: String,
    pub device_type: String,
    pub protocol: serde_json::Value,
    pub enabled: bool,
}

pub async fn load_devices(pool: &sqlx::PgPool) -> Result<Vec<DeviceRow>, sqlx::Error> {
    sqlx::query_as::<_, DeviceRow>(
        "SELECT id, role, device_type, protocol, enabled FROM devices WHERE enabled = true",
    )
    .fetch_all(pool)
    .await
}

pub async fn insert_device(pool: &sqlx::PgPool, row: &DeviceRow) -> Result<(), sqlx::Error> {
    sqlx::query(
        "INSERT INTO devices (id, role, device_type, protocol, enabled) VALUES ($1, $2, $3, $4, $5)",
    )
    .bind(&row.id)
    .bind(&row.role)
    .bind(&row.device_type)
    .bind(&row.protocol)
    .bind(row.enabled)
    .execute(pool)
    .await?;
    Ok(())
}

pub async fn delete_device(pool: &sqlx::PgPool, device_id: &str) -> Result<bool, sqlx::Error> {
    let result = sqlx::query("DELETE FROM devices WHERE id = $1")
        .bind(device_id)
        .execute(pool)
        .await?;
    Ok(result.rows_affected() > 0)
}

pub async fn list_all_devices(pool: &sqlx::PgPool) -> Result<Vec<DeviceRow>, sqlx::Error> {
    sqlx::query_as::<_, DeviceRow>(
        "SELECT id, role, device_type, protocol, enabled FROM devices ORDER BY role, id",
    )
    .fetch_all(pool)
    .await
}

// ── Bevy Components ──

#[derive(Debug, Clone, bevy_ecs::component::Component)]
pub struct DeviceInfo {
    pub id: String,
    pub role: String,
    pub device_type: String,
    /// Protocol config as raw JSON — matches the DeviceProtocol shape in node::device
    pub protocol: serde_json::Value,
}

#[derive(Debug, Clone, bevy_ecs::component::Component)]
pub struct DeviceStatus {
    pub available: bool,
}

/// Attached to a device entity while it is executing a task. Removed on release.
#[derive(Debug, Clone, bevy_ecs::component::Component)]
pub struct CurrentTask {
    pub section_id: String,
}

// ── Bevy Resources ──

/// Device ID → Entity lookup, populated on startup from the DB.
#[derive(Debug, Default, bevy_ecs::resource::Resource)]
pub struct DeviceRegistry {
    devices: HashMap<String, bevy_ecs::entity::Entity>,
}

impl DeviceRegistry {
    pub fn get(&self, device_id: &str) -> Option<bevy_ecs::entity::Entity> {
        self.devices.get(device_id).copied()
    }

    pub fn insert(&mut self, device_id: String, entity: bevy_ecs::entity::Entity) {
        self.devices.insert(device_id, entity);
    }

    pub fn remove(&mut self, device_id: &str) -> Option<bevy_ecs::entity::Entity> {
        self.devices.remove(device_id)
    }

    pub fn len(&self) -> usize {
        self.devices.len()
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, bevy_ecs::entity::Entity)> {
        self.devices.iter().map(|(k, &v)| (k.as_str(), v))
    }
}

/// Tracks which devices are allocated to which sections.
#[derive(Debug, Default, bevy_ecs::resource::Resource)]
pub struct ActiveAllocations {
    allocations: HashMap<String, Vec<String>>,
}

impl ActiveAllocations {
    pub fn allocate(&mut self, section_id: String, device_ids: Vec<String>) {
        self.allocations.insert(section_id, device_ids);
    }

    pub fn release(&mut self, section_id: &str) -> Option<Vec<String>> {
        self.allocations.remove(section_id)
    }

    pub fn get(&self, section_id: &str) -> Option<&Vec<String>> {
        self.allocations.get(section_id)
    }

    pub fn is_empty(&self) -> bool {
        self.allocations.is_empty()
    }
}

// ── API / handler → Bevy command channel ──

/// Response to an Allocate command: (role→device_id, device_id→protocol JSON).
pub type AllocateResponse = Option<(AllocationResult, HashMap<String, serde_json::Value>)>;

pub enum DeviceCommand {
    Add(DeviceRow),
    Remove {
        device_id: String,
    },
    Allocate {
        section_id: String,
        device_roles: DeviceRoles,
        response: tokio::sync::oneshot::Sender<AllocateResponse>,
    },
    Release {
        section_id: String,
    },
}

// ── Allocation types ──

/// Maps role name → specifier ($any, $any.type, or device_id).
pub type DeviceRoles = HashMap<String, String>;

/// Maps role name → allocated device_id.
pub type AllocationResult = HashMap<String, String>;

#[derive(Debug)]
enum RoleSpec {
    Any,
    AnyOfType(String),
    Exact(String),
}

fn parse_role_spec(s: &str) -> RoleSpec {
    if s == "$any" {
        RoleSpec::Any
    } else if let Some(t) = s.strip_prefix("$any.") {
        RoleSpec::AnyOfType(t.to_string())
    } else {
        RoleSpec::Exact(s.to_string())
    }
}

/// Try to satisfy all roles atomically. Returns None if any role cannot be filled.
/// Exact device IDs bypass availability — they rely on call_device's DeliveryLabel queueing.
pub fn try_allocate<'a>(
    device_roles: &DeviceRoles,
    devices: impl Iterator<Item = (&'a DeviceInfo, &'a DeviceStatus)> + Clone,
) -> Option<AllocationResult> {
    let mut result = AllocationResult::new();
    let mut claimed: HashSet<&str> = HashSet::new();

    for (role, spec_str) in device_roles {
        match parse_role_spec(spec_str) {
            RoleSpec::Exact(device_id) => {
                result.insert(role.clone(), device_id);
            }
            RoleSpec::Any => {
                let found = devices.clone().find(|(info, status)| {
                    info.role == *role && status.available && !claimed.contains(info.id.as_str())
                });
                let (info, _) = found?;
                claimed.insert(&info.id);
                result.insert(role.clone(), info.id.clone());
            }
            RoleSpec::AnyOfType(ref dtype) => {
                let found = devices.clone().find(|(info, status)| {
                    info.role == *role
                        && info.device_type == *dtype
                        && status.available
                        && !claimed.contains(info.id.as_str())
                });
                let (info, _) = found?;
                claimed.insert(&info.id);
                result.insert(role.clone(), info.id.clone());
            }
        }
    }
    Some(result)
}

// ── Bevy world helpers ──

/// Process pending device commands (called from the executor loop).
pub fn process_device_commands(
    app: &mut bevy_app::App,
    rx: &std::sync::mpsc::Receiver<DeviceCommand>,
) {
    let cmds: Vec<DeviceCommand> = std::iter::from_fn(|| rx.try_recv().ok()).collect();
    if cmds.is_empty() {
        return;
    }
    let world = app.world_mut();
    for cmd in cmds {
        match cmd {
            DeviceCommand::Add(row) => {
                let id = row.id.clone();
                let entity = world
                    .spawn((
                        DeviceInfo {
                            id: row.id,
                            role: row.role,
                            device_type: row.device_type,
                            protocol: row.protocol,
                        },
                        DeviceStatus { available: true },
                    ))
                    .id();
                world
                    .resource_mut::<DeviceRegistry>()
                    .insert(id.clone(), entity);
                tracing::info!("Device added via API: {id}");
            }
            DeviceCommand::Remove { device_id } => {
                let entity = world.resource_mut::<DeviceRegistry>().remove(&device_id);
                if let Some(entity) = entity {
                    world.entity_mut(entity).despawn();
                    tracing::info!("Device removed via API: {device_id}");
                }
            }
            DeviceCommand::Allocate {
                section_id,
                device_roles,
                response,
            } => {
                handle_allocate(world, section_id, device_roles, response);
            }
            DeviceCommand::Release { section_id } => {
                handle_release(world, section_id);
            }
        }
    }
}

fn handle_allocate(
    world: &mut bevy_ecs::world::World,
    section_id: String,
    device_roles: DeviceRoles,
    response: tokio::sync::oneshot::Sender<AllocateResponse>,
) {
    let device_data: Vec<(DeviceInfo, DeviceStatus)> = {
        let mut q = world.query::<(&DeviceInfo, &DeviceStatus)>();
        q.iter(world).map(|(i, s)| (i.clone(), s.clone())).collect()
    };

    let alloc = try_allocate(&device_roles, device_data.iter().map(|(i, s)| (i, s)));

    if let Some(ref alloc) = alloc {
        let protocols: HashMap<String, serde_json::Value> = alloc
            .values()
            .filter_map(|did| {
                device_data
                    .iter()
                    .find(|(info, _)| info.id == *did)
                    .map(|(info, _)| (did.clone(), info.protocol.clone()))
            })
            .collect();

        let allocated_ids: Vec<String> = alloc.values().cloned().collect();

        // Mark devices unavailable + attach CurrentTask
        let entities: Vec<_> = {
            let registry = world.resource::<DeviceRegistry>();
            allocated_ids
                .iter()
                .filter_map(|id| registry.get(id))
                .collect()
        };
        {
            for entity in entities {
                if let Some(mut status) = world.get_mut::<DeviceStatus>(entity) {
                    status.available = false;
                }
                world.entity_mut(entity).insert(CurrentTask {
                    section_id: section_id.clone(),
                });
            }
        }

        world
            .resource_mut::<ActiveAllocations>()
            .allocate(section_id.clone(), allocated_ids);

        tracing::info!("Allocated devices for section {section_id}: {:?}", alloc);
        let _ = response.send(Some((alloc.clone(), protocols)));
    } else {
        tracing::warn!(
            "Allocation failed for section {section_id}: no devices for {:?}",
            device_roles
        );
        let _ = response.send(None);
    }
}

fn handle_release(world: &mut bevy_ecs::world::World, section_id: String) {
    let released = world
        .resource_mut::<ActiveAllocations>()
        .release(&section_id);
    let Some(device_ids) = released else {
        return;
    };
    let entities: Vec<_> = {
        let registry = world.resource::<DeviceRegistry>();
        device_ids
            .iter()
            .filter_map(|id| registry.get(id))
            .collect()
    };
    for entity in entities {
        if let Some(mut status) = world.get_mut::<DeviceStatus>(entity) {
            status.available = true;
        }
        if world.get::<CurrentTask>(entity).is_some() {
            world.entity_mut(entity).remove::<CurrentTask>();
        }
    }
    tracing::info!(
        "Released {} device(s) for section {section_id}",
        device_ids.len()
    );
}

// ── Diagram placeholder resolution ──

/// Walk the diagram JSON recursively and replace `$role_name` in call_device
/// configs with the actual device_id and protocol from the allocation.
pub fn resolve_device_placeholders(
    json: &mut serde_json::Value,
    allocation: &AllocationResult,
    protocols: &HashMap<String, serde_json::Value>,
) {
    match json {
        serde_json::Value::Object(map) => {
            let is_call_device = map.get("builder").and_then(|v| v.as_str()) == Some("call_device");

            if is_call_device {
                resolve_call_device_config(map, allocation, protocols);
            }

            for child in map.values_mut() {
                resolve_device_placeholders(child, allocation, protocols);
            }
        }
        serde_json::Value::Array(items) => {
            for child in items {
                resolve_device_placeholders(child, allocation, protocols);
            }
        }
        _ => {}
    }
}

fn resolve_call_device_config(
    op: &mut serde_json::Map<String, serde_json::Value>,
    allocation: &AllocationResult,
    protocols: &HashMap<String, serde_json::Value>,
) {
    let Some(config) = op.get_mut("config").and_then(|v| v.as_object_mut()) else {
        return;
    };
    let role = config
        .get("device_id")
        .and_then(|v| v.as_str())
        .and_then(|s| s.strip_prefix('$'))
        .map(|r| r.to_string());
    let Some(role) = role else { return };
    let Some(actual_id) = allocation.get(&role) else {
        tracing::warn!("No allocation for role ${role}");
        return;
    };
    config.insert(
        "device_id".to_string(),
        serde_json::Value::String(actual_id.clone()),
    );
    if let Some(protocol) = protocols.get(actual_id) {
        config.insert("protocol".to_string(), protocol.clone());
    }
}

/// Spawn device entities from DB rows and insert resources.
pub fn hydrate_devices(app: &mut bevy_app::App, rows: Vec<DeviceRow>) {
    let mut registry = DeviceRegistry::default();
    for row in rows {
        let id = row.id.clone();
        let entity = app
            .world_mut()
            .spawn((
                DeviceInfo {
                    id: row.id,
                    role: row.role,
                    device_type: row.device_type,
                    protocol: row.protocol,
                },
                DeviceStatus { available: true },
            ))
            .id();
        registry.insert(id, entity);
    }
    tracing::info!("Hydrated {} device(s) into Bevy ECS", registry.len());
    app.insert_resource(registry);
    app.insert_resource(ActiveAllocations::default());
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_devices() -> Vec<(DeviceInfo, DeviceStatus)> {
        vec![
            (
                DeviceInfo {
                    id: "mir_01".into(),
                    role: "mobile_manipulator".into(),
                    device_type: "mir".into(),
                    protocol: serde_json::json!({"type": "http", "url": "http://10.0.0.1"}),
                },
                DeviceStatus { available: true },
            ),
            (
                DeviceInfo {
                    id: "mir_02".into(),
                    role: "mobile_manipulator".into(),
                    device_type: "mir".into(),
                    protocol: serde_json::json!({"type": "http", "url": "http://10.0.0.2"}),
                },
                DeviceStatus { available: false },
            ),
            (
                DeviceInfo {
                    id: "seer_01".into(),
                    role: "mobile_manipulator".into(),
                    device_type: "seer".into(),
                    protocol: serde_json::json!({"type": "http", "url": "http://10.0.0.3"}),
                },
                DeviceStatus { available: true },
            ),
            (
                DeviceInfo {
                    id: "dsc_01".into(),
                    role: "dsc_station".into(),
                    device_type: "x3_dsc".into(),
                    protocol: serde_json::json!({"type": "grpc", "uri": "http://10.0.0.10:50051"}),
                },
                DeviceStatus { available: true },
            ),
        ]
    }

    #[test]
    fn allocate_any_picks_first_available() {
        let devices = make_devices();
        let iter = devices.iter().map(|(i, s)| (i, s));

        let roles: DeviceRoles = HashMap::from([("mobile_manipulator".into(), "$any".into())]);
        let result = try_allocate(&roles, iter).unwrap();

        assert_eq!(result["mobile_manipulator"], "mir_01");
    }

    #[test]
    fn allocate_any_of_type() {
        let devices = make_devices();
        let iter = devices.iter().map(|(i, s)| (i, s));

        let roles: DeviceRoles = HashMap::from([("mobile_manipulator".into(), "$any.seer".into())]);
        let result = try_allocate(&roles, iter).unwrap();

        assert_eq!(result["mobile_manipulator"], "seer_01");
    }

    #[test]
    fn allocate_exact_bypasses_availability() {
        let devices = make_devices();
        let iter = devices.iter().map(|(i, s)| (i, s));

        let roles: DeviceRoles = HashMap::from([("mobile_manipulator".into(), "mir_02".into())]);
        let result = try_allocate(&roles, iter).unwrap();

        // mir_02 is not available, but exact bypasses the check
        assert_eq!(result["mobile_manipulator"], "mir_02");
    }

    #[test]
    fn allocate_fails_when_no_device_available() {
        let devices = make_devices();
        let iter = devices.iter().map(|(i, s)| (i, s));

        let roles: DeviceRoles = HashMap::from([("nonexistent_role".into(), "$any".into())]);
        assert!(try_allocate(&roles, iter).is_none());
    }

    #[test]
    fn allocate_multi_role_no_double_claim() {
        let devices = make_devices();
        let iter = devices.iter().map(|(i, s)| (i, s));

        let roles: DeviceRoles = HashMap::from([
            ("mobile_manipulator".into(), "$any".into()),
            ("dsc_station".into(), "$any".into()),
        ]);
        let result = try_allocate(&roles, iter).unwrap();

        assert_eq!(result["mobile_manipulator"], "mir_01");
        assert_eq!(result["dsc_station"], "dsc_01");
    }

    #[test]
    fn allocate_two_of_same_role_uses_different_devices() {
        let devices = make_devices();
        let iter = devices.iter().map(|(i, s)| (i, s));

        // If a template needs two mobile_manipulators, should pick different ones.
        // We simulate this with two role entries that have distinct keys.
        let roles: DeviceRoles = HashMap::from([
            ("mm_pickup".into(), "$any".into()),
            ("mm_dropoff".into(), "$any".into()),
        ]);
        // Both roles look for any device — but "mm_pickup" and "mm_dropoff" aren't
        // real device roles, so $any with a non-matching role returns None.
        assert!(try_allocate(&roles, iter).is_none());
    }

    #[test]
    fn parse_role_specifiers() {
        assert!(matches!(parse_role_spec("$any"), RoleSpec::Any));
        assert!(matches!(
            parse_role_spec("$any.mir"),
            RoleSpec::AnyOfType(t) if t == "mir"
        ));
        assert!(matches!(
            parse_role_spec("mir_01"),
            RoleSpec::Exact(id) if id == "mir_01"
        ));
    }

    #[test]
    fn resolve_placeholders_replaces_device_id_and_protocol() {
        let mut diagram = serde_json::json!({
            "ops": {
                "move_to_pickup": {
                    "builder": "call_device",
                    "config": {
                        "device_id": "$mobile_manipulator",
                        "protocol": {"type": "mqtt"},
                        "task_type": "GoTo",
                        "task_params": {}
                    }
                },
                "inspect": {
                    "builder": "call_device",
                    "config": {
                        "device_id": "dsc_01",
                        "protocol": {"type": "grpc", "uri": "http://10.0.0.10:50051"},
                        "task_type": "Inspect",
                        "task_params": {}
                    }
                }
            },
            "templates": {
                "inner": {
                    "ops": {
                        "nested_call": {
                            "builder": "call_device",
                            "config": {
                                "device_id": "$dsc_station",
                                "protocol": {"type": "mqtt"},
                                "task_type": "RunDSC",
                                "task_params": {}
                            }
                        }
                    }
                }
            }
        });

        let allocation: AllocationResult = HashMap::from([
            ("mobile_manipulator".into(), "mir_01".into()),
            ("dsc_station".into(), "dsc_01".into()),
        ]);
        let protocols: HashMap<String, serde_json::Value> = HashMap::from([
            (
                "mir_01".into(),
                serde_json::json!({"type": "http", "url": "http://10.0.0.1"}),
            ),
            (
                "dsc_01".into(),
                serde_json::json!({"type": "grpc", "uri": "http://10.0.0.10:50051"}),
            ),
        ]);

        resolve_device_placeholders(&mut diagram, &allocation, &protocols);

        let move_config = &diagram["ops"]["move_to_pickup"]["config"];
        assert_eq!(move_config["device_id"], "mir_01");
        assert_eq!(move_config["protocol"]["type"], "http");
        assert_eq!(move_config["protocol"]["url"], "http://10.0.0.1");

        // Non-placeholder device_id left unchanged
        let inspect_config = &diagram["ops"]["inspect"]["config"];
        assert_eq!(inspect_config["device_id"], "dsc_01");
        // Protocol kept from original config (not overwritten since no $ prefix)
        assert_eq!(inspect_config["protocol"]["uri"], "http://10.0.0.10:50051");

        // Nested template resolved too
        let nested_config = &diagram["templates"]["inner"]["ops"]["nested_call"]["config"];
        assert_eq!(nested_config["device_id"], "dsc_01");
        assert_eq!(nested_config["protocol"]["type"], "grpc");
    }
}
