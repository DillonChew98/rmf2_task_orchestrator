use crate::client::http::{EnsureHttp, HttpError, HttpHandle};

use crossflow::bevy_ecs;
use crossflow::diagram::grpc::AbortOnDropExt;
use crossflow::{ConfigExample, DiagramElementRegistry, NodeBuilderOptions, bevy_app, prelude::*};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::num::NonZeroU32;
use std::time::Duration;

pub(crate) fn register(
    _app: &mut bevy_app::App,
    registry: &mut DiagramElementRegistry,
    ensure_http: EnsureHttp,
) {
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
#[serde(default, deny_unknown_fields)]
struct RetryPolicy {
    pub max_attempts: NonZeroU32,
    pub wait_secs: u64,
    pub retryable_statuses: Vec<u16>,
}

impl Default for RetryPolicy {
    fn default() -> Self {
        Self {
            max_attempts: NonZeroU32::new(5).unwrap(),
            wait_secs: 5,
            retryable_statuses: vec![408, 429, 503],
        }
    }
}

#[derive(Serialize, Deserialize, Clone, JsonSchema)]
#[serde(deny_unknown_fields)]
struct HttpRequestConfig {
    pub method: HttpMethod,
    /// If you leave this unset, the input must be `{"uri": "..."}`. An optional `body` key is the request body.
    pub uri: Option<String>,
    #[serde(default)]
    pub headers: HashMap<String, String>,
    pub body: Option<JsonMessage>,
    pub retry: Option<RetryPolicy>,
}

fn register_http_request_node(registry: &mut DiagramElementRegistry, ensure_http: EnsureHttp) {
    registry
        .register_node_builder(
            NodeBuilderOptions::new("http_request")
                .with_default_display_text("HTTP Request")
                .with_description("Make an outgoing HTTP request. If no body is specified in config, the upstream input is used as the request body. If no uri is specified in config, the upstream input must be {\"uri\": ...}.")
                .with_config_examples([
                    ConfigExample::new(
                        "Health check",
                        HttpRequestConfig {
                            method: HttpMethod::GET,
                            uri: Some("http://localhost:2727/health_check".into()),
                            headers: Default::default(),
                            body: None,
                            retry: None,
                        },
                    ),
                    ConfigExample::new(
                        "POST with JSON body",
                        HttpRequestConfig {
                            method: HttpMethod::POST,
                            uri: Some("http://localhost:8080/api/tasks".into()),
                            headers: [("Content-Type".into(), "application/json".into())].into(),
                            body: Some(serde_json::json!({
                                "task_type": "pick_and_place",
                                "asset_id": "ManipulatorRobot1"
                            })),
                            retry: None,
                        },
                    ),
                    ConfigExample::new(
                        "GET with retry",
                        HttpRequestConfig {
                            method: HttpMethod::GET,
                            uri: Some("http://localhost:8080/api/v2.0.0/status".into()),
                            headers: Default::default(),
                            body: None,
                            retry: Some(RetryPolicy::default()),
                        },
                    ),
                    ConfigExample::new(
                        "URI from input",
                        HttpRequestConfig {
                            method: HttpMethod::POST,
                            uri: None,
                            headers: Default::default(),
                            body: None,
                            retry: None,
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
                let (uri, input_body) = match config.uri {
                    Some(uri) => (uri, Some(request)),
                    None => {
                        let JsonMessage::Object(mut fields) = request else {
                            return Err("http_request: uri is unset in config, so the input must be an object with a uri field".into());
                        };
                        let Some(JsonMessage::String(uri)) = fields.remove("uri") else {
                            return Err("http_request: uri is unset in config and the input has no string uri field".into());
                        };
                        (uri, fields.remove("body"))
                    }
                };
                let body = config.body.or(input_body);
                //load "${NAME}" headers from env vars
                let mut headers = config.headers;
                for value in headers.values_mut() {
                    if let Some(name) = value.strip_prefix("${").and_then(|v| v.strip_suffix('}')) {
                        *value = std::env::var(name).map_err(|_| {
                            format!("http_request: header needs env var {name}, unset or not UTF-8")
                        })?;
                    }
                }
                request_with_retry(http, rt, method, uri, headers, body, config.retry).await
            }
        };
    builder.create_node(callback.into_callback())
}

async fn request_with_retry(
    http: HttpHandle,
    rt: tokio::runtime::Handle,
    method: reqwest::Method,
    uri: String,
    headers: HashMap<String, String>,
    body: Option<JsonMessage>,
    retry: Option<RetryPolicy>,
) -> Result<JsonMessage, String> {
    rt.spawn(async move {
        let (max_attempts, wait, retryable_statuses) = match &retry {
            Some(policy) => (
                policy.max_attempts.get(),
                Duration::from_secs(policy.wait_secs),
                policy.retryable_statuses.as_slice(),
            ),
            None => (1, Duration::ZERO, &[] as &[u16]),
        };

        let mut last_err = String::new();
        for attempt in 1..=max_attempts {
            if attempt > 1 {
                tokio::time::sleep(wait).await;
            }

            let result = http
                .request(method.clone(), &uri, headers.clone(), body.clone())
                .await;

            last_err = match result {
                Err(e @ HttpError::InvalidRequest(_)) => return Err(e.to_string()),
                Err(e @ HttpError::OutcomeUnknown(_))
                    if matches!(method, reqwest::Method::POST | reqwest::Method::PATCH)
                        && max_attempts > 1 =>
                {
                    return Err(format!("{e} (unsafe to retry {method})"));
                }
                Err(e) => e.to_string(),
                Ok(resp) if resp.status.is_success() => {
                    if resp.body.is_empty() {
                        return Ok(serde_json::json!({"status": resp.status.as_u16()}));
                    }
                    return Ok(serde_json::from_str(&resp.body)
                        .unwrap_or(JsonMessage::String(resp.body)));
                }
                Ok(resp) if retryable_statuses.contains(&resp.status.as_u16()) => {
                    format!("{}: {}", resp.status, resp.body)
                }
                Ok(resp) => return Err(format!("{}: {}", resp.status, resp.body)),
            };

            if attempt < max_attempts {
                tracing::warn!(
                    "http_request: attempt {attempt}/{max_attempts} failed, retrying in {wait:?}: {last_err}"
                );
            }
        }

        if max_attempts == 1 {
            Err(last_err)
        } else {
            Err(format!("failed after {max_attempts} attempts: {last_err}"))
        }
    })
    //stop retries after workflow ends
    .abort_on_drop()
    .await
    .map_err(|e| e.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::client::http::HttpSettings;
    use crate::executor::TokioHandle;
    use crossflow::{Diagram, testing::*};
    use reqwest::StatusCode;
    use serde_json::json;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    const REQUEST_TIMEOUT_SECS: u64 = 2;

    /// - `/service_unavailable_then_ok` (GET): 503 for the first two counted requests,
    ///   then 200.
    /// - `/not_found` (GET): always 404.
    /// - `/service_unavailable` (any method): always 503.
    /// - `/bad_gateway` (POST): always 502.
    /// - `/ok_after_timeout` (POST): 200, but only after the request timeout.
    /// - `/ok_after_500ms` (GET): 200 after 500 ms. Not counted.
    /// - `/echo_auth` (GET): 200 with `{"auth": <Authorization header>}`. Not counted.
    async fn spawn_test_server() -> (String, Arc<AtomicUsize>) {
        let request_count = Arc::new(AtomicUsize::new(0));
        let flaky = {
            let request_count = request_count.clone();
            move || {
                let request_count = request_count.clone();
                async move {
                    let request_number = request_count.fetch_add(1, Ordering::SeqCst) + 1;
                    if request_number <= 2 {
                        (StatusCode::SERVICE_UNAVAILABLE, "busy".to_string())
                    } else {
                        (StatusCode::OK, r#"{"ok":true}"#.to_string())
                    }
                }
            }
        };
        let always = |status: StatusCode| {
            let request_count = request_count.clone();
            move || {
                let request_count = request_count.clone();
                async move {
                    request_count.fetch_add(1, Ordering::SeqCst);
                    (status, "nope".to_string())
                }
            }
        };
        let slow = || async {
            tokio::time::sleep(Duration::from_millis(500)).await;
            (StatusCode::OK, r#"{"slow":true}"#.to_string())
        };
        let late = {
            let request_count = request_count.clone();
            move || {
                let request_count = request_count.clone();
                async move {
                    request_count.fetch_add(1, Ordering::SeqCst);
                    tokio::time::sleep(Duration::from_secs(REQUEST_TIMEOUT_SECS + 1)).await;
                    (StatusCode::OK, "too late".to_string())
                }
            }
        };
        let echo_auth = |headers: axum::http::HeaderMap| async move {
            let auth = headers
                .get("authorization")
                .and_then(|v| v.to_str().ok())
                .unwrap_or("");
            (StatusCode::OK, format!(r#"{{"auth":"{auth}"}}"#))
        };
        let app = axum::Router::new()
            .route("/service_unavailable_then_ok", axum::routing::get(flaky))
            .route("/echo_auth", axum::routing::get(echo_auth))
            .route(
                "/not_found",
                axum::routing::get(always(StatusCode::NOT_FOUND)),
            )
            .route(
                "/service_unavailable",
                axum::routing::any(always(StatusCode::SERVICE_UNAVAILABLE)),
            )
            .route(
                "/bad_gateway",
                axum::routing::post(always(StatusCode::BAD_GATEWAY)),
            )
            .route("/ok_after_timeout", axum::routing::post(late))
            .route("/ok_after_500ms", axum::routing::get(slow));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        (format!("http://{addr}"), request_count)
    }

    fn request_diagram(config: serde_json::Value) -> Diagram {
        Diagram::from_json(json!({
            "version": "0.1.0",
            "start": "request",
            "ops": {
                "request": {
                    "type": "node",
                    "builder": "http_request",
                    "config": config,
                    "next": "result"
                },
                "result": {
                    "type": "fork_result",
                    "ok": { "builtin": "terminate" },
                    "err": { "builtin": "terminate" }
                }
            }
        }))
        .unwrap()
    }

    fn run(diagram: &Diagram, input: serde_json::Value) -> (JsonMessage, TestingContext) {
        let mut ctx = TestingContext::minimal_plugins();
        ctx.app
            .insert_resource(TokioHandle(tokio::runtime::Handle::current()));
        let mut registry = DiagramElementRegistry::new();
        let settings = HttpSettings {
            request_timeout_secs: REQUEST_TIMEOUT_SECS,
            ..Default::default()
        };
        let ensure_http = EnsureHttp::new(Some(settings));
        register(&mut ctx.app, &mut registry, ensure_http);

        let workflow = ctx
            .command(|cmds| {
                diagram
                    .spawn_io_workflow::<JsonMessage, JsonMessage>(cmds, &registry)
                    .map_err(Box::new)
            })
            .unwrap();
        let mut outcome = ctx.command(|cmds| cmds.request(input, workflow).outcome());
        ctx.run_with_conditions(&mut outcome, Duration::from_secs(10));
        let out = outcome
            .try_recv()
            .expect("workflow did not respond")
            .expect("workflow was cancelled");
        (out, ctx)
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_policy_retries_only_listed_statuses() {
        let (base_url, request_count) = spawn_test_server().await;

        let (out, _) = run(
            &request_diagram(json!({
                "method": "GET",
                "uri": format!("{base_url}/service_unavailable_then_ok"),
                "retry": {"max_attempts": 3, "wait_secs": 0}
            })),
            json!({}),
        );
        assert_eq!(out, json!({"ok": true}));
        assert_eq!(request_count.load(Ordering::SeqCst), 3);

        request_count.store(0, Ordering::SeqCst);
        let (out, _) = run(
            &request_diagram(json!({
                "method": "GET",
                "uri": format!("{base_url}/not_found"),
                "retry": {"max_attempts": 3, "wait_secs": 0}
            })),
            json!({}),
        );
        assert!(
            out.as_str().unwrap_or_default().contains("404"),
            "got: {out}"
        );
        assert_eq!(request_count.load(Ordering::SeqCst), 1);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_no_policy() {
        let (base_url, request_count) = spawn_test_server().await;

        let (out, _) = run(
            &request_diagram(
                json!({"method": "GET", "uri": format!("{base_url}/service_unavailable")}),
            ),
            json!({}),
        );

        assert!(
            out.as_str().unwrap_or_default().contains("503"),
            "got: {out}"
        );
        assert_eq!(request_count.load(Ordering::SeqCst), 1);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_post_retries() {
        let (base_url, request_count) = spawn_test_server().await;
        let (out, _) = run(
            &request_diagram(json!({
                "method": "POST",
                "uri": "http://127.0.0.1:1/api",
                "retry": {"max_attempts": 2, "wait_secs": 0}
            })),
            json!({}),
        );

        assert!(
            out.as_str()
                .unwrap_or_default()
                .contains("failed after 2 attempts"),
            "should have retried, got: {out}"
        );

        run(
            &request_diagram(json!({
                "method":"POST",
                "uri":format!("{base_url}/service_unavailable"),
                "retry":{"max_attempts":3,"wait_secs":0}
            })),
            json!({}),
        );
        assert_eq!(request_count.load(Ordering::SeqCst), 3);

        request_count.store(0, Ordering::SeqCst);
        run(
            &request_diagram(json!({
                "method": "POST",
                "uri": format!("{base_url}/bad_gateway"),
                "retry": {"max_attempts": 3, "wait_secs": 0, "retryable_statuses": [502]}
            })),
            json!({}),
        );
        assert_eq!(request_count.load(Ordering::SeqCst), 3);

        request_count.store(0, Ordering::SeqCst);
        run(
            &request_diagram(json!({
                "method": "POST",
                "uri": format!("{base_url}/ok_after_timeout"),
                "retry": {"max_attempts": 3, "wait_secs": 0}
            })),
            json!({}),
        );
        assert_eq!(request_count.load(Ordering::SeqCst), 1);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_terminate_stops_the_retries() {
        //slow branch terminates workflow after 500ms, request_count should not
        //increase after workflow is terminated
        let (base_url, request_count) = spawn_test_server().await;

        let diagram = Diagram::from_json(json!({
            "version": "0.1.0",
            "start": "fork",
            "ops": {
                "fork": { "type": "fork_clone", "next": ["retrying", "slow"] },
                "retrying": {
                    "type": "node",
                    "builder": "http_request",
                    "config": {
                        "method": "GET",
                        "uri": format!("{base_url}/service_unavailable"),
                        "retry": {"wait_secs": 1}
                    },
                    "next": "retrying_result"
                },
                "retrying_result": {
                    "type": "fork_result",
                    "ok": { "builtin": "terminate" },
                    "err": { "builtin": "terminate" }
                },
                "slow": {
                    "type": "node",
                    "builder": "http_request",
                    "config": { "method": "GET", "uri": format!("{base_url}/ok_after_500ms") },
                    "next": "slow_result"
                },
                "slow_result": {
                    "type": "fork_result",
                    "ok": { "builtin": "terminate" },
                    "err": { "builtin": "terminate" }
                }
            }
        }))
        .unwrap();
        let (out, mut ctx) = run(&diagram, json!({}));
        assert_eq!(out, json!({"slow": true}));

        ctx.run(Duration::from_secs(2));

        assert_eq!(
            request_count.load(Ordering::SeqCst),
            1,
            "a retry ran after the workflow ended"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_dynamic_uri() {
        let (base_url, _) = spawn_test_server().await;

        let diagram = Diagram::from_json(json!({
            "version": "0.1.0",
            "start": "target",
            "ops": {
                "target": {
                    "type": "transform",
                    "cel": format!("{{'uri': '{base_url}/ok_after_500ms'}}"),
                    "next": "request"
                },
                "request": {
                    "type": "node",
                    "builder": "http_request",
                    "config": {"method": "GET"},
                    "next": "result"
                },
                "result": {
                    "type": "fork_result",
                    "ok": { "builtin": "terminate" },
                    "err": { "builtin": "terminate" }
                }
            }
        }))
        .unwrap();
        let (out, _) = run(&diagram, json!({}));

        assert_eq!(out, json!({"slow": true}));
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn test_header_placeholder_reads_env() {
        let (base_url, _) = spawn_test_server().await;

        let diagram = request_diagram(json!({
            "method": "GET",
            "uri": format!("{base_url}/echo_auth"),
            "headers": { "Authorization": "${CARGO_PKG_NAME}" }
        }));
        let (out, _) = run(&diagram, json!({}));

        assert_eq!(out, json!({"auth": env!("CARGO_PKG_NAME")}));
    }
}
