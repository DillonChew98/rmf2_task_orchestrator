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

use axum::Json;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::routing::{delete, get};
use serde::Deserialize;

use crate::device_registry::{self, DeviceCommand, DeviceRow};

#[derive(Clone)]
pub struct DeviceApiState {
    pub db: sqlx::PgPool,
    pub device_cmd_tx: std::sync::mpsc::Sender<DeviceCommand>,
}

#[derive(Deserialize)]
struct AddDeviceRequest {
    id: String,
    role: String,
    device_type: String,
    protocol: serde_json::Value,
}

async fn list_devices(
    State(state): State<DeviceApiState>,
) -> Result<Json<Vec<DeviceRow>>, StatusCode> {
    device_registry::list_all_devices(&state.db)
        .await
        .map(Json)
        .map_err(|e| {
            tracing::error!("Failed to list devices: {e}");
            StatusCode::INTERNAL_SERVER_ERROR
        })
}

async fn add_device(
    State(state): State<DeviceApiState>,
    Json(req): Json<AddDeviceRequest>,
) -> Result<(StatusCode, Json<DeviceRow>), (StatusCode, String)> {
    let row = DeviceRow {
        id: req.id,
        role: req.role,
        device_type: req.device_type,
        protocol: req.protocol,
        enabled: true,
    };

    device_registry::insert_device(&state.db, &row)
        .await
        .map_err(|e| {
            tracing::error!("Failed to insert device: {e}");
            (StatusCode::CONFLICT, format!("Failed to add device: {e}"))
        })?;

    let _ = state.device_cmd_tx.send(DeviceCommand::Add(row.clone()));

    Ok((StatusCode::CREATED, Json(row)))
}

async fn remove_device(
    State(state): State<DeviceApiState>,
    Path(device_id): Path<String>,
) -> Result<StatusCode, (StatusCode, String)> {
    let deleted = device_registry::delete_device(&state.db, &device_id)
        .await
        .map_err(|e| {
            tracing::error!("Failed to delete device: {e}");
            (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("Failed to delete device: {e}"),
            )
        })?;

    if !deleted {
        return Err((
            StatusCode::NOT_FOUND,
            format!("Device {device_id} not found"),
        ));
    }

    let _ = state
        .device_cmd_tx
        .send(DeviceCommand::Remove { device_id });

    Ok(StatusCode::NO_CONTENT)
}

pub fn device_router(state: DeviceApiState) -> axum::Router {
    axum::Router::new()
        .route("/api/devices", get(list_devices).post(add_device))
        .route("/api/devices/{id}", delete(remove_device))
        .with_state(state)
}
