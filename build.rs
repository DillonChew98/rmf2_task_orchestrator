use std::io;
use std::path::PathBuf;

fn main() -> io::Result<()> {
    println!("cargo:rerun-if-changed=assets/protos");

    let mut protos: Vec<PathBuf> = Vec::new();
    let mut pending = vec![PathBuf::from("assets/protos")];
    while let Some(dir) = pending.pop() {
        let entries = std::fs::read_dir(&dir)
            .unwrap_or_else(|e| panic!("Failed to read {}: {e}", dir.display()));
        for entry in entries {
            let entry = entry.unwrap_or_else(|e| panic!("Failed to read {}: {e}", dir.display()));
            let path = entry.path();
            if path.is_dir() {
                pending.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("proto") {
                protos.push(path);
            }
        }
    }

    let file_descriptor_path = PathBuf::from(std::env::var("OUT_DIR").expect("OUT_DIR not set"))
        .join("file_descriptor_set.bin");

    tonic_prost_build::configure()
        .build_client(false)
        .build_server(false)
        .file_descriptor_set_path(file_descriptor_path)
        .compile_protos(&protos, &[PathBuf::from("assets/protos")])
        .unwrap_or_else(|e| panic!("Failed to compile protos: {e}"));

    Ok(())
}
