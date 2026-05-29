#[derive(serde::Deserialize, Clone)]
pub struct AmqpSettings {
    pub host: String,
    pub port: u16,
    pub consumer: ConsumerSettings,
}

#[derive(serde::Deserialize, Clone)]
pub struct ConsumerSettings {
    pub exchange: String,
    pub queue: String,
    #[serde(default)]
    pub routing_key: String,
    #[serde(default = "default_exchange_kind")]
    pub exchange_kind: String,
}

fn default_exchange_kind() -> String {
    "topic".to_string()
}

impl AmqpSettings {
    pub fn to_url(&self) -> String {
        format!("amqp://{}:{}", self.host, self.port)
    }
}
