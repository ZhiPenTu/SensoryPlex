use serde::Deserialize;
use std::num::NonZeroUsize;
use tokio::sync::mpsc;

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Pipeline {
    pub api_version: String,
    pub kind: String,
    pub metadata: Metadata,
    pub spec: PipelineSpec,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Metadata {
    pub name: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PipelineSpec {
    pub source: Source,
    pub processors: Vec<Processor>,
    pub slow_enrichment: Vec<Processor>,
    pub sinks: Vec<Processor>,
    pub queue_capacity: NonZeroUsize,
    pub data_egress: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Source {
    pub r#type: String,
    pub uri_secret_ref: String,
}
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Processor {
    pub r#type: String,
    pub model: Option<String>,
}

impl Pipeline {
    pub fn parse(yaml: &str) -> Result<Self, String> {
        let p: Self =
            serde_yaml::from_str(yaml).map_err(|_| "invalid_pipeline_schema".to_string())?;
        if p.api_version != "edge.material/v1" || p.kind != "Pipeline" || p.metadata.name.is_empty()
        {
            return Err("unsupported_pipeline_contract".into());
        }
        if !["file", "srt"].contains(&p.spec.source.r#type.as_str())
            || p.spec.source.uri_secret_ref.is_empty()
        {
            return Err("invalid_source".into());
        }
        if p.spec.data_egress != "local_only" {
            return Err("data_policy_denied".into());
        }
        if p.spec.processors.is_empty()
            || p.spec.sinks.is_empty()
            || p.spec.queue_capacity.get() > 65536
        {
            return Err("invalid_pipeline_capacity_or_stages".into());
        }
        if p.spec
            .processors
            .iter()
            .chain(&p.spec.slow_enrichment)
            .chain(&p.spec.sinks)
            .any(|p| p.r#type.is_empty())
        {
            return Err("missing_processor_capability".into());
        }
        Ok(p)
    }
}

/// Admission never creates an unbounded queue or waits on a full slow path.
pub fn bounded_queue<T>(capacity: NonZeroUsize) -> (mpsc::Sender<T>, mpsc::Receiver<T>) {
    mpsc::channel(capacity.get())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn full_queue_reports_backpressure_without_losing_accepted_work() {
        let (tx, mut rx) = bounded_queue(NonZeroUsize::new(1).unwrap());
        tx.try_send(1).unwrap();
        assert!(matches!(
            tx.try_send(2),
            Err(mpsc::error::TrySendError::Full(2))
        ));
        assert_eq!(rx.recv().await, Some(1));
        tx.try_send(2).unwrap();
        drop(tx);
        assert_eq!(rx.recv().await, Some(2));
        assert_eq!(rx.recv().await, None);
    }
    #[test]
    fn pipeline_rejects_unbounded_queue_and_egress() {
        let example = include_str!("../../../config/pipelines/file-material.yaml");
        assert!(Pipeline::parse(example).is_ok());
        assert!(
            Pipeline::parse(&example.replace("queue_capacity: 32", "queue_capacity: 0")).is_err()
        );
        assert!(Pipeline::parse(&example.replace("local_only", "cloud")).is_err());
    }
}
