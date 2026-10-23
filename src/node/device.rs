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

use crate::client::amqp::AmqpClientResource;
use crate::client::http::{EnsureHttp, HttpHandle};
use crate::client::mqtt::{EnsureMqtt, MqttHandle};
use crate::client::opcua::{EnsureOpcua, OpcuaHandle};
use crate::config::GrpcSettings;
use crate::executor::TokioHandle;

use crossflow::bevy_app;
use crossflow::bevy_ecs;
use crossflow::diagram::grpc::AbortOnDropExt;
use crossflow::prelude::*;
use crossflow::{ConfigExample, DiagramElementRegistry, NodeBuilderOptions};
use opcua::types::{DataValue, NodeId, ReadValueId, TimestampsToReturn, Variant, WriteValue};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::future::Future;
use thiserror::Error;

// ── Types ──

/// Which protocol to use when communicating with a device.
/// All protocols share the same per-device queue — requests to the same
/// device_id are serialized regardless of protocol.
#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema)]
#[serde(tag = "type")]
pub(crate) enum DeviceProtocol {
    #[serde(rename = "mqtt")]
    Mqtt,
    #[serde(rename = "opcua")]
    Opcua {
        uri: String,
        namespace: u16,
        status_node: String,
        command_node: String,
        #[serde(default = "default_idle_value")]
        idle_value: String,
        #[serde(default = "default_done_value")]
        done_value: String,
        #[serde(default = "default_error_value")]
        error_value: String,
    },
    #[serde(rename = "http")]
    Http { url: String },
    #[serde(rename = "amqp")]
    Amqp {
        #[serde(default = "default_amqp_exchange")]
        exchange: String,
        #[serde(default)]
        routing_key: String,
    },
    #[serde(rename = "grpc")]
    Grpc {
        uri: String,
        service: String,
        method: String,
    },
}

fn default_idle_value() -> String {
    "IDLE".into()
}
fn default_done_value() -> String {
    "COMPLETED".into()
}
fn default_error_value() -> String {
    "ERROR".into()
}
fn default_amqp_exchange() -> String {
    "@RECEIVE@".into()
}

impl Default for DeviceProtocol {
    fn default() -> Self {
        Self::Mqtt
    }
}

#[derive(Debug, Clone)]
pub(crate) struct DeviceRequest {
    pub device_id: String,
    pub protocol: DeviceProtocol,
    pub task_type: String,
    pub task_params: serde_json::Value,
}

#[derive(Debug, Clone, Serialize, Deserialize, JsonSchema, Error)]
pub(crate) enum DeviceError {
    #[error("Device {device_id} failed: {message}")]
    DeviceFailed { device_id: String, message: String },
    #[error("Subscribe error: {0}")]
    Subscribe(String),
    #[error("Publish error: {0}")]
    Publish(String),
    #[error("Protocol not configured: {0}")]
    NotConfigured(String),
    #[error("Internal error: {0}")]
    Internal(String),
}

/// Per-device delivery label — same device_id = queued, different = parallel
#[derive(Debug, Clone, PartialEq, Eq, Hash, DeliveryLabel)]
struct DeviceLabel(String);

// ── Config ──

#[derive(Serialize, Deserialize, Clone, JsonSchema)]
#[serde(deny_unknown_fields)]
struct CallDeviceConfig {
    pub device_id: String,
    #[serde(default)]
    pub protocol: DeviceProtocol,
    pub task_type: String,
    #[serde(default)]
    pub task_params: serde_json::Value,
}

// ── gRPC TLS handle ──

/// Bevy Resource that holds gRPC TLS config (read once at startup)
/// and provides a `connect(uri)` method for building channels.
#[derive(Clone, bevy_ecs::resource::Resource)]
pub(crate) struct GrpcDeviceHandle {
    tls_ca_cert: Option<Vec<u8>>,
    tls_domain: Option<String>,
    tls_identity: Option<(Vec<u8>, Vec<u8>)>,
}

