use crossflow::bevy_ecs;
use opcua::client::{ClientBuilder, IdentityToken, Session};
use opcua::crypto::SecurityPolicy;
use opcua::types::{MessageSecurityMode, UserTokenPolicy};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

#[derive(Clone, bevy_ecs::resource::Resource)]
pub struct OpcuaHandle {
    sessions: Arc<tokio::sync::Mutex<HashMap<String, Arc<Session>>>>,
}

impl Default for OpcuaHandle {
    fn default() -> Self {
        Self {
            sessions: Arc::new(tokio::sync::Mutex::new(HashMap::new())),
        }
    }
}

impl OpcuaHandle {
    pub async fn session(&self, uri: &str) -> Result<Arc<Session>, String> {
        let mut sessions = self.sessions.lock().await;
        if let Some(session) = sessions.get(uri) {
            return Ok(session.clone());
        }

        let mut client = ClientBuilder::new()
            .application_name("RMF2 Task Orchestrator")
            .application_uri("urn:rmf2-task-orchestrator")
            .trust_server_certs(true)
            .create_sample_keypair(true)
            .session_retry_limit(3)
            .client()
            .map_err(|e| format!("opcua: failed to create client: {e:?}"))?;

        let (session, event_loop) = client
            .connect_to_matching_endpoint(
                (
                    uri,
                    SecurityPolicy::None.to_str(),
                    MessageSecurityMode::None,
                    UserTokenPolicy::anonymous(),
                ),
                IdentityToken::Anonymous,
            )
            .await
            .map_err(|e| format!("opcua: failed to connect to {uri}: {e}"))?;

        event_loop.spawn();
        session.wait_for_connection().await;

        sessions.insert(uri.to_string(), session.clone());
        Ok(session)
    }
}

#[derive(Clone)]
pub(crate) struct EnsureOpcua(Arc<Mutex<bool>>);

impl EnsureOpcua {
    pub(crate) fn new() -> Self {
        Self(Arc::new(Mutex::new(true)))
    }
}

impl bevy_ecs::system::Command for EnsureOpcua {
    fn apply(self, world: &mut bevy_ecs::prelude::World) {
        let mut pending = self.0.lock().unwrap();
        if *pending {
            world.insert_resource(OpcuaHandle::default());
            *pending = false;
        }
    }
}
