use crossflow::diagram::{Diagram, DiagramOperation, SectionProvider, SectionTemplate};

use serde::Deserialize;
use std::{collections::HashMap, path::PathBuf};

use thiserror::Error as ThisError;

#[derive(Debug, ThisError)]
pub enum TemplateError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("Template file missing ")]
    BadPath { path: PathBuf },
    #[error("Failed to parse template '{name}': {reason}")]
    Parse { name: String, reason: String },
    #[error("Failed to find template in registry. TemplateSource may be invalid")]
    NotFound { name: String },
}

#[derive(Deserialize)]
pub struct DiagramFile {
    #[serde(default)]
    templates: HashMap<String, SectionTemplate>,
}

pub struct TemplateFolderSource {
    dir_path: PathBuf,
}

pub struct LoadedTemplates {
    pub templates: HashMap<String, SectionTemplate>,
    pub raw_json: HashMap<String, serde_json::Value>,
}

pub trait TemplateSource: Send + Sync {
    fn load(&self) -> LoadedTemplates;

    fn name(&self) -> String;
}

impl TemplateFolderSource {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            dir_path: path.into(),
        }
    }
}

impl TemplateSource for TemplateFolderSource {
    fn load(&self) -> LoadedTemplates {
        let mut templates: HashMap<String, SectionTemplate> = HashMap::new();
        let mut raw_json: HashMap<String, serde_json::Value> = HashMap::new();

        let entries = match std::fs::read_dir(&self.dir_path) {
            Ok(entries) => entries,
            Err(e) => {
                tracing::warn!(
                    "Failed to read templates directory {}: {e}",
                    self.dir_path.display()
                );
                return LoadedTemplates {
                    templates,
                    raw_json,
                };
            }
        };
        for file in entries {
            let file_path = match file {
                Ok(dir) => dir.path(),
                Err(e) => {
                    tracing::warn!("Failed to read directory entry: {e}");
                    continue;
                }
            };
            // Only read files with .json extension
            if file_path
                .extension()
                .and_then(|extension| extension.to_str())
                != Some("json")
            {
                continue;
            }

            let content = match std::fs::read_to_string(&file_path) {
                Ok(con) => con,
                Err(e) => {
                    tracing::warn!("Failed to read template file {}: {e}", file_path.display());
                    continue;
                }
            };
            let diagram: DiagramFile = match serde_json::from_str(&content) {
                Ok(diagram) => diagram,
                Err(e) => {
                    tracing::warn!("Failed to parse template file {}: {e}", file_path.display());
                    continue;
                }
            };
            let raw: serde_json::Value = match serde_json::from_str(&content) {
                Ok(v) => v,
                Err(_) => continue,
            };
            let raw_templates = raw
                .get("templates")
                .and_then(|v| v.as_object())
                .cloned()
                .unwrap_or_default();
            for (name, template) in diagram.templates {
                if templates.contains_key(&name) {
                    tracing::warn!(
                        "Duplicate template '{name}' in {}, skipping",
                        file_path.display()
                    );
                    continue;
                }
                if let Some(raw_tmpl) = raw_templates.get(&name) {
                    raw_json.insert(name.clone(), raw_tmpl.clone());
                }
                templates.insert(name, template);
            }
        }
        LoadedTemplates {
            templates,
            raw_json,
        }
    }

    fn name(&self) -> String {
        let path_name = format!("{}", self.dir_path.display());
        path_name
    }
}

#[derive(Default)]
pub struct TemplateRegistry {
    templates: HashMap<String, SectionTemplate>,
    raw_json: HashMap<String, serde_json::Value>,
}

impl TemplateRegistry {
    pub fn add_source(&mut self, source: &dyn TemplateSource) {
        let loaded = source.load();
        for (name, template) in loaded.templates {
            if self.templates.contains_key(&name) {
                tracing::warn!(
                    "Duplicate template '{name}' from source '{}' in template registry, skipping...",
                    source.name()
                );
                continue;
            }
            if let Some(raw) = loaded.raw_json.get(&name) {
                self.raw_json.insert(name.clone(), raw.clone());
            }
            self.templates.insert(name, template);
        }
    }

    pub fn sub_section_op_sequences(&self, template_name: &str) -> HashMap<String, u32> {
        let mut seqs = HashMap::new();
        let Some(raw) = self.raw_json.get(template_name) else {
            return seqs;
        };
        let Some(ops) = raw.get("ops").and_then(|v| v.as_object()) else {
            return seqs;
        };
        for (op_name, op) in ops {
            if op.get("type").and_then(|v| v.as_str()) != Some("section") {
                continue;
            }
            if let Some(seq) = op.get("op_sequence").and_then(|v| v.as_u64()) {
                seqs.insert(op_name.clone(), seq as u32);
            }
        }
        seqs
    }

