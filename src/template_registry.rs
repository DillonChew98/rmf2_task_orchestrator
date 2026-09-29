use crossflow::{bevy_derive::Deref, bevy_derive::DerefMut, bevy_ecs};
use crossflow::diagram::{SectionTemplate, Diagram};
use std::{path::PathBuf, collections::HashMap};
use serde::Deserialize;

use thiserror::Error as ThisError;

#[derive(Debug, ThisError)]
pub enum TemplateError {
    #[error("IO error: {0}")]
    Io(#[from] std::io::Error),
    #[error("Template file missing ")]
    BadPath {path: PathBuf},
    #[error("Failed to parse template '{name}': {reason}")]
    Parse { name: String, reason: String },

    
}

#[derive(Deserialize)]
pub struct DiagramFile {
    #[serde(default)]
    templates: HashMap<String, SectionTemplate>
}

pub struct TemplateFolderSource {
    dir_path: PathBuf,
}

pub trait TemplateSource: Send + Sync {
    fn load(&self) -> Result<HashMap<String, SectionTemplate>, TemplateError>;

    fn name(&self) -> String;
}

impl TemplateFolderSource {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self {
            dir_path: path.into()
        }
    }
}

impl TemplateSource for TemplateFolderSource {
    fn load(&self) -> Result<HashMap<String, SectionTemplate>, TemplateError> {
        let mut templates: HashMap<String, SectionTemplate> = HashMap::new();

        for file in std::fs::read_dir(&self.dir_path)? {
            let file_path = file?.path();
            // Only read files with .json extension
            if file_path.extension().and_then(|extension| extension.to_str()) != Some("json") {
                continue;
            }

            let content = std::fs::read_to_string(&file_path)?;
            let diagram: DiagramFile = serde_json::from_str(&content).map_err(|e| TemplateError::Parse {
                name: file_path.display().to_string(),
                reason: e.to_string(),
            })?;
            for (name, template) in diagram.templates {
                if templates.contains_key(&name) {
                    tracing::warn!("Duplicate template '{name}' in {}, skipping", file_path.display());
                    continue;
                }
                templates.insert(name, template);
            }
        }
        Ok(templates)
    }

    fn name(&self) -> String {
        format!("{}", self.dir_path.display())
    }

}

#[derive(Default, bevy_ecs::prelude::Resource, Deref, DerefMut)]
pub struct TemplateRegistry {
    templates: HashMap<String, SectionTemplate>,
}

impl TemplateRegistry {
    pub fn add_source(&mut self, source: &dyn TemplateSource) -> Result<&mut Self, TemplateError> {
        for (name, template) in source.load()? {
            if self.templates.contains_key(&name) {
                tracing::warn!("Duplicate template '{name}' from source '{}' in template registry, skipping...", source.name());
                continue;
            }
            self.templates.insert(name, template);
        }
        Ok(self)
    }
    pub fn inject(&self, diagram: &mut Diagram) {
        for op in diagram.ops.fi{

        }
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
                            "builder": "GoToNode",
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
        let templates = source.load().unwrap();

        assert!(templates.contains_key("pickup"));
        assert_eq!(templates.len(), 1);

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
                            "builder": "GoToNode",
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
        let templates = source.load().unwrap();

        assert_eq!(templates.len(), 2);


    }

    #[test]
    fn duplicate_template() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("a.json"), test_json()).unwrap();
        fs::write(dir.path().join("b.json"), test_json()).unwrap();
        
        let source = TemplateFolderSource::new(dir.path());
        let templates = source.load().unwrap();

        assert_eq!(templates.len(), 1);
    }

    #[test]
    fn empty_folder() {
        let dir = tempdir().unwrap();
        let source = TemplateFolderSource::new(dir.path());
        let templates = source.load().unwrap();
        assert!(templates.is_empty());
    }

    #[test]
    fn add_source() {
        let dir = tempdir().unwrap();
        fs::write(dir.path().join("pickup.json"), test_json()).unwrap();
        let source = TemplateFolderSource::new(dir.path());
        let mut registry = TemplateRegistry::default();
        registry.add_source(&source).unwrap();

        assert!(registry.get("pickup").is_some());
    }


}