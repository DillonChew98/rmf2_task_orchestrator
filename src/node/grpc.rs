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

use crate::config::GrpcSettings;
use crossflow::DiagramElementRegistry;
use crossflow::diagram::grpc::GrpcTlsSettings;
use prost_reflect::DescriptorPool;
use std::path::Path;
use std::sync::Arc;
use tokio::runtime::Runtime;

pub(crate) fn register(
    registry: &mut DiagramElementRegistry,
    runtime: Arc<Runtime>,
    settings: &GrpcSettings,
) {
    let descriptor_set_bytes = include_bytes!(concat!(env!("OUT_DIR"), "/file_descriptor_set.bin"));
    DescriptorPool::decode_global_file_descriptor_set(&descriptor_set_bytes[..])
        .expect("failed to load proto descriptors from assets/protos");

    registry.enable_grpc(runtime, tls_settings(settings));
}

fn tls_settings(settings: &GrpcSettings) -> Option<GrpcTlsSettings> {
    let Some(ca_cert) = &settings.ca_cert else {
        if settings.client_cert.is_some()
            || settings.client_key.is_some()
            || settings.domain_name.is_some()
        {
            panic!("set [grpc] ca_cert to use client_cert, client_key or domain_name");
        }
        return None;
    };
    let identity_pem = match (&settings.client_cert, &settings.client_key) {
        (Some(cert), Some(key)) => Some((read_pem(cert), read_pem(key))),
        (None, None) => None,
        _ => panic!("set both [grpc] client_cert and client_key, or neither"),
    };
    Some(GrpcTlsSettings {
        ca_cert_pem: read_pem(ca_cert),
        domain_name: settings.domain_name.clone(),
        identity_pem,
    })
}

fn read_pem(path: &Path) -> Vec<u8> {
    std::fs::read(path)
        .unwrap_or_else(|e| panic!("failed to read gRPC cert {}: {e}", path.display()))
}