impl GrpcDeviceHandle {
    pub fn from_settings(settings: &GrpcSettings) -> Self {
        let tls_ca_cert = settings.ca_cert.as_ref().map(|p| {
            std::fs::read(p).unwrap_or_else(|e| panic!("read gRPC CA cert {}: {e}", p.display()))
        });
        let tls_domain = settings.domain_name.clone();
        let tls_identity = match (&settings.client_cert, &settings.client_key) {
            (Some(cert), Some(key)) => Some((
                std::fs::read(cert).unwrap_or_else(|e| panic!("read gRPC client cert: {e}")),
                std::fs::read(key).unwrap_or_else(|e| panic!("read gRPC client key: {e}")),
            )),
            _ => None,
        };
        Self {
            tls_ca_cert,
            tls_domain,
            tls_identity,
        }
    }

    async fn connect(&self, uri: &str) -> Result<tonic::transport::Channel, DeviceError> {
        let endpoint = tonic::transport::Channel::from_shared(uri.to_string())
            .map_err(|e| DeviceError::Internal(format!("invalid gRPC uri: {e}")))?;

        let channel = if let Some(ca_cert) = &self.tls_ca_cert {
            let mut tls = tonic::transport::ClientTlsConfig::new()
                .ca_certificate(tonic::transport::Certificate::from_pem(ca_cert));
            if let Some(domain) = &self.tls_domain {
                tls = tls.domain_name(domain);
            }
            if let Some((cert, key)) = &self.tls_identity {
                tls = tls.identity(tonic::transport::Identity::from_pem(cert, key));
            }
            endpoint
                .tls_config(tls)
                .map_err(|e| DeviceError::Internal(format!("gRPC TLS config: {e}")))?
                .connect()
                .await
        } else {
            endpoint.connect().await
        };

        channel.map_err(|e| DeviceError::Internal(format!("gRPC connect: {e}")))
    }
}

// ── gRPC dynamic codec ──
// Minimal codec for unary calls using prost_reflect DynamicMessage.

struct DynCodec {
    output_desc: prost_reflect::MessageDescriptor,
}

struct DynEncoder;

struct DynDecoder(prost_reflect::MessageDescriptor);

impl tonic::codec::Encoder for DynEncoder {
    type Item = prost_reflect::DynamicMessage;
    type Error = tonic::Status;

    fn encode(
        &mut self,
        item: Self::Item,
        dst: &mut tonic::codec::EncodeBuf<'_>,
    ) -> Result<(), Self::Error> {
        prost::Message::encode(&item, dst)
            .map_err(|e| tonic::Status::internal(format!("encode: {e}")))
    }
}

impl tonic::codec::Decoder for DynDecoder {
    type Item = prost_reflect::DynamicMessage;
    type Error = tonic::Status;

    fn decode(
        &mut self,
        src: &mut tonic::codec::DecodeBuf<'_>,
    ) -> Result<Option<Self::Item>, Self::Error> {
        let mut msg = prost_reflect::DynamicMessage::new(self.0.clone());
        prost::Message::merge(&mut msg, src)
            .map_err(|e| tonic::Status::internal(format!("decode: {e}")))?;
        Ok(Some(msg))
    }
}

impl tonic::codec::Codec for DynCodec {
    type Encode = prost_reflect::DynamicMessage;
    type Decode = prost_reflect::DynamicMessage;
    type Encoder = DynEncoder;
    type Decoder = DynDecoder;

    fn encoder(&mut self) -> Self::Encoder {
        DynEncoder
    }

    fn decoder(&mut self) -> Self::Decoder {
        DynDecoder(self.output_desc.clone())
    }
}

// ── Service function ──
// Dispatches to the protocol-specific handler.
// All protocols share one Service entity, so DeviceLabel queueing works
// across protocols for the same device.

