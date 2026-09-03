use crate::client::http::{EnsureHttp, HttpHandle, HttpSettings};

use crossflow::bevy_ecs;
use crossflow::{ConfigExample, DiagramElementRegistry, NodeBuilderOptions, bevy_app, prelude::*};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;

pub(crate) fn register(
    _app: &mut bevy_app::App,
    registry: &mut DiagramElementRegistry,
    http_config: Option<HttpSettings>,
) {
    let ensure_http = EnsureHttp::new(http_config);
    register_http_request_node(registry, ensure_http);
}

#[derive(JsonSchema, Serialize, Deserialize, Clone)]
#[allow(clippy::upper_case_acronyms)]
pub enum HttpMethod {
    GET,
    POST,
    PUT,
    PATCH,
    DELETE,
}

impl From<HttpMethod> for reqwest::Method {
    fn from(method: HttpMethod) -> reqwest::Method {
        match method {
            HttpMethod::GET => reqwest::Method::GET,
            HttpMethod::POST => reqwest::Method::POST,
            HttpMethod::PUT => reqwest::Method::PUT,
            HttpMethod::PATCH => reqwest::Method::PATCH,
            HttpMethod::DELETE => reqwest::Method::DELETE,
        }
    }
}

#[derive(Serialize, Deserialize, Clone, JsonSchema)]
struct HttpRequestConfig {
    pub method: HttpMethod,
    pub uri: String,
    #[serde(default)]
    pub headers: HashMap<String, String>,
    pub body: Option<JsonMessage>,
}

fn register_http_request_node(registry: &mut DiagramElementRegistry, ensure_http: EnsureHttp) {
    registry
        .register_node_builder(
            NodeBuilderOptions::new("http_request")
                .with_default_display_text("HTTP Request")
                .with_description("Make an outgoing HTTP request. If no body is specified in config, the upstream input is used as the request body.")
                .with_config_examples([
                    ConfigExample::new(
                        "Health check",
                        HttpRequestConfig {
                            method: HttpMethod::GET,
                            uri: "http://localhost:2727/health_check".into(),
                            headers: Default::default(),
                            body: None,
                        },
                    ),
                    ConfigExample::new(
                        "POST with JSON body",
                        HttpRequestConfig {
                            method: HttpMethod::POST,
                            uri: "http://localhost:8080/api/tasks".into(),
                            headers: [("Content-Type".into(), "application/json".into())].into(),
                            body: Some(serde_json::json!({
                                "task_type": "pick_and_place",
                                "asset_id": "ManipulatorRobot1"
                            })),
                        },
                    ),
                ]),
            move |builder, config: HttpRequestConfig| {
                http_request_node(builder, config, ensure_http.clone())
            },
        )
        .with_result();
}

fn http_request_node(
    builder: &mut Builder,
    config: HttpRequestConfig,
    ensure_http: EnsureHttp,
) -> Node<JsonMessage, Result<JsonMessage, String>> {
    builder.commands().queue(ensure_http);

    let callback =
        move |Async { request, .. }: Async<JsonMessage>,
              http: bevy_ecs::prelude::Res<HttpHandle>,
              rt: bevy_ecs::prelude::Res<crate::executor::TokioHandle>| {
            let http = http.clone();
            let rt = rt.0.clone();
            let config = config.clone();
            async move {
                let method: reqwest::Method = config.method.into();
                let body = config.body.or(Some(request));
                rt.spawn(async move {
                    http.request(method, &config.uri, config.headers, body)
                        .await
                })
                .await
                .map_err(|e| e.to_string())?
            }
        };
    builder.create_node(callback.into_callback())
}
