//! 存储端口；实现必须在返回前确认写入已持久化（durable）。
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
    /// 在一次事务中追加一条不可变 revision 及其 outbox 事件。
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
    /// 该 adapter 外部只能见到不透明、已授权的对象引用。
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