fn device_service_fn(
    AsyncService { request, .. }: AsyncService<DeviceRequest>,
    mqtt: Option<bevy_ecs::prelude::Res<MqttHandle>>,
    opcua: Option<bevy_ecs::prelude::Res<OpcuaHandle>>,
    http: Option<bevy_ecs::prelude::Res<HttpHandle>>,
    amqp: Option<bevy_ecs::prelude::Res<AmqpClientResource>>,
    grpc: Option<bevy_ecs::prelude::Res<GrpcDeviceHandle>>,
    rt: bevy_ecs::prelude::Res<TokioHandle>,
) -> impl Future<Output = Result<JsonMessage, DeviceError>> + use<> {
    let mqtt = mqtt.map(|r| r.clone());
    let opcua = opcua.map(|r| r.clone());
    let http = http.map(|r| r.clone());
    let amqp = amqp.map(|r| r.clone());
    let grpc = grpc.map(|r| r.clone());
    let rt = rt.0.clone();
    async move {
        match request.protocol.clone() {
            DeviceProtocol::Mqtt => {
                let handle = mqtt.ok_or(DeviceError::NotConfigured("MQTT".into()))?;
                mqtt_device_handler(request, handle, rt).await
            }
            DeviceProtocol::Opcua {
                uri,
                namespace,
                status_node,
                command_node,
                idle_value,
                done_value,
                error_value,
            } => {
                let handle = opcua.ok_or(DeviceError::NotConfigured("OPC UA".into()))?;
                opcua_device_handler(
                    request,
                    handle,
                    rt,
                    &uri,
                    namespace,
                    &status_node,
                    &command_node,
                    &idle_value,
                    &done_value,
                    &error_value,
                )
                .await
            }
            DeviceProtocol::Http { url } => {
                let handle = http.ok_or(DeviceError::NotConfigured("HTTP".into()))?;
                http_device_handler(request, handle, rt, &url).await
            }
            DeviceProtocol::Amqp {
                exchange,
                routing_key,
            } => {
                let client = amqp.ok_or(DeviceError::NotConfigured("AMQP".into()))?;
                amqp_device_handler(request, client, rt, &exchange, &routing_key).await
            }
            DeviceProtocol::Grpc {
                uri,
                service,
                method,
            } => {
                let handle = grpc.ok_or(DeviceError::NotConfigured("gRPC".into()))?;
                grpc_device_handler(request, handle, rt, &uri, &service, &method).await
            }
        }
    }
}

// ── MQTT handler ──
// Wait for IDLE on status topic → publish task → wait for COMPLETED/FAILED

async fn mqtt_device_handler(
    request: DeviceRequest,
    mqtt: MqttHandle,
    rt: tokio::runtime::Handle,
) -> Result<JsonMessage, DeviceError> {
    rt.spawn(async move {
        let status_topic = format!("asset/{}/asset_status", request.device_id);
        let request_topic = format!("asset/{}/task_request", request.device_id);
        let response_topic = format!("asset/{}/task_status", request.device_id);

        let mut status_rx = mqtt
            .subscribe(&status_topic, 0)
            .await
            .map_err(|e| DeviceError::Subscribe(e.to_string()))?;

        let mut response_rx = mqtt
            .subscribe(&response_topic, 0)
            .await
            .map_err(|e| DeviceError::Subscribe(e.to_string()))?;

        loop {
            let msg = match status_rx.recv().await {
                Ok(msg) => msg,
                Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                    tracing::warn!(
                        "call_device({}): status lagged {n} messages",
                        request.device_id
                    );
                    continue;
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                    return Err(DeviceError::Subscribe("Status channel closed".into()));
                }
            };
            match serde_json::from_slice::<serde_json::Value>(&msg) {
                Ok(update) => {
                    if update.get("state").and_then(|s| s.as_str()) == Some("IDLE") {
                        tracing::debug!("call_device({}): device is IDLE", request.device_id);
                        break;
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        "call_device({}): failed to parse status: {e}",
                        request.device_id
                    );
                }
            }
        }

        let payload = serde_json::json!({
            "asset_id": &request.device_id,
            "task_type": &request.task_type,
            "task_params": &request.task_params,
        });
        let data =
            serde_json::to_vec(&payload).map_err(|e| DeviceError::Internal(e.to_string()))?;

        mqtt.publish(&request_topic, data, 0, false)
            .await
            .map_err(|e| DeviceError::Publish(e.to_string()))?;

        tracing::debug!(
            "call_device({}): published request, waiting for response",
            request.device_id
        );

        loop {
            let msg = match response_rx.recv().await {
                Ok(msg) => msg,
                Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                    tracing::warn!(
                        "call_device({}): response lagged {n} messages",
                        request.device_id
                    );
                    continue;
                }
                Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                    return Err(DeviceError::Subscribe("Response channel closed".into()));
                }
            };
            match serde_json::from_slice::<serde_json::Value>(&msg) {
                Ok(update) => {
                    let status = update.get("status").and_then(|s| s.as_str());
                    match status {
                        Some("COMPLETED") => {
                            tracing::debug!("call_device({}): completed", request.device_id);
                            return Ok(serde_json::json!({
                                "status": "ok",
                                "device_id": request.device_id,
                            }));
                        }
                        Some("FAILED") => {
                            let error = update
                                .get("error")
                                .and_then(|e| e.as_str())
                                .unwrap_or("unknown");
                            return Err(DeviceError::DeviceFailed {
                                device_id: request.device_id,
                                message: error.to_string(),
                            });
                        }
                        _ => {}
                    }
                }
                Err(e) => {
                    tracing::warn!(
                        "call_device({}): failed to parse response: {e}",
                        request.device_id
                    );
                }
            }
        }
    })
    .abort_on_drop()
    .await
    .map_err(|e| DeviceError::Internal(e.to_string()))?
}

