use crate::client::opcua::{EnsureOpcua, OpcuaHandle};
use crate::node::utils::MessageStream;

use crossflow::bevy_ecs;
use crossflow::diagram::grpc::AbortOnDropExt;
use crossflow::{ConfigExample, DiagramElementRegistry, NodeBuilderOptions, prelude::*};
use opcua::client::{DataChangeCallback, MonitoredItem};
use opcua::types::{
    DataValue, MonitoredItemCreateRequest, NodeId, ReadValueId, TimestampsToReturn, Variant,
    WriteValue,
};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use std::time::Duration;

pub(crate) fn register(_app: &mut crossflow::bevy_app::App, registry: &mut DiagramElementRegistry) {
    let ensure = EnsureOpcua::new();
    register_opcua_read(registry, ensure.clone());
    register_opcua_write(registry, ensure.clone());
    register_opcua_subscribe(registry, ensure);
}

// ── Config types ──

#[derive(Serialize, Deserialize, Clone, JsonSchema)]
#[serde(deny_unknown_fields)]
struct OpcuaReadConfig {
    pub uri: String,
    pub namespace: u16,
    pub node_ids: Vec<String>,
}

#[derive(Serialize, Deserialize, Clone, JsonSchema)]
#[serde(deny_unknown_fields)]
struct OpcuaWriteConfig {
    pub uri: String,
    pub namespace: u16,
}

#[derive(Serialize, Deserialize, Clone, JsonSchema)]
#[serde(deny_unknown_fields)]
struct OpcuaSubscribeConfig {
    pub uri: String,
    pub namespace: u16,
    pub node_ids: Vec<String>,
    #[serde(default = "default_interval_ms")]
    pub interval_ms: u64,
}

fn default_interval_ms() -> u64 {
    1000
}

// ── Read node ──

fn register_opcua_read(registry: &mut DiagramElementRegistry, ensure: EnsureOpcua) {
    registry
        .register_node_builder(
            NodeBuilderOptions::new("opcua_read")
                .with_default_display_text("OPC UA Read")
                .with_description("Read one or more node values from an OPC UA server.")
                .with_config_examples([ConfigExample::new(
                    "Read two variables",
                    OpcuaReadConfig {
                        uri: "opc.tcp://192.168.1.100:4840".into(),
                        namespace: 2,
                        node_ids: vec!["Temperature".into(), "Pressure".into()],
                    },
                )]),
            move |builder, config: OpcuaReadConfig| {
                opcua_read_node(builder, config, ensure.clone())
            },
        )
        .with_result();
}

fn opcua_read_node(
    builder: &mut Builder,
    config: OpcuaReadConfig,
    ensure: EnsureOpcua,
) -> Node<JsonMessage, Result<JsonMessage, String>> {
    builder.commands().queue(ensure);

    let callback =
        move |Async { request: _, .. }: Async<JsonMessage>,
              opcua: bevy_ecs::prelude::Res<OpcuaHandle>,
              rt: bevy_ecs::prelude::Res<crate::executor::TokioHandle>| {
            let opcua = opcua.clone();
            let rt = rt.0.clone();
            let config = config.clone();
            async move {
                rt.spawn(async move {
                    let session = opcua.session(&config.uri).await?;

                    let nodes_to_read: Vec<ReadValueId> = config
                        .node_ids
                        .iter()
                        .map(|id| ReadValueId::from(NodeId::new(config.namespace, id.as_str())))
                        .collect();

                    let results = session
                        .read(&nodes_to_read, TimestampsToReturn::Neither, 0.0)
                        .await
                        .map_err(|e| format!("opcua_read: {e}"))?;

                    let values: serde_json::Map<String, serde_json::Value> = config
                        .node_ids
                        .iter()
                        .zip(results.iter())
                        .map(|(id, dv)| (id.clone(), data_value_to_json(dv)))
                        .collect();

                    Ok(JsonMessage::Object(
                        values
                            .into_iter()
                            .map(|(k, v)| (k, JsonMessage::from(v)))
                            .collect(),
                    ))
                })
                .abort_on_drop()
                .await
                .map_err(|e| e.to_string())?
            }
        };
    builder.create_node(callback.into_callback())
}

