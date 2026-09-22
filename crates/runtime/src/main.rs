use sensoryplex_runtime::{capability, Pipeline};
use sensoryplex_sdk::runtime::{
    runtime_service_server::{RuntimeService, RuntimeServiceServer},
    DescribeCapabilitiesRequest, DescribeCapabilitiesResponse, HealthRequest, HealthResponse,
};
use tonic::{Request, Response, Status};

struct Runtime;
#[tonic::async_trait]
impl RuntimeService for Runtime {
    async fn health(&self, _: Request<HealthRequest>) -> Result<Response<HealthResponse>, Status> {
        Ok(Response::new(HealthResponse {
            state: capability::state().into(),
            unavailable_capabilities: capability::unavailable_capabilities(),
        }))
    }

    async fn describe_capabilities(
        &self,
        _: Request<DescribeCapabilitiesRequest>,
    ) -> Result<Response<DescribeCapabilitiesResponse>, Status> {
        Ok(Response::new(capability::describe()))
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
    tracing::info!(
        %address,
        platform = %capability::platform(),
        state = capability::state(),
        "runtime control endpoint started"
    );
    tonic::transport::Server::builder()
        .add_service(RuntimeServiceServer::new(Runtime))
        .serve_with_shutdown(address, async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