// ── OPC UA handler ──
// Read status node until idle → write command → poll status node until done/error

async fn opcua_device_handler(
    request: DeviceRequest,
    opcua: OpcuaHandle,
    rt: tokio::runtime::Handle,
    uri: &str,
    namespace: u16,
    status_node: &str,
    command_node: &str,
    idle_value: &str,
    done_value: &str,
    error_value: &str,
) -> Result<JsonMessage, DeviceError> {
    let uri = uri.to_string();
    let status_node = status_node.to_string();
    let command_node = command_node.to_string();
    let idle_value = idle_value.to_string();
    let done_value = done_value.to_string();
    let error_value = error_value.to_string();

    rt.spawn(async move {
        let session = opcua
            .session(&uri)
            .await
            .map_err(|e| DeviceError::Internal(e.to_string()))?;

        let status_node_id = NodeId::new(namespace, status_node.as_str());
        let command_node_id = NodeId::new(namespace, command_node.as_str());

        // Poll status node until device is idle
        loop {
            let results = session
                .read(
                    &[ReadValueId::from(status_node_id.clone())],
                    TimestampsToReturn::Neither,
                    0.0,
                )
                .await
                .map_err(|e| DeviceError::Internal(format!("opcua read: {e}")))?;

            if let Some(value) = results.first().and_then(|dv| dv.value.as_ref()) {
                let val_str = variant_to_string(value);
                if val_str == idle_value {
                    tracing::debug!("call_device({}): device is IDLE (opcua)", request.device_id);
                    break;
                }
                tracing::debug!(
                    "call_device({}): waiting for IDLE, got {}",
                    request.device_id,
                    val_str
                );
            }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }

        // Write command to the command node
        let command_payload = serde_json::json!({
            "task_type": &request.task_type,
            "task_params": &request.task_params,
        });
        let command_str = serde_json::to_string(&command_payload)
            .map_err(|e| DeviceError::Internal(e.to_string()))?;

        let results = session
            .write(&[WriteValue {
                node_id: command_node_id,
                attribute_id: 13,
                index_range: Default::default(),
                value: DataValue::from(Variant::String(command_str.into())),
            }])
            .await
            .map_err(|e| DeviceError::Publish(format!("opcua write: {e}")))?;

        if let Some(status) = results.first() {
            if !status.is_good() {
                return Err(DeviceError::Publish(format!(
                    "opcua write failed: {status}"
                )));
            }
        }

        tracing::debug!(
            "call_device({}): wrote command, polling for completion (opcua)",
            request.device_id
        );

        // Poll status node until done or error
        loop {
            let results = session
                .read(
                    &[ReadValueId::from(status_node_id.clone())],
                    TimestampsToReturn::Neither,
                    0.0,
                )
                .await
                .map_err(|e| DeviceError::Internal(format!("opcua read: {e}")))?;

            if let Some(value) = results.first().and_then(|dv| dv.value.as_ref()) {
                let val_str = variant_to_string(value);
                if val_str == done_value {
                    tracing::debug!("call_device({}): completed (opcua)", request.device_id);
                    return Ok(serde_json::json!({
                        "status": "ok",
                        "device_id": request.device_id,
                    }));
                }
                if val_str == error_value {
                    return Err(DeviceError::DeviceFailed {
                        device_id: request.device_id,
                        message: format!("device status: {val_str}"),
                    });
                }
            }
            tokio::time::sleep(std::time::Duration::from_millis(500)).await;
        }
    })
    .abort_on_drop()
    .await
    .map_err(|e| DeviceError::Internal(e.to_string()))?
}

