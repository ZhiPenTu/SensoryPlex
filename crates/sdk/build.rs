fn main() -> Result<(), Box<dyn std::error::Error>> {
    std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    let root = "../../proto";
    let files = [
        "common/v1/common.proto",
        "material/v1/material.proto",
        "media/v1/media.proto",
        "media/v1/handoff.proto",
        "media/v1/live.proto",
        "runtime/v1/runtime.proto",
        "gateway/v1/gateway.proto",
    ];
    let paths: Vec<_> = files.iter().map(|file| format!("{root}/{file}")).collect();
    for path in &paths {
        println!("cargo:rerun-if-changed={path}");
    }
    tonic_build::configure()
        .boxed(".edge.material.runtime.v1.PluginInput.value.buffer")
        .boxed(".edge.material.runtime.v1.PluginInput.value.observation")
        .compile_protos(&paths, &[root])?;
    Ok(())
}