    pub fn inject(&self, diagram: &mut Diagram) -> Result<(), TemplateError> {
        let mut temp: Vec<String> = Vec::new();

        for op in diagram.ops.values() {
            if let DiagramOperation::Section(section) = op.as_ref()
                && let SectionProvider::Template(name) = &section.provider
            {
                temp.push(name.to_string());
            }
        }

        while let Some(name) = temp.pop() {
            if diagram.templates.contains_key(name.as_str()) {
                continue;
            }
            match self.templates.get(&name) {
                Some(template) => {
                    for op in template.ops.values() {
                        if let DiagramOperation::Section(section) = op.as_ref()
                            && let SectionProvider::Template(nested) = &section.provider
                        {
                            temp.push(nested.to_string());
                        }
                    }
                    diagram.templates.insert(name.into(), template.clone());
                }
                None => {
                    return Err(TemplateError::NotFound { name });
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{assert_eq, fs};
    use tempfile::tempdir;

    fn test_json() -> &'static str {
        r#"{
            "version": "0.1.0",
            "templates": {
                "pickup": {
                    "inputs": ["entry"],
                    "outputs": ["done"],
                    "ops": {
                        "entry": {
                            "type": "node",
                            "builder": "DefaultNode",
                            "next": "done"
                        }
                    }
                }
            },
            "start": { "builtin": "dispose" },
            "ops": {}
        }"#
    }

    #[test]
    fn load_from_folder() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("pickup.json"), test_json()).unwrap();
        fs::write(dir.path().join("README.md"), "template").unwrap();

        let source = TemplateFolderSource::new(dir.path());
        let templates = source.load();

        assert!(templates.templates.contains_key("pickup"));
        assert_eq!(templates.templates.len(), 1);

        let dropoff = r#"
        {
            "version": "0.1.0",
            "templates": {
                "dropoff": {
                    "inputs": ["entry"],
                    "outputs": ["done"],
                    "ops": {
                        "entry": {
                            "type": "node",
                            "builder": "DefaultNode",
                            "next": "done"
                        }
                    }
                }
            },
            "start": { "builtin": "dispose" },
            "ops": {}
        }
        "#;
        fs::write(dir.path().join("dropoff.json"), dropoff).unwrap();
        let source = TemplateFolderSource::new(dir.path());
        let templates = source.load();

        assert_eq!(templates.templates.len(), 2);
    }

    #[test]
    fn duplicate_template() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("a.json"), test_json()).unwrap();
        fs::write(dir.path().join("b.json"), test_json()).unwrap();

        let source = TemplateFolderSource::new(dir.path());
        let templates = source.load();

        assert_eq!(templates.templates.len(), 1);
    }

    #[test]
    fn empty_folder() {
        let dir = tempdir().unwrap();
        let source = TemplateFolderSource::new(dir.path());
        let templates = source.load();
        assert!(templates.templates.is_empty());
    }

    #[test]
    fn add_source() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("pickup.json"), test_json()).unwrap();
        let source = TemplateFolderSource::new(dir.path());
        let mut registry = TemplateRegistry::default();
        registry.add_source(&source);

        assert!(registry.templates.contains_key("pickup"));
    }

    #[test]
    fn inject_basic() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("pickup.json"), test_json()).unwrap();
        let source = TemplateFolderSource::new(dir.path());
        let mut registry = TemplateRegistry::default();
        registry.add_source(&source);
        let mut diagram = Diagram::from_json_str(
            r#"{
            "version": "0.1.0",
            "start": "do_pickup",
            "ops": {
                "do_pickup": {
                    "type": "section",
                    "template": "pickup",
                    "connect": {
                        "done": { "builtin": "terminate" }
                    }
                }
            }
        }"#,
        )
        .unwrap();

        assert!(diagram.templates.is_empty());
        registry.inject(&mut diagram).unwrap();
        assert!(diagram.templates.contains_key("pickup"));
        assert_eq!(diagram.templates.len(), 1);
    }

    #[test]
    fn inject_nested() {
        let nested_json = r#"{
            "version": "0.1.0",
            "templates": {
                "outer": {
                    "inputs": ["entry"],
                    "outputs": ["done"],
                    "ops": {
                        "entry": {
                            "type": "section",
                            "template": "inner",
                            "connect": {
                                "done": "done"
                            }
                        }
                    }
                },
                "inner": {
                    "inputs": ["entry"],
                    "outputs": ["done"],
                    "ops": {
                        "entry": {
                            "type": "node",
                            "builder": "DefaultNode",
                            "next": "done"
                        }
                    }
                }
            },
            "start": { "builtin": "dispose" },
            "ops": {}
        }"#;

        let dir = tempdir().unwrap();
        fs::write(dir.path().join("nested.json"), nested_json).unwrap();
        let source = TemplateFolderSource::new(dir.path());
        let mut registry = TemplateRegistry::default();
        registry.add_source(&source);

        let mut diagram = Diagram::from_json_str(
            r#"{
            "version": "0.1.0",
            "start": "use_outer",
            "ops": {
                "use_outer": {
                    "type": "section",
                    "template": "outer",
                    "connect": {
                        "done": { "builtin": "terminate" }
                    }
                }
            }
        }"#,
        )
        .unwrap();

        assert!(diagram.templates.is_empty());
        registry.inject(&mut diagram).unwrap();
        assert!(diagram.templates.contains_key("outer"));
        assert!(diagram.templates.contains_key("inner"));
        assert_eq!(diagram.templates.len(), 2);
    }
}