fn variant_to_string(v: &Variant) -> String {
    match v {
        Variant::String(s) => s.to_string(),
        Variant::Boolean(b) => b.to_string(),
        Variant::SByte(n) => n.to_string(),
        Variant::Byte(n) => n.to_string(),
        Variant::Int16(n) => n.to_string(),
        Variant::UInt16(n) => n.to_string(),
        Variant::Int32(n) => n.to_string(),
        Variant::UInt32(n) => n.to_string(),
        Variant::Int64(n) => n.to_string(),
        Variant::UInt64(n) => n.to_string(),
        Variant::Float(n) => n.to_string(),
        Variant::Double(n) => n.to_string(),
        other => format!("{other:?}"),
    }
}

// ── HTTP handler ──
// POST task request to device endpoint, parse response

async fn http_device_handler(
    request: DeviceRequest,
    http: HttpHandle,
    rt: tokio::runtime::Handle,
    url: &str,
) -> Result<JsonMessage, DeviceError> {
    let url = url.to_string();

    rt.spawn(async move {
        let payload = serde_json::json!({
            "device_id": &request.device_id,
            "task_type": &request.task_type,
            "task_params": &request.task_params,
        });

        tracing::debug!("call_device({}): POST {} (http)", request.device_id, url);

        let response = http
            .request(
                reqwest::Method::POST,
                &url,
                [("Content-Type".into(), "application/json".into())].into(),
                Some(payload),
            )
            .await
            .map_err(|e| DeviceError::Publish(format!("http request: {e}")))?;

        if response.status.is_success() {
            tracing::debug!("call_device({}): completed (http)", request.device_id);
            let body: serde_json::Value =
                serde_json::from_str(&response.body).unwrap_or(serde_json::json!({
                    "status": "ok",
                    "device_id": request.device_id,
                    "raw": response.body,
                }));
            Ok(body)
        } else {
            Err(DeviceError::DeviceFailed {
                device_id: request.device_id,
                message: format!("{}: {}", response.status, response.body),
            })
        }
    })
    .abort_on_drop()
    .await
    .map_err(|e| DeviceError::Internal(e.to_string()))?
}

// ── AMQP handler ──
// Publish task request, wait for response via request_response pattern

async fn amqp_device_handler(
    request: DeviceRequest,
    client: AmqpClientResource,
    rt: tokio::runtime::Handle,
    exchange: &str,
    routing_key: &str,
) -> Result<JsonMessage, DeviceError> {
    let exchange = exchange.to_string();
    let routing_key = routing_key.to_string();

    rt.spawn(async move {
        let task_id = format!("{}:{}", request.device_id, request.task_type);

        let payload = serde_json::json!({
            "type": "TaskRequest",
            "id": format!("{task_id}:TaskRequest"),
            "taskType": &request.task_type,
            "taskCommand": "START",
            "taskParams": &request.task_params,
            "deviceId": &request.device_id,
        });
        let data =
            serde_json::to_vec(&payload).map_err(|e| DeviceError::Internal(e.to_string()))?;

        tracing::debug!(
            "call_device({}): publishing to {}/{} (amqp)",
            request.device_id,
            exchange,
            routing_key
        );

        let response_data = client
            .0
            .request_response(&exchange, &routing_key, &data, &task_id)
            .await
            .map_err(|e| DeviceError::Publish(format!("amqp request: {e}")))?;

        let response: serde_json::Value = serde_json::from_slice(&response_data)
            .map_err(|e| DeviceError::Internal(format!("parse response: {e}")))?;

        let status = response
            .get("status")
            .and_then(|s| {
                if let Some(obj) = s.as_object() {
                    obj.get("value").and_then(|v| v.as_str())
                } else {
                    s.as_str()
                }
            })
            .unwrap_or("UNKNOWN");

        match status {
            "COMPLETED" => {
                tracing::debug!("call_device({}): completed (amqp)", request.device_id);
                Ok(serde_json::json!({
                    "status": "ok",
                    "device_id": request.device_id,
                    "response": response,
                }))
            }
            "ERROR" => Err(DeviceError::DeviceFailed {
                device_id: request.device_id,
                message: response
                    .get("error")
                    .and_then(|e| e.as_str())
                    .unwrap_or("unknown")
                    .to_string(),
            }),
            other => {
                tracing::debug!(
                    "call_device({}): got status {other}, treating as ok (amqp)",
                    request.device_id
                );
                Ok(serde_json::json!({
                    "status": "ok",
                    "device_id": request.device_id,
                    "response": response,
                }))
            }
        }
    })
    .abort_on_drop()
    .await
    .map_err(|e| DeviceError::Internal(e.to_string()))?
}

