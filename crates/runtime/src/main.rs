use sensoryplex_runtime::Pipeline;
use sensoryplex_sdk::runtime::{
    runtime_service_server::{RuntimeService, RuntimeServiceServer},
    HealthRequest, HealthResponse,
};
use tonic::{Request, Response, Status};

struct Runtime;
#[tonic::async_trait]
impl RuntimeService for Runtime {
    async fn health(&self, _: Request<HealthRequest>) -> Result<Response<HealthResponse>, Status> {
        Ok(Response::new(HealthResponse {
            state: "degraded".into(),
            unavailable_capabilities: vec![
                "media_ingestion".into(),
                "model_inference".into(),
                "event_dispatch".into(),
                "semantic_index".into(),
            ],
        }))
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args.first().map(String::as_str) == Some("check") && args.len() == 2 {
        Pipeline::parse(&std::fs::read_to_string(&args[1])?).map_err(std::io::Error::other)?;
        println!("pipeline schema valid; capability availability must be checked before execution");
        return Ok(());
    }
    if !args.is_empty() && args != ["serve"] {
        return Err("usage: sensoryplex-runtime [serve | check <pipeline.yaml>]".into());
    }
    let address = std::env::var("SENSORYPLEX_RUNTIME_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:50051".into())
        .parse()?;
    tracing::info!(%address, "runtime control endpoint started");
    tonic::transport::Server::builder()
        .add_service(RuntimeServiceServer::new(Runtime))
        .serve_with_shutdown(address, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
