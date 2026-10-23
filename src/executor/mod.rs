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

mod amqp_handlers;

use crate::TemplateRegistry;
use crate::WorkflowTrackerHandle;
use crate::client::amqp::AmqpClientResource;
use crate::client::http::EnsureHttp;
use crate::client::mqtt::{EnsureMqtt, MqttSettings};
use crate::client::opcua::EnsureOpcua;
use crate::client::{AmqpClient, AmqpRouter};
use crate::config::{AmqpSettings, GrpcSettings, HttpSettings};
use crate::device_registry::{self, DeviceCommand, DeviceRow};
use crate::node;
use crate::workflow_updates::setup_trace_forwarding;
use amqp_handlers::handle_workflow_execute;

use std::sync::Arc;
use std::{thread, time::Duration};

use axum::Router;
use crossflow::bevy_time::TimePlugin;
use crossflow::trace::TracedEvent;
use crossflow::{CrossflowExecutorApp, DiagramElementRegistry, bevy_app, bevy_ecs};
use crossflow_diagram_editor::{ServerOptions, new_router};
use tokio::sync::{mpsc, oneshot};

#[derive(Clone)]
pub struct ExecutorHandle {
    pub executor_url: String,
}

#[derive(bevy_ecs::prelude::Resource)]
pub(crate) struct TokioHandle(pub(crate) tokio::runtime::Handle);

// Spawn the Bevy executor in a separate thread
pub async fn spawn(
    amqp_config: &AmqpSettings,
    mqtt_config: Option<MqttSettings>,
    http_config: &HttpSettings,
    grpc_config: &GrpcSettings,
    devices: Vec<DeviceRow>,
) -> Result<
    (
        ExecutorHandle,
        Router,
        mpsc::UnboundedReceiver<TracedEvent>,
        std::sync::mpsc::Sender<DeviceCommand>,
    ),
    String,
> {
    let amqp_client = create_amqp_client(amqp_config).await?;
    let (router_tx, router_rx) = oneshot::channel();
    let (trace_tx, trace_rx) = mpsc::unbounded_channel();
    let (device_cmd_tx, device_cmd_rx) = std::sync::mpsc::channel();
    let tokio_handle = TokioHandle(tokio::runtime::Handle::current());
    let grpc_config = grpc_config.clone();
    let executor_url = String::from(http_config);

    thread::spawn(move || {
        let mut app = bevy_app::App::new();
        app.add_plugins((CrossflowExecutorApp::default(), TimePlugin));
        app.insert_resource(tokio_handle);
        app.insert_resource(AmqpClientResource(amqp_client.clone()));
        device_registry::hydrate_devices(&mut app, devices);
        setup_trace_forwarding(&mut app, trace_tx);

        let ensure_mqtt = EnsureMqtt::new(mqtt_config);
        let ensure_opcua = EnsureOpcua::new();
        let ensure_http = EnsureHttp::new(None);

        let mut registry = DiagramElementRegistry::new();
        node::amqp::register(&mut registry, amqp_client);
        node::mqtt::register(&mut app, &mut registry, ensure_mqtt.clone());
        node::http::register(&mut app, &mut registry, ensure_http.clone());
        node::opcua::register(&mut app, &mut registry, ensure_opcua.clone());
        node::device::register(
            &mut app,
            &mut registry,
            ensure_mqtt,
            ensure_opcua,
            ensure_http,
            &grpc_config,
        );
        node::utils::register(&mut registry);
        {
            let grpc_runtime = std::sync::Arc::new(
                tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(2)
                    .enable_all()
                    .build()
                    .expect("failed to create gRPC tokio runtime"),
            );
            // Leak one Arc ref so the runtime is never dropped in tokio's async
            // context (the router carries a clone back to the #[tokio::main] thread;
            // dropping a Runtime there panics). Safe: the TO runs for the process lifetime.
            std::mem::forget(grpc_runtime.clone());
            node::grpc::register(&mut registry, grpc_runtime, &grpc_config);
        }

        let diagram_editor_router = new_router(&mut app, registry, ServerOptions::default());
        let _ = router_tx.send(diagram_editor_router);

        loop {
            app.update();
            device_registry::process_device_commands(&mut app, &device_cmd_rx);
            thread::sleep(Duration::from_millis(1));
        }
    });

    let diagram_editor_router = router_rx
        .await
        .map_err(|_| "Failed to spawn executor, channel closed".to_string())?;

    let handle = ExecutorHandle { executor_url };

    Ok((handle, diagram_editor_router, trace_rx, device_cmd_tx))
}

pub fn create_amqp_router(
    handle: ExecutorHandle,
    registry: Arc<TemplateRegistry>,
    tracker: WorkflowTrackerHandle,
    device_cmd_tx: std::sync::mpsc::Sender<DeviceCommand>,
) -> AmqpRouter {
    AmqpRouter::default().route("", {
        let handle = handle.clone();
        let registry = Arc::clone(&registry);
        let tracker = tracker.clone();
        move |data| {
            let handle = handle.clone();
            let registry = Arc::clone(&registry);
            let tracker = tracker.clone();
            let device_cmd_tx = device_cmd_tx.clone();
            handle_workflow_execute(handle, registry, tracker, device_cmd_tx, data)
        }
    })
}

pub async fn create_amqp_client(amqp_config: &AmqpSettings) -> Result<Arc<AmqpClient>, String> {
    AmqpClient::connect(
        &String::from(amqp_config),
        "@RECEIVE@",
        "@RECEIVE@-task-responses",
    )
    .await
    .map(Arc::new)
    .map_err(|e| format!("Failed to connect to AMQP: {e}"))
}
