//! Storage ports; implementations must confirm durable writes before returning.
use sensoryplex_sdk::{common::EventEnvelope, material::MaterialUnit};

#[derive(Debug, thiserror::Error)]
pub enum StorageError {
    #[error("revision_conflict")]
    RevisionConflict,
    #[error("dependency_unavailable")]
    Unavailable,
    #[error("invalid_contract")]
    InvalidContract,
}

pub trait MetadataStore: Send + Sync {
    /// Append an immutable revision and its outbox event in one transaction.
    fn append_revision(
        &self,
        material: &MaterialUnit,
        event: &EventEnvelope,
    ) -> impl std::future::Future<Output = Result<(), StorageError>> + Send;
    fn get_revision(
        &self,
        id: &str,
        revision: Option<u32>,
    ) -> impl std::future::Future<Output = Result<Option<MaterialUnit>, StorageError>> + Send;
}

pub trait BlobStore: Send + Sync {
    /// Only opaque, authorized object references leave this adapter.
    fn verify(
        &self,
        object_ref: &str,
        content_hash: &str,
    ) -> impl std::future::Future<Output = Result<bool, StorageError>> + Send;
}

pub trait VectorStore: Send + Sync {
    fn upsert(
        &self,
        embedding_id: &str,
        material_id: &str,
        revision: u32,
        model_release_id: &str,
        vector: &[f32],
    ) -> impl std::future::Future<Output = Result<(), StorageError>> + Send;
}