// ── Write node ──

fn register_opcua_write(registry: &mut DiagramElementRegistry, ensure: EnsureOpcua) {
    registry
        .register_node_builder(
            NodeBuilderOptions::new("opcua_write")
                .with_default_display_text("OPC UA Write")
                .with_description(
                    "Write values to OPC UA server nodes. Input must be an object mapping node IDs to values, e.g. {\"Temperature\": 25.0}.",
                )
                .with_config_examples([ConfigExample::new(
                    "Write to namespace 2",
                    OpcuaWriteConfig {
                        uri: "opc.tcp://192.168.1.100:4840".into(),
                        namespace: 2,
                    },
                )]),
            move |builder, config: OpcuaWriteConfig| {
                opcua_write_node(builder, config, ensure.clone())
            },
        )
        .with_result();
}

fn opcua_write_node(
    builder: &mut Builder,
    config: OpcuaWriteConfig,
    ensure: EnsureOpcua,
) -> Node<JsonMessage, Result<JsonMessage, String>> {
    builder.commands().queue(ensure);

    let callback =
        move |Async { request, .. }: Async<JsonMessage>,
              opcua: bevy_ecs::prelude::Res<OpcuaHandle>,
              rt: bevy_ecs::prelude::Res<crate::executor::TokioHandle>| {
            let opcua = opcua.clone();
            let rt = rt.0.clone();
            let config = config.clone();
            async move {
                rt.spawn(async move {
                    let session = opcua.session(&config.uri).await?;

                    let JsonMessage::Object(fields) = request else {
                        return Err(
                            "opcua_write: input must be an object mapping node IDs to values"
                                .into(),
                        );
                    };

                    let nodes_to_write: Vec<WriteValue> = fields
                        .into_iter()
                        .map(|(id, value)| {
                            let node_id = NodeId::new(config.namespace, id.as_str());
                            let variant = json_message_to_variant(&value);
                            WriteValue {
                                node_id,
                                attribute_id: 13, // Value attribute
                                index_range: Default::default(),
                                value: DataValue::new_now(variant),
                            }
                        })
                        .collect();

                    let results = session
                        .write(&nodes_to_write)
                        .await
                        .map_err(|e| format!("opcua_write: {e}"))?;

                    let any_bad = results.iter().any(|s| !s.is_good());
                    if any_bad {
                        let errs: Vec<String> = results
                            .iter()
                            .enumerate()
                            .filter(|(_, s)| !s.is_good())
                            .map(|(i, s)| format!("node {i}: {s}"))
                            .collect();
                        return Err(format!("opcua_write: {}", errs.join(", ")));
                    }

                    Ok(serde_json::json!({"written": results.len()}))
                })
                .abort_on_drop()
                .await
                .map_err(|e| e.to_string())?
            }
        };
    builder.create_node(callback.into_callback())
}

// ── Subscribe node ──

fn register_opcua_subscribe(registry: &mut DiagramElementRegistry, ensure: EnsureOpcua) {
    registry
        .register_node_builder(
            NodeBuilderOptions::new("opcua_subscribe")
                .with_default_display_text("OPC UA Subscribe")
                .with_description(
                    "Subscribe to data changes on OPC UA nodes. Streams each change as a JSON message through the 'message' stream output.",
                )
                .with_config_examples([ConfigExample::new(
                    "Subscribe to two variables",
                    OpcuaSubscribeConfig {
                        uri: "opc.tcp://192.168.1.100:4840".into(),
                        namespace: 2,
                        node_ids: vec!["Temperature".into(), "Pressure".into()],
                        interval_ms: 1000,
                    },
                )]),
            move |builder, config: OpcuaSubscribeConfig| {
                opcua_subscribe_node(builder, config, ensure.clone())
            },
        )
        .with_result();
}

