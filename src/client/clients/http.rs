use crossflow::bevy_ecs;
use reqwest::{Certificate, Client, Method};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};
use std::time::Duration;

#[derive(serde::Deserialize, Clone, Default)]
pub struct HttpSettings {
    pub timeout_secs: Option<u64>,
    pub ca_bundle: Option<PathBuf>,
}

#[derive(Clone, bevy_ecs::resource::Resource)]
pub struct HttpHandle {
    inner: Client,
}

impl Default for HttpHandle {
    fn default() -> Self {
        Self {
            inner: Client::new(),
        }
    }
}

impl HttpHandle {
    fn build(config: HttpSettings) -> Self {
        let mut builder = Client::builder();
        if let Some(timeout) = config.timeout_secs {
            builder = builder.timeout(Duration::from_secs(timeout));
        }
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
        body: Option<serde_json::Value>
    ) -> Result<serde_json::Value, String> {
        let mut req = self.inner.request(method.clone(), url);
        for (key, value) in headers {
            req = req.header(key, value);

        }
        if method != Method::GET && method != Method::DELETE {
            if let Some(body) = body {
                req = req.json(&body);
            }
        }
        
        let response = req.send().await.map_err(|e| e.to_string())?;
        let status = response.status();
        if !status.is_success() {
            let body = response.text().await.unwrap_or_default();
            return Err(format!("{status}:{body}"));
        }
        response.json().await.map_err(|e| e.to_string())
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
            timeout_secs: None,
            ca_bundle: Some(PathBuf::from("/fake_directory/fake_certs.pem")),
        };
        HttpHandle::build(settings);
    }

    async fn spawn_test_server() -> String {
        let app = axum::Router::new()
            .route("/status", axum::routing::get(|| async {
                axum::Json(serde_json::json!({"status":"ok"}))
            }))
            .route("/echo", axum::routing::post(|body: axum::Json<serde_json::Value>| async move {
                axum::Json(body.0)
            }));
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
        let result = handle.request(reqwest::Method::GET, &format!("{base_url}/status"), HashMap::new(), None).await;
        assert_eq!(result.unwrap(), serde_json::json!({"status":"ok"}));

        let result = handle.request(reqwest::Method::GET, &format!("{base_url}/error"), HashMap::new(), None).await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_post_request() {
        let base_url = spawn_test_server().await;
        let handle = HttpHandle::build(HttpSettings::default());
        let payload = serde_json::json!({"foo": "boo"});
        let result = handle.request(reqwest::Method::POST, &format!("{base_url}/echo"), HashMap::new(), Some(payload.clone())).await;
        assert_eq!(result.unwrap(), payload);
    }
}

