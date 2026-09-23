use crossflow::bevy_ecs;
use reqwest::{Certificate, Client, Method, StatusCode};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(Debug, PartialEq)]
pub struct HttpResponse {
    pub status: StatusCode,
    pub body: String,
}

#[derive(Debug, thiserror::Error)]
pub enum HttpError {
    #[error("invalid request: {}", with_causes(.0))]
    InvalidRequest(reqwest::Error),
    #[error("connect failed: {}", with_causes(.0))]
    Connect(reqwest::Error),
    #[error("outcome unknown: {}", with_causes(.0))]
    OutcomeUnknown(reqwest::Error),
}

fn with_causes(error: &dyn std::error::Error) -> String {
    let mut text = error.to_string();
    let mut cause = error.source();
    while let Some(inner) = cause {
        text.push_str(": ");
        text.push_str(&inner.to_string());
        cause = inner.source();
    }
    text
}

impl From<reqwest::Error> for HttpError {
    fn from(e: reqwest::Error) -> Self {
        if e.is_builder() {
            Self::InvalidRequest(e)
        } else if e.is_connect() {
            Self::Connect(e)
        } else {
            Self::OutcomeUnknown(e)
        }
    }
}

#[derive(serde::Deserialize, Clone)]
#[serde(default)]
pub struct HttpSettings {
    pub request_timeout_secs: u64,
    pub connect_timeout_secs: u64,
    pub ca_bundle: Option<PathBuf>,
}

impl Default for HttpSettings {
    fn default() -> Self {
        Self {
            request_timeout_secs: 30,
            connect_timeout_secs: 5,
            ca_bundle: None,
        }
    }
}

#[derive(Clone, bevy_ecs::resource::Resource)]
pub struct HttpHandle {
    inner: Client,
}

impl Default for HttpHandle {
    fn default() -> Self {
        Self::build(HttpSettings::default())
    }
}

impl HttpHandle {
    fn build(config: HttpSettings) -> Self {
        let mut builder = Client::builder()
            .connect_timeout(Duration::from_secs(config.connect_timeout_secs))
            .timeout(Duration::from_secs(config.request_timeout_secs))
            .pool_max_idle_per_host(0);
        if let Some(path) = &config.ca_bundle {
            let data = std::fs::read(path)
                .unwrap_or_else(|e| panic!("Failed to read CA bundle {}: {e}", path.display()));
            let certs = Certificate::from_pem_bundle(&data)
                .unwrap_or_else(|e| panic!("Invalid CA bundle {}: {e}", path.display()));
            for cert in certs {
                builder = builder.add_root_certificate(cert);
            }
        }
        Self {
            inner: builder.build().expect("Failed to build HTTP client"),
        }
    }

    pub async fn request(
        &self,
        method: Method,
        url: &str,
        headers: HashMap<String, String>,
        body: Option<serde_json::Value>,
    ) -> Result<HttpResponse, HttpError> {
        let mut req = self.inner.request(method.clone(), url);
        for (key, value) in headers {
            req = req.header(key, value);
        }
        if method != Method::GET
            && method != Method::DELETE
            && let Some(body) = body
        {
            req = req.json(&body);
        }

        let r = req.send().await?;
        let status = r.status();
        let body = r.text().await?;
        Ok(HttpResponse { status, body })
    }
}

#[derive(serde::Deserialize, Clone)]
pub struct HttpTomlFormat {
    pub http: HttpSettings,
}

#[derive(Clone)]
pub(crate) struct EnsureHttp(Arc<Mutex<Option<HttpSettings>>>);

impl EnsureHttp {
    pub(crate) fn new(config: Option<HttpSettings>) -> Self {
        Self(Arc::new(Mutex::new(
            config.or_else(|| Some(Self::load_config())),
        )))
    }
    fn load_config() -> HttpSettings {
        crate::config::load_base_configuration::<HttpTomlFormat>()
            .map(|c| c.http)
            .unwrap_or_default()
    }
}

impl bevy_ecs::system::Command for EnsureHttp {
    fn apply(self, world: &mut bevy_ecs::prelude::World) {
        if let Some(http_config) = self.0.lock().unwrap().take() {
            world.insert_resource(HttpHandle::build(http_config));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_default_settings() {
        let http = HttpHandle::build(HttpSettings::default());
        assert!(http.inner.get("https://test.com").build().is_ok())
    }

    #[test]
    #[should_panic]
    fn test_invalid_cert_path() {
        let settings = HttpSettings {
            ca_bundle: Some(PathBuf::from("/fake_directory/fake_certs.pem")),
            ..Default::default()
        };
        HttpHandle::build(settings);
    }

    async fn spawn_test_server() -> String {
        let app = axum::Router::new()
            .route(
                "/status",
                axum::routing::get(|| async { axum::Json(serde_json::json!({"status":"ok"})) }),
            )
            .route(
                "/echo",
                axum::routing::post(|body: axum::Json<serde_json::Value>| async move {
                    axum::Json(body.0)
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        format!("http://{addr}")
    }

    #[tokio::test]
    async fn test_get_request() {
        let base_url = spawn_test_server().await;
        let handle = HttpHandle::build(HttpSettings::default());
        let result = handle
            .request(
                reqwest::Method::GET,
                &format!("{base_url}/status"),
                HashMap::new(),
                None,
            )
            .await;
        assert_eq!(
            result.unwrap(),
            HttpResponse {
                status: StatusCode::OK,
                body: r#"{"status":"ok"}"#.to_string(),
            }
        );

        let result = handle
            .request(
                reqwest::Method::GET,
                &format!("{base_url}/error"),
                HashMap::new(),
                None,
            )
            .await;
        let resp = result.unwrap();
        assert_eq!(resp.status, StatusCode::NOT_FOUND);

        let result = handle
            .request(
                reqwest::Method::GET,
                "http://127.0.0.1:1",
                HashMap::new(),
                None,
            )
            .await;

        assert!(
            matches!(result, Err(HttpError::Connect(_))),
            "expected connect error"
        );
    }

    #[tokio::test]
    async fn test_connect_error_names_the_cause() {
        let result = HttpHandle::default()
            .request(
                reqwest::Method::GET,
                "http://127.0.0.1:1",
                HashMap::new(),
                None,
            )
            .await;

        let message = result.unwrap_err().to_string();
        assert!(message.contains("Connection refused"), "got: {message}");
    }

    #[tokio::test]
    async fn test_post_request() {
        let base_url = spawn_test_server().await;
        let handle = HttpHandle::build(HttpSettings::default());
        let payload = serde_json::json!({"foo": "boo"});
        let result = handle
            .request(
                reqwest::Method::POST,
                &format!("{base_url}/echo"),
                HashMap::new(),
                Some(payload.clone()),
            )
            .await;
        assert_eq!(
            result.unwrap(),
            HttpResponse {
                status: StatusCode::OK,
                body: r#"{"foo":"boo"}"#.to_string()
            }
        );
    }
}
