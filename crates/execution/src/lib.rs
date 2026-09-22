//! 硬件抽象。Adapter 必须显式上报回退（fallback）情况，不得隐式假定。
use sensoryplex_sdk::common::{BufferDescriptor, ProcessingError, RequestContext};

#[derive(Debug, Clone)]
pub struct Capability {
    pub backend: String,
    pub runtime_version: String,
    pub supported_precisions: Vec<String>,
    pub memory_kinds: Vec<String>,
    pub max_concurrency: u32,
}

pub trait ExecutionBackend: Send + Sync {
    fn capability(&self) -> Capability;
    fn load(
        &mut self,
        model_ref: &str,
        config_hash: &str,
    ) -> impl std::future::Future<Output = Result<(), ProcessingError>> + Send;
    fn compile(
        &mut self,
        profile: &str,
    ) -> impl std::future::Future<Output = Result<(), ProcessingError>> + Send;
    fn infer(
        &self,
        context: &RequestContext,
        inputs: &[BufferDescriptor],
    ) -> impl std::future::Future<Output = Result<Vec<BufferDescriptor>, ProcessingError>> + Send;
    fn unload(&mut self) -> impl std::future::Future<Output = Result<(), ProcessingError>> + Send;
}
