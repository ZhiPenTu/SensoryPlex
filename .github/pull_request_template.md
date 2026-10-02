## 变更概述 (Summary)
<!-- 简要描述本次 PR 解决的问题或引入的新特性 -->

### 关联 Issue / ADR
<!-- 例如：Fixes #123, Relates to ADR-029 -->

---

## 变更类型 (Change Type)
- [ ] 🚀 新特性 (New Feature / Plugin)
- [ ] 🐛 缺陷修复 (Bug Fix)
- [ ] 📚 文档修订 (Documentation)
- [ ] ♻️ 架构重构 (Refactoring)
- [ ] 🧪 测试用例 (Tests)
- [ ] ⚡ 性能优化 (Performance)

---

## 核心工程红线自检清单 (Engineering Red Lines Checklist)
*请在提交前逐项核对并勾选，所有项均须严格遵守：*

- [ ] **契约规范**：未手工修改任何生成代码；涉及 Proto 契约改动时已运行 `make proto` 并在 PR 中提交生成产物。
- [ ] **架构职责与环境**：控制面代码通过容器环境验证；端侧插件未反向依赖 `services` 内部模块。
- [ ] **真实证据原则**：媒体/多模态改动使用了真实媒体样本进行端到端验证；没有用被跳过的测试或健康检查冒充测试证据。
- [ ] **诚实性与零静默降级**：未编写任何合成/伪造业务数据或静默 fallback 逻辑；缺失与未知状态已显式说明原因码。
- [ ] **数据面与凭据安全**：控制消息、日志、状态行及事件中绝无原始音视频帧、PCM 音频、Tensor 矩阵、密钥或宿主绝对私有路径。
- [ ] **数据库不可变性**：未修改历史迁移文件，所有 Schema 变更均为递增追加式脚本。
- [ ] **双语注释规范**：手写代码注释默认中文；Proto 契约与生成代码保持英文。

---

## 本地验证证据 (Verification Evidence)
*请在下方粘贴覆盖本次改动范围的实际终端运行命令与通过结果：*

```sh
# 例如：
# make lint-ruff
# make test-contracts
# make test-integration
# make orchestration-p1-check
```