fn opcua_subscribe_node(
    builder: &mut Builder,
    config: OpcuaSubscribeConfig,
    ensure: EnsureOpcua,
) -> Node<JsonMessage, Result<(), String>, MessageStream> {
    builder.commands().queue(ensure);

    let callback =
        move |Async { streams, .. }: Async<JsonMessage, MessageStream>,
              opcua: bevy_ecs::prelude::Res<OpcuaHandle>,
              rt: bevy_ecs::prelude::Res<crate::executor::TokioHandle>| {
            let opcua = opcua.clone();
            let rt = rt.0.clone();
            let config = config.clone();
            async move {
                let task = rt
                    .spawn(async move {
                        let session = opcua.session(&config.uri).await?;

                        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel::<JsonMessage>();

                        let subscription_id = session
                            .create_subscription(
                                Duration::from_millis(config.interval_ms),
                                10,
                                30,
                                0,
                                0,
                                true,
                                DataChangeCallback::new(
                                    move |dv: DataValue, item: &MonitoredItem| {
                                        let node_id = item.item_to_monitor().node_id.to_string();
                                        let value = data_value_to_json(&dv);
                                        let msg = serde_json::json!({
                                            "node_id": node_id,
                                            "value": value,
                                        });
                                        let _ = tx.send(msg);
                                    },
                                ),
                            )
                            .await
                            .map_err(|e| {
                                format!("opcua_subscribe: create subscription failed: {e}")
                            })?;

                        let items_to_create: Vec<MonitoredItemCreateRequest> = config
                            .node_ids
                            .iter()
                            .map(|id| NodeId::new(config.namespace, id.as_str()).into())
                            .collect();

                        session
                            .create_monitored_items(
                                subscription_id,
                                TimestampsToReturn::Both,
                                items_to_create,
                            )
                            .await
                            .map_err(|e| {
                                format!("opcua_subscribe: create monitored items failed: {e}")
                            })?;

                        while let Some(msg) = rx.recv().await {
                            streams.message.send(msg);
                        }

                        Ok(())
                    })
                    .abort_on_drop();

                task.await.map_err(|e| e.to_string())?
            }
        };
    builder.create_node(callback.into_callback())
}

// ── Helpers ──

fn data_value_to_json(dv: &DataValue) -> serde_json::Value {
    match &dv.value {
        Some(v) => variant_to_json(v),
        None => serde_json::Value::Null,
    }
}

fn variant_to_json(v: &Variant) -> serde_json::Value {
    match v {
        Variant::Boolean(b) => serde_json::Value::Bool(*b),
        Variant::SByte(n) => serde_json::json!(n),
        Variant::Byte(n) => serde_json::json!(n),
        Variant::Int16(n) => serde_json::json!(n),
        Variant::UInt16(n) => serde_json::json!(n),
        Variant::Int32(n) => serde_json::json!(n),
        Variant::UInt32(n) => serde_json::json!(n),
        Variant::Int64(n) => serde_json::json!(n),
        Variant::UInt64(n) => serde_json::json!(n),
        Variant::Float(n) => serde_json::json!(n),
        Variant::Double(n) => serde_json::json!(n),
        Variant::String(s) => serde_json::Value::String(s.to_string()),
        _ => serde_json::Value::String(format!("{v:?}")),
    }
}

fn json_message_to_variant(msg: &JsonMessage) -> Variant {
    match msg {
        JsonMessage::Bool(b) => Variant::Boolean(*b),
        JsonMessage::Number(n) => {
            if let Some(i) = n.as_i64() {
                Variant::Int64(i)
            } else if let Some(f) = n.as_f64() {
                Variant::Double(f)
            } else {
                Variant::String(n.to_string().into())
            }
        }
        JsonMessage::String(s) => Variant::String(s.clone().into()),
        other => Variant::String(other.to_string().into()),
    }
}