// ── gRPC handler ──
// Look up proto method, encode task_params as DynamicMessage, unary call, decode response

async fn grpc_device_handler(
    request: DeviceRequest,
    grpc: GrpcDeviceHandle,
    rt: tokio::runtime::Handle,
    uri: &str,
    service: &str,
    method: &str,
) -> Result<JsonMessage, DeviceError> {
    let uri = uri.to_string();
    let service = service.to_string();
    let method = method.to_string();

    rt.spawn(async move {
        let pool = prost_reflect::DescriptorPool::global();
        let svc_desc = pool
            .get_service_by_name(&service)
            .ok_or(DeviceError::Internal(format!(
                "gRPC service not found: {service}"
            )))?;
        let method_desc =
            svc_desc
                .methods()
                .find(|m| m.name() == method)
                .ok_or(DeviceError::Internal(format!(
                    "gRPC method not found: {service}/{method}"
                )))?;

        let input_desc = method_desc.input();
        let output_desc = method_desc.output();

        let input_msg =
            prost_reflect::DynamicMessage::deserialize(input_desc, &request.task_params)
                .map_err(|e| DeviceError::Internal(format!("gRPC encode input: {e}")))?;

        tracing::debug!(
            "call_device({}): gRPC {}/{} → {}",
            request.device_id,
            service,
            method,
            uri
        );

        let channel = grpc.connect(&uri).await?;
        let mut client = tonic::client::Grpc::new(channel);
        client
            .ready()
            .await
            .map_err(|e| DeviceError::Internal(format!("gRPC not ready: {e}")))?;

        let path: tonic::codegen::http::uri::PathAndQuery = format!("/{}/{}", service, method)
            .parse()
            .map_err(|e| DeviceError::Internal(format!("invalid gRPC path: {e}")))?;

        let codec = DynCodec { output_desc };
        let response = client
            .unary(tonic::Request::new(input_msg), path, codec)
            .await
            .map_err(|e| DeviceError::Publish(format!("gRPC call: {e}")))?;

        let output_msg = response.into_inner();
        let json = serde_json::to_value(&output_msg)
            .map_err(|e| DeviceError::Internal(format!("gRPC decode output: {e}")))?;

        tracing::debug!("call_device({}): completed (grpc)", request.device_id);
        Ok(json)
    })
    .abort_on_drop()
    .await
    .map_err(|e| DeviceError::Internal(e.to_string()))?
}

// ── Registration ──

pub(crate) fn register(
    app: &mut bevy_app::App,
    registry: &mut DiagramElementRegistry,
    ensure_mqtt: EnsureMqtt,
    ensure_opcua: EnsureOpcua,
    ensure_http: EnsureHttp,
    grpc_settings: &GrpcSettings,
) {
    app.insert_resource(GrpcDeviceHandle::from_settings(grpc_settings));
    let device_service = app.spawn_service(device_service_fn);
    register_call_device(
        registry,
        ensure_mqtt,
        ensure_opcua,
        ensure_http,
        device_service,
    );
}

