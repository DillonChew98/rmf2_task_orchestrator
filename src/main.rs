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

use std::sync::Arc;
use tracing_subscriber::{layer::SubscriberExt, util::SubscriberInitExt};

use rmf2_task_orchestrator::client;
use rmf2_task_orchestrator::config::{Settings, load_base_configuration};
use rmf2_task_orchestrator::db::init_db;
use rmf2_task_orchestrator::{TemplateFolderSource, TemplateRegistry};
use rmf2_task_orchestrator::{WorkflowTracker, create_amqp_router, spawn, workflow_events_router};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let log_dir = std::env::var("LOG_DIR").unwrap_or_else(|_| "logs".into());
    let file_appender = tracing_appender::rolling::daily(&log_dir, "to.log");
    let (non_blocking, _guard) = tracing_appender::non_blocking(file_appender);

    tracing_subscriber::registry()
        .with(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| {
                format!(
                    "{}=info,{}::workflow_updates=debug",
                    env!("CARGO_CRATE_NAME"),
                    env!("CARGO_CRATE_NAME"),
                )
                .into()
            }),
        )
        .with(tracing_subscriber::fmt::layer())
        .with(
            tracing_subscriber::fmt::layer()
                .with_ansi(false)
                .with_writer(non_blocking),
        )
        .init();

    let config: Settings =
        load_base_configuration().map_err(|e| format!("Error loading config file: {e}"))?;

    let amqp_config = &config.amqp;
    let mqtt_config = None;
    let http_config = &config.http;
    let grpc_config = &config.grpc;
    let app_config = &config.app;

    let mut registry = TemplateRegistry::default();
    if let Some(dir) = &app_config.template_directory {
        let source = TemplateFolderSource::new(dir);
        registry.add_source(&source)
    }
    let registry = Arc::new(registry);
    let (executor_handle, editor_router, trace_rx) =
        spawn(amqp_config, mqtt_config, http_config, grpc_config).await?;

    let db_pool = match config.database {
        Some(ref db_config) => match sqlx::PgPool::connect(&db_config.url).await {
            Ok(pool) => {
                init_db(&pool)
                    .await
                    .map_err(|e| format!("Failed to init database: {e}"))?;
                tracing::info!("Connected to Postgres");
                Some(pool)
            }
            Err(e) => {
                tracing::warn!(
                    "Failed to connect to database: {e}. Workflow status will not be persisted."
                );
                None
            }
        },
        None => {
            tracing::info!("No database configured, workflow status will not be persisted");
            None
        }
    };

    let (tracker, tracker_handle) = WorkflowTracker::new(64, db_pool);
    tokio::spawn(tracker.run(trace_rx));

    let amqp_connection = client::AmqpConnection::new(amqp_config)
        .await
        .map_err(|e| format!("Failed to connect to AMQP broker: {e}"))?;

    let amqp_router = create_amqp_router(
        executor_handle,
        Arc::clone(&registry),
        tracker_handle.clone(),
    );
    tokio::spawn(client::run_consumer(
        amqp_connection,
        amqp_config.consumer.clone(),
        amqp_router,
    ));

    let app = editor_router.merge(workflow_events_router(tracker_handle));

    let listener = tokio::net::TcpListener::bind(http_config.addr())
        .await
        .map_err(|e| format!("Failed to bind to address: {e}"))?;

    let local_addr = listener.local_addr()?;
    tracing::info!("Server listening on: http://{}", local_addr);
    tracing::info!("Diagram editor: http://{}/", local_addr);
    tracing::info!("Executor API: POST http://{}/api/executor/run", local_addr);
    tracing::info!("Workflow SSE: http://{}/api/workflows/events", local_addr);
    axum::serve(listener, app).await?;

    Ok(())
}
