//! Pipeline DAG 编译与单次 Run 的确定性状态机。
//!
//! 本模块是 ADR-029 的 P0：它只验证图与推进内存状态，绝不直接启动插件、写数据库或发布消息。
//! 跨进程命令、持久化与真实 worker 调用必须建立在本模块的状态/依赖规则之上，不能各自重写一套。

use std::collections::{BTreeMap, BTreeSet};

use serde::Deserialize;

/// 图与单次运行的硬上限。P0 不允许用一个畸形配置创建无界的编译或状态表。
const MAX_GRAPH_NODES: usize = 128;
const MAX_GRAPH_EDGES: usize = 1_024;
const MAX_ATTEMPTS: u32 = 16;

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrchestrationGraph {
    pub nodes: Vec<OrchestrationNode>,
    pub edges: Vec<OrchestrationEdge>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrchestrationNode {
    pub id: String,
    pub plugin_id: String,
    pub consumes: Vec<String>,
    pub produces: Vec<String>,
    pub placement: Placement,
    pub deadline_ms: u32,
    pub max_attempts: u32,
    #[serde(default)]
    pub priority: u32,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum Placement {
    DataPlaneLocal,
    ObjectRefAllowed,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrchestrationEdge {
    pub from: String,
    pub to: String,
    pub modality: String,
    pub join: JoinPolicy,
    #[serde(default = "required_by_default")]
    pub required: bool,
}

fn required_by_default() -> bool {
    true
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum JoinPolicy {
    SameItem,
    SameStreamWindow,
    WindowContains,
}

/// 已通过结构、契约与拓扑排序校验的不可变图。
#[derive(Clone, Debug)]
pub struct CompiledGraph {
    nodes: BTreeMap<String, OrchestrationNode>,
    edges: Vec<OrchestrationEdge>,
    topological_order: Vec<String>,
}

impl OrchestrationGraph {
    /// 编译只接受能力匹配、无环且有上限的图；不修复、不猜测、更不根据名字连边。
    pub fn compile(&self) -> Result<CompiledGraph, String> {
        if self.nodes.is_empty() || self.nodes.len() > MAX_GRAPH_NODES {
            return Err("invalid_orchestration_node_count".into());
        }
        if self.edges.len() > MAX_GRAPH_EDGES {
            return Err("invalid_orchestration_edge_count".into());
        }

        let mut nodes = BTreeMap::new();
        for node in &self.nodes {
            validate_node(node)?;
            if nodes.insert(node.id.clone(), node.clone()).is_some() {
                return Err("duplicate_orchestration_node_id".into());
            }
        }

        let mut seen_edges = BTreeSet::new();
        let mut in_degree: BTreeMap<String, usize> =
            nodes.keys().map(|node| (node.clone(), 0)).collect();
        let mut outgoing: BTreeMap<String, Vec<String>> = BTreeMap::new();
        for edge in &self.edges {
            let from = nodes
                .get(&edge.from)
                .ok_or_else(|| "orchestration_edge_from_not_found".to_string())?;
            let to = nodes
                .get(&edge.to)
                .ok_or_else(|| "orchestration_edge_to_not_found".to_string())?;
            if edge.from == edge.to || edge.modality.is_empty() {
                return Err("invalid_orchestration_edge".into());
            }
            if !from.produces.iter().any(|item| item == &edge.modality) {
                return Err("orchestration_edge_modality_not_produced".into());
            }
            if !to.consumes.iter().any(|item| item == &edge.modality) {
                return Err("orchestration_edge_modality_not_consumed".into());
            }
            if edge.join == JoinPolicy::SameItem
                && (from.placement != Placement::DataPlaneLocal
                    || to.placement != Placement::DataPlaneLocal)
            {
                return Err("same_item_requires_data_plane_local".into());
            }
            let identity = format!(
                "{}\u{1f}{}\u{1f}{}\u{1f}{:?}",
                edge.from, edge.to, edge.modality, edge.join
            );
            if !seen_edges.insert(identity) {
                return Err("duplicate_orchestration_edge".into());
            }
            *in_degree
                .get_mut(&edge.to)
                .expect("edge endpoint was checked above") += 1;
            outgoing
                .entry(edge.from.clone())
                .or_default()
                .push(edge.to.clone());
        }

        let mut ready: BTreeSet<String> = in_degree
            .iter()
            .filter(|(_, degree)| **degree == 0)
            .map(|(node, _)| node.clone())
            .collect();
        let mut topological_order = Vec::with_capacity(nodes.len());
        while let Some(node) = ready.pop_first() {
            topological_order.push(node.clone());
            for child in outgoing.get(&node).into_iter().flatten() {
                let degree = in_degree
                    .get_mut(child)
                    .expect("outgoing target was checked above");
                *degree -= 1;
                if *degree == 0 {
                    ready.insert(child.clone());
                }
            }
        }
        if topological_order.len() != nodes.len() {
            return Err("orchestration_cycle_detected".into());
        }
        Ok(CompiledGraph {
            nodes,
            edges: self.edges.clone(),
            topological_order,
        })
    }
}

fn validate_node(node: &OrchestrationNode) -> Result<(), String> {
    if node.id.is_empty() || node.plugin_id.is_empty() || node.deadline_ms == 0 {
        return Err("invalid_orchestration_node".into());
    }
    if node.max_attempts == 0 || node.max_attempts > MAX_ATTEMPTS {
        return Err("invalid_orchestration_attempt_budget".into());
    }
    if node.produces.iter().any(String::is_empty) || node.consumes.iter().any(String::is_empty) {
        return Err("invalid_orchestration_modality".into());
    }
    if duplicate(&node.produces) || duplicate(&node.consumes) {
        return Err("duplicate_orchestration_modality".into());
    }
    Ok(())
}

fn duplicate(items: &[String]) -> bool {
    let mut values = BTreeSet::new();
    items.iter().any(|item| !values.insert(item))
}

impl CompiledGraph {
    pub fn node_count(&self) -> usize {
        self.nodes.len()
    }

    pub fn edge_count(&self) -> usize {
        self.edges.len()
    }

    pub fn topological_order(&self) -> &[String] {
        &self.topological_order
    }

    /// 创建本次 Run 的内存视图。实际持久化层必须用相同的状态与转换规则。
    pub fn begin_run(&self) -> RunState {
        let mut tasks = self
            .nodes
            .keys()
            .map(|id| (id.clone(), TaskRecord::new()))
            .collect();
        refresh_ready(&self.nodes, &self.edges, &mut tasks);
        RunState { tasks }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TaskState {
    Pending,
    Ready,
    Running,
    RetryWait,
    Succeeded,
    Failed,
    Cancelled,
    Blocked,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TaskRecord {
    pub state: TaskState,
    pub attempts: u32,
    pub reason_code: Option<String>,
}

impl TaskRecord {
    fn new() -> Self {
        Self {
            state: TaskState::Pending,
            attempts: 0,
            reason_code: None,
        }
    }
}

#[derive(Clone, Debug)]
pub struct RunState {
    tasks: BTreeMap<String, TaskRecord>,
}

impl RunState {
    pub fn task(&self, node_id: &str) -> Option<&TaskRecord> {
        self.tasks.get(node_id)
    }

    pub fn ready_tasks(&self) -> Vec<&str> {
        self.tasks
            .iter()
            .filter_map(|(id, task)| (task.state == TaskState::Ready).then_some(id.as_str()))
            .collect()
    }

    pub fn start(&mut self, graph: &CompiledGraph, node_id: &str) -> Result<(), String> {
        let task = self
            .tasks
            .get_mut(node_id)
            .ok_or_else(|| "orchestration_task_not_found".to_string())?;
        if task.state != TaskState::Ready {
            return Err("orchestration_task_not_ready".into());
        }
        task.state = TaskState::Running;
        task.attempts += 1;
        if task.attempts > graph.nodes[node_id].max_attempts {
            return Err("orchestration_attempt_budget_exhausted".into());
        }
        Ok(())
    }

    pub fn succeed(&mut self, graph: &CompiledGraph, node_id: &str) -> Result<(), String> {
        transition_running(&mut self.tasks, node_id, TaskState::Succeeded, None)?;
        refresh_ready(&graph.nodes, &graph.edges, &mut self.tasks);
        Ok(())
    }

    pub fn fail(
        &mut self,
        graph: &CompiledGraph,
        node_id: &str,
        retryable: bool,
        reason_code: &str,
    ) -> Result<(), String> {
        let task = self
            .tasks
            .get_mut(node_id)
            .ok_or_else(|| "orchestration_task_not_found".to_string())?;
        if task.state != TaskState::Running || reason_code.is_empty() {
            return Err("invalid_orchestration_task_failure".into());
        }
        task.reason_code = Some(reason_code.to_string());
        task.state = if retryable && task.attempts < graph.nodes[node_id].max_attempts {
            TaskState::RetryWait
        } else {
            TaskState::Failed
        };
        refresh_ready(&graph.nodes, &graph.edges, &mut self.tasks);
        Ok(())
    }

    pub fn release_retry(&mut self, graph: &CompiledGraph, node_id: &str) -> Result<(), String> {
        let task = self
            .tasks
            .get_mut(node_id)
            .ok_or_else(|| "orchestration_task_not_found".to_string())?;
        if task.state != TaskState::RetryWait {
            return Err("orchestration_task_not_retry_wait".into());
        }
        task.state = TaskState::Pending;
        refresh_ready(&graph.nodes, &graph.edges, &mut self.tasks);
        Ok(())
    }

    pub fn cancel(&mut self) {
        for task in self.tasks.values_mut() {
            if !matches!(
                task.state,
                TaskState::Succeeded | TaskState::Failed | TaskState::Blocked
            ) {
                task.state = TaskState::Cancelled;
                task.reason_code = Some("pipeline_run_cancelled".into());
            }
        }
    }
}

fn transition_running(
    tasks: &mut BTreeMap<String, TaskRecord>,
    node_id: &str,
    state: TaskState,
    reason_code: Option<String>,
) -> Result<(), String> {
    let task = tasks
        .get_mut(node_id)
        .ok_or_else(|| "orchestration_task_not_found".to_string())?;
    if task.state != TaskState::Running {
        return Err("orchestration_task_not_running".into());
    }
    task.state = state;
    task.reason_code = reason_code;
    Ok(())
}

fn refresh_ready(
    nodes: &BTreeMap<String, OrchestrationNode>,
    edges: &[OrchestrationEdge],
    tasks: &mut BTreeMap<String, TaskRecord>,
) {
    let mut incoming: BTreeMap<&str, Vec<&OrchestrationEdge>> = BTreeMap::new();
    for edge in edges {
        incoming.entry(&edge.to).or_default().push(edge);
    }
    for node_id in nodes.keys() {
        let task = &tasks[node_id];
        if task.state != TaskState::Pending {
            continue;
        }
        let edges = incoming
            .get(node_id.as_str())
            .map(Vec::as_slice)
            .unwrap_or(&[]);
        let required = edges
            .iter()
            .filter(|edge| edge.required)
            .collect::<Vec<_>>();
        let blocked = required.iter().any(|edge| {
            matches!(
                tasks[&edge.from].state,
                TaskState::Failed | TaskState::Cancelled | TaskState::Blocked
            )
        });
        let all_succeeded = required
            .iter()
            .all(|edge| tasks[&edge.from].state == TaskState::Succeeded);
        let record = tasks.get_mut(node_id).expect("node task exists");
        if blocked {
            record.state = TaskState::Blocked;
            record.reason_code = Some("required_upstream_not_succeeded".into());
        } else if all_succeeded {
            record.state = TaskState::Ready;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn graph() -> CompiledGraph {
        OrchestrationGraph {
            nodes: vec![
                OrchestrationNode {
                    id: "source".into(),
                    plugin_id: "runtime.file-source".into(),
                    consumes: vec![],
                    produces: vec!["media.video_frame".into()],
                    placement: Placement::DataPlaneLocal,
                    deadline_ms: 1_000,
                    max_attempts: 1,
                    priority: 0,
                },
                OrchestrationNode {
                    id: "ocr".into(),
                    plugin_id: "org.sensoryplex.ocr-rapidocr".into(),
                    consumes: vec!["media.video_frame".into()],
                    produces: vec!["observation.ocr_blocks".into()],
                    placement: Placement::DataPlaneLocal,
                    deadline_ms: 5_000,
                    max_attempts: 2,
                    priority: 0,
                },
                OrchestrationNode {
                    id: "embedding".into(),
                    plugin_id: "org.sensoryplex.embed-bge-onnx".into(),
                    consumes: vec!["observation.ocr_blocks".into()],
                    produces: vec!["observation.text_embedding".into()],
                    placement: Placement::ObjectRefAllowed,
                    deadline_ms: 5_000,
                    max_attempts: 2,
                    priority: 0,
                },
            ],
            edges: vec![
                OrchestrationEdge {
                    from: "source".into(),
                    to: "ocr".into(),
                    modality: "media.video_frame".into(),
                    join: JoinPolicy::SameItem,
                    required: true,
                },
                OrchestrationEdge {
                    from: "ocr".into(),
                    to: "embedding".into(),
                    modality: "observation.ocr_blocks".into(),
                    join: JoinPolicy::SameStreamWindow,
                    required: true,
                },
            ],
        }
        .compile()
        .expect("valid graph")
    }

    #[test]
    fn compiler_is_deterministic_and_rejects_cycles() {
        let valid = graph();
        assert_eq!(valid.node_count(), 3);
        assert_eq!(valid.edge_count(), 2);
        assert_eq!(valid.topological_order(), ["source", "ocr", "embedding"]);

        let invalid = OrchestrationGraph {
            nodes: vec![
                OrchestrationNode {
                    id: "a".into(),
                    plugin_id: "a".into(),
                    consumes: vec!["x".into()],
                    produces: vec!["y".into()],
                    placement: Placement::DataPlaneLocal,
                    deadline_ms: 1,
                    max_attempts: 1,
                    priority: 0,
                },
                OrchestrationNode {
                    id: "b".into(),
                    plugin_id: "b".into(),
                    consumes: vec!["y".into()],
                    produces: vec!["x".into()],
                    placement: Placement::DataPlaneLocal,
                    deadline_ms: 1,
                    max_attempts: 1,
                    priority: 0,
                },
            ],
            edges: vec![
                OrchestrationEdge {
                    from: "a".into(),
                    to: "b".into(),
                    modality: "y".into(),
                    join: JoinPolicy::SameItem,
                    required: true,
                },
                OrchestrationEdge {
                    from: "b".into(),
                    to: "a".into(),
                    modality: "x".into(),
                    join: JoinPolicy::SameItem,
                    required: true,
                },
            ],
        };
        assert_eq!(
            invalid.compile().unwrap_err(),
            "orchestration_cycle_detected"
        );

        let cross_node_buffer = OrchestrationGraph {
            nodes: vec![
                OrchestrationNode {
                    id: "source".into(),
                    plugin_id: "source".into(),
                    consumes: vec![],
                    produces: vec!["media.video_frame".into()],
                    placement: Placement::ObjectRefAllowed,
                    deadline_ms: 1,
                    max_attempts: 1,
                    priority: 0,
                },
                OrchestrationNode {
                    id: "processor".into(),
                    plugin_id: "processor".into(),
                    consumes: vec!["media.video_frame".into()],
                    produces: vec![],
                    placement: Placement::DataPlaneLocal,
                    deadline_ms: 1,
                    max_attempts: 1,
                    priority: 0,
                },
            ],
            edges: vec![OrchestrationEdge {
                from: "source".into(),
                to: "processor".into(),
                modality: "media.video_frame".into(),
                join: JoinPolicy::SameItem,
                required: true,
            }],
        };
        assert_eq!(
            cross_node_buffer.compile().unwrap_err(),
            "same_item_requires_data_plane_local"
        );
    }

    #[test]
    fn run_unlocks_only_after_required_predecessors_and_retries_boundedly() {
        let graph = graph();
        let mut run = graph.begin_run();
        assert_eq!(run.ready_tasks(), ["source"]);
        run.start(&graph, "source").unwrap();
        run.succeed(&graph, "source").unwrap();
        assert_eq!(run.ready_tasks(), ["ocr"]);
        run.start(&graph, "ocr").unwrap();
        run.fail(&graph, "ocr", true, "transient_backend_failure")
            .unwrap();
        assert_eq!(run.task("ocr").unwrap().state, TaskState::RetryWait);
        assert!(run.ready_tasks().is_empty());
        run.release_retry(&graph, "ocr").unwrap();
        run.start(&graph, "ocr").unwrap();
        run.succeed(&graph, "ocr").unwrap();
        assert_eq!(run.ready_tasks(), ["embedding"]);
    }

    #[test]
    fn required_failure_blocks_descendants_and_cancel_never_unlocks_more_work() {
        let graph = graph();
        let mut run = graph.begin_run();
        run.start(&graph, "source").unwrap();
        run.succeed(&graph, "source").unwrap();
        run.start(&graph, "ocr").unwrap();
        run.fail(&graph, "ocr", false, "invalid_input").unwrap();
        assert_eq!(run.task("embedding").unwrap().state, TaskState::Blocked);

        let mut cancellation = graph.begin_run();
        cancellation.cancel();
        assert_eq!(
            cancellation.task("source").unwrap().state,
            TaskState::Cancelled
        );
        assert!(cancellation.ready_tasks().is_empty());
    }
}