fn register_call_device(
    registry: &mut DiagramElementRegistry,
    ensure_mqtt: EnsureMqtt,
    ensure_opcua: EnsureOpcua,
    ensure_http: EnsureHttp,
    device_service: Service<DeviceRequest, Result<JsonMessage, DeviceError>>,
) {
    registry
        .register_node_builder(
            NodeBuilderOptions::new("call_device")
                .with_default_display_text("Call Device")
                .with_description(
                    "Send a task request to a device and wait for completion. \
                     Requests to the same device are automatically queued (one at a time). \
                     Different devices run in parallel. \
                     Supports MQTT, OPC UA, HTTP, AMQP, and gRPC protocols.",
                )
                .with_config_examples([
                    ConfigExample::new(
                        "MQTT device",
                        CallDeviceConfig {
                            device_id: "ManipulatorRobot1".into(),
                            protocol: DeviceProtocol::Mqtt,
                            task_type: "Depalletize".into(),
                            task_params: serde_json::json!({"pallet_id": "P-001"}),
                        },
                    ),
                    ConfigExample::new(
                        "OPC UA device",
                        CallDeviceConfig {
                            device_id: "CNC_Machine_01".into(),
                            protocol: DeviceProtocol::Opcua {
                                uri: "opc.tcp://192.168.1.100:4840".into(),
                                namespace: 2,
                                status_node: "DeviceStatus".into(),
                                command_node: "DeviceCommand".into(),
                                idle_value: "IDLE".into(),
                                done_value: "COMPLETED".into(),
                                error_value: "ERROR".into(),
                            },
                            task_type: "RunProgram".into(),
                            task_params: serde_json::json!({"program": "part_A"}),
                        },
                    ),
                    ConfigExample::new(
                        "HTTP device",
                        CallDeviceConfig {
                            device_id: "VisionSystem1".into(),
                            protocol: DeviceProtocol::Http {
                                url: "http://192.168.1.50:8080/api/tasks".into(),
                            },
                            task_type: "Inspect".into(),
                            task_params: serde_json::json!({"camera": "top"}),
                        },
                    ),
                    ConfigExample::new(
                        "AMQP device",
                        CallDeviceConfig {
                            device_id: "AMR_Fleet1".into(),
                            protocol: DeviceProtocol::Amqp {
                                exchange: "@RECEIVE@".into(),
                                routing_key: "".into(),
                            },
                            task_type: "GoTo".into(),
                            task_params: serde_json::json!({"location": "Station_A"}),
                        },
                    ),
                    ConfigExample::new(
                        "gRPC device",
                        CallDeviceConfig {
                            device_id: "PLC_Controller_01".into(),
                            protocol: DeviceProtocol::Grpc {
                                uri: "http://192.168.1.200:50051".into(),
                                service: "device.DeviceService".into(),
                                method: "ExecuteTask".into(),
                            },
                            task_type: "RunCycle".into(),
                            task_params: serde_json::json!({"cycle": "assembly_A"}),
                        },
                    ),
                ]),
            move |builder, config: CallDeviceConfig| {
                call_device_node(
                    builder,
                    config,
                    ensure_mqtt.clone(),
                    ensure_opcua.clone(),
                    ensure_http.clone(),
                    device_service,
                )
            },
        )
        .with_result();
}

// ── Node builder ──

fn call_device_node(
    builder: &mut Builder,
    config: CallDeviceConfig,
    ensure_mqtt: EnsureMqtt,
    ensure_opcua: EnsureOpcua,
    ensure_http: EnsureHttp,
    device_service: Service<DeviceRequest, Result<JsonMessage, DeviceError>>,
) -> Node<JsonMessage, Result<JsonMessage, DeviceError>> {
    // Queue the appropriate Ensure* based on protocol so the handle
    // exists by the time the service function runs
    match &config.protocol {
        DeviceProtocol::Mqtt => builder.commands().queue(ensure_mqtt),
        DeviceProtocol::Opcua { .. } => builder.commands().queue(ensure_opcua),
        DeviceProtocol::Http { .. } => builder.commands().queue(ensure_http),
        DeviceProtocol::Amqp { .. } | DeviceProtocol::Grpc { .. } => {}
    }

    let label = DeviceLabel(config.device_id.clone());
    let instructions = DeliveryInstructions::new(label);

    builder.create_io_scope(move |scope, builder| {
        let config = config.clone();
        builder
            .chain(scope.start)
            .map_block(move |_input: JsonMessage| DeviceRequest {
                device_id: config.device_id.clone(),
                protocol: config.protocol.clone(),
                task_type: config.task_type.clone(),
                task_params: config.task_params.clone(),
            })
            .then(device_service.instruct(instructions))
            .connect(scope.terminate);
    })
}
