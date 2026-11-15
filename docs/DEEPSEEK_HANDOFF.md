# DevFlow AI 首次同步与接续说明

快照日期：2026-11-11。本文是首次同步的概览，不代替当前代码或 SPEC；继续工作前先查看项目根目录 `AGENTS.md` 和 `docs/PROGRESS.md` 顶部最新记录，再按本步定向查 `docs/SPEC.md`、代码及测试。`docs/HANDOFF.md`、`docs/MONTH_PLAN.md` 和 `docs/EVALUATION_BOUNDARY.md` 包含历史阶段描述，部分已过时，不能用旧句子覆盖最新进度。两边共享同一工作区时，不要重复应用用户提供的进度或覆盖已有改动。

最新协作状态：已停止使用 DSH；系统中已有 API Key，模型设为 `deepseek-flash`，模型列表接口验证成功。API 编码协作尚未接通；另一侧的 REPLAN 基线改动尚未验收。不得输出或复制 API Key。

## 背景与目标

DevFlow AI 是面向企业研发 Issue 的证据驱动软件工程 Agent 系统，是用户继 RAG 项目后的第二个实习项目。目标是将 Issue 形成可持久化 Task，通过多个角色探索、规划、修改、验证，依据执行证据进行失败分类、恢复或升级人工，最终形成可审查的补丁、轨迹和评测结果。用户也希望掌握设计与面试表达；当前已授权 AI 直接实现已理解的内容。长期记忆、Skills 和受控 RSI 是后续扩展，不代表当前已经完成。

这不是固定的一次性 Agent 流水线：Supervisor 在政策和预算约束下路由，角色之间通过有证据的 Handoff 交接；Harness 执行和记录工具操作，不替 Agent 做专业决策。Agent 声称完成、补丁已生成、公开测试通过和正式验收通过是不同状态。

## 协作和学习约定

- 按已授权的阶段目标批量推进，连续处理相互依赖的排查、编码、测试和普通修复，不为每个小步骤等待“继续”；只有必须由用户决定的重大问题、无法自行获得的资源，或需要学习的关键新知识才停下。每批完成后集中汇报，已掌握知识不重复考查。
- Codex 侧负责拆分、架构、关键代码审查、测试证据核验和教学；计划使用 DeepSeek API 辅助编码，但开发协作接口尚未接通。`docs/DSH_TASK_TEMPLATE.md` 仅保留历史任务模板，不使用其中 DSH 调用方式。
- 尽量节约用量：从最新记录和相关文件开始，不重复扫描全部历史；关键事务、并发、权限、工作区和验收必须审查实现及证据。后续尽量记录模型调用耗时、可取得的操作次数、Codex 测试时间和修复轮数，缺失数据记未知。
- 两边结束各自工作时给用户一段可复制的增量同步：本次目标、实现和修改文件、实际测试结果与跳过项、未完成/风险、下一步及 Git 状态。用户转发另一边的进度后，先看实际工作区和 `PROGRESS.md` 核对，再继续；不重复长篇背景。不要假定用户转发的总结已自动同步文件。

## 架构与角色

项目路径 `D:\python\PyCharm\Project\05_devflow-ai`；Windows / PowerShell。Python 3.11、FastAPI、SQLAlchemy、PostgreSQL、Alembic、LangGraph、pytest 和 uv，具体版本以 `pyproject.toml` / `uv.lock` 为准。Docker 用于 PostgreSQL 和独立评测相关测试。

- Supervisor：任务准入、流程路由、失败分类、停止条件和人工升级；不替代其他 Agent 的专业工作。
- Explorer：按需探索代码、读取事实、提出有依据的定位；不修改代码。
- Planner：根据 Issue 和探索证据制定计划、范围及验证方式；不直接修改代码。
- Coder：按批准计划和范围生成并应用补丁；不决定验收结果。
- Verifier：运行登记的公开检查和语义审查，基于证据形成验收结果；不修改补丁。

数据库记录 Task、Run、AgentExecution、Handoff、ToolCall、Artifact、Verification、FailureRecord 等；Task 是业务任务，Run 是一次执行尝试。Policy 保存角色权限、资源预算和停止条件。RunCoordinator、租约和工作区锁保障领取、执行权和提交边界；ContextBuilder 按阶段汇总证据。完整状态机、分类和边界以 `docs/SPEC.md` 为准。

## 当前代码结构

- `src/devflow_ai/api.py`、`schemas.py`、`services.py`、`repositories.py`：API、应用服务和持久化访问；`models.py`、`enums.py` 定义数据和状态。
- `admission*.py`：任务准入、预算、领取、派发和人工干预；`coordinator.py`、`heartbeat.py`、`workspace_lock.py`：Run 协调、执行权与隔离。
- `supervisor.py`、`explorer.py`、`planner.py`、`coder.py`、`verifier.py`、`worker.py`、`orchestration.py`、`context.py`：各角色契约及单次编排。
- `harness.py`、`artifacts.py`、`baseline.py`、`verification_config.py`、`independent_evaluation.py`：受控工具、证据、公开基线和独立评测；`policy.py`、`recovery_policy.py`：策略和恢复判断。
- `model_gateway.py`、`deepseek_adapter.py`：模型请求与适配；`migrations/versions/` 是 Alembic 迁移；`tests/` 是单元和集成测试；`benchmarks/click_3019_public/` 为公开任务材料。
- `docs/SPEC.md` 是设计依据，`docs/PROGRESS.md` 是按日期记录的实现与验证事实，`docs/DECISIONS.md` 记录决策。当前根目录没有 README.md；不要假设有完整交付文档。

## 已实现及验证过的主要能力

1. Task 创建、查询、幂等、准入记录与基础数据库服务；Run 领取、租约续期、执行权检查、持久化、预算和部分人工干预。相关测试使用隔离 PostgreSQL schema；单次 CLI 不代表持续后台 Worker 已完成。
2. 五角色的提示词、结构化输出和主要 Worker 接收器；有限输出修复、工具权限、路径和命令控制、工作区、补丁及工具/Artifact 证据；部分图路由已通过模拟模型、真实 Git/pytest 和隔离数据库测试。
3. 公开基线绑定可信调用方的 `VerificationConfig`，补丁后可运行公开检查与语义审查；Supervisor 可以从 Coder 后路由至 Verifier、重新规划或阻塞。可信配置由调用方提供，不等于完整任务包已被外部权威认证。
4. 可信控制端持有的私有用例可经 Docker 中 stdin/stdout 适配器做独立评测；相同用例先对缺陷基准和已知正确版本校准，再绑定草稿、补丁和结果。正式通过事务在证据完整时写入 Verification、Run 与 Task 状态。候选每次会收到单条输入；现有容器措施不证明彻底防逃逸或没有任何信息泄露，材料和参考版本来源仍依赖可信调用方。
5. 首次 `IMPLEMENTATION_FAILURE` 的证据复核、正式失败记录和一条重试路径已经实现；UNKNOWN 重新诊断有持久启动标记、额度预留与部分恢复链路。标记在首次节点派发前提交，即使随后崩溃且节点尚未真正运行也计数；不是模型已执行的证明。
6. 曾对 Click #3019 做过一次真实 deepseek-flash 尝试：公开基线 2 失败/2 通过，补丁后公开 4 项通过，语义审查与校准后的 8 个独立用例通过，隔离 schema 中得到 Verification passed、Run PASSED、Task AWAITING_REVIEW。schema 后来按测试夹具清理，证据导出在项目外；这不是常驻数据库中的任务、不是已合并的修复，也不证明整体成功率。参考 Click 8.3.1 包由可信控制端提供；私有用例和参考内容不得转发。

## 当前最准确接续点

最近一小步：UNKNOWN 恢复中 Coder 受阻 → Supervisor REPLAN → Planner → Supervisor 批准 CODE 的交接，已可在**同一新计划已有匹配可信基线**时派发一次 Coder，保存结果后停止；重复调用与无效基线/已有未核验工作区改动保守处理，不清理。最新定向复测 64 passed、2 skipped；Worker/Coder 回归 133 passed、4 skipped。测试使用模拟模型、真实 Git/pytest、隔离 PostgreSQL，所需新计划基线由夹具预先构造，初始 UNKNOWN 也由夹具构造。

**最新变化：另一侧已修改重新规划后的 BASELINE 派发，但该改动尚未由 Codex 验收。** 不要把测试夹具创建的基线或未经验证的修改当成真实端到端已接通。下一步先核对实际代码、运行专项并审查基线前后 Supervisor 身份、重放和工作区完整性；若原工作区已有补丁导致不能安全重新建立基线，应保持阻塞并明确，不得自动清理或放宽检查。用户可能另外转发更新，以最新用户指令和实际代码为准。

## 尚未完成或有限制

- REPLAN 后新基线、再次 Coder 后 Supervisor/Verifier 及受预算约束的完整自动循环尚未整体接通；分类之外的多种失败恢复、重复失败的新策略仍有限。
- 跨租约接管、节点运行中崩溃后的恢复及持续后台调度尚不完整。不能将单次方法和合成流程称作持续自治 Worker。
- 初始 UNKNOWN 真正分类到恢复、再到正式验收尚无完整真实模型端到端证明；测试替身、一次真实任务和全量成功率要严格区分。
- Benchmark 多任务/重复试验、人工审核及可交付运行说明仍需完善；长期记忆、Skills、受控 RSI 等按 SPEC 阶段门槛推进，当前不能声称完成。
- 项目是学习型开发过程，用户关心有效推进，不希望对低风险细节反复提问或过度设计防御；但已要求 Coder 无法修改的独立隐藏验收，不能退化成只看公开测试。

## 环境、Git 与测试边界

最近本地提交为 `d0ccbdb`（Save DevFlow implementation progress）；此后仍有大量修改和新文件尚未提交，包括协作文档、恢复代码、测试及两条新迁移。切勿 reset、清理、覆盖或替用户提交；后续先看 `git status`。不得自行升级/降级现有数据库；数据库集成测试须显式加载 `.env.test`、确认测试库并使用测试夹具 UUID 隔离 schema。Docker 专项需按测试标记显式启用；区分通过和跳过。测试与旧全量回归必须标明代码时点和范围，不能把历史结果当成本轮测试。

不要索要、展示或上传 `.env`、`.env.test`、账户凭据、隐藏用例、参考答案及原始敏感日志；不把它们粘贴到 DeepSeek。必要时只传本文件、用户授权的代码和经过净化的测试摘要。不要自行执行数据库迁移、提交 Git、删除工作区或修改系统权限。

## 发给 DeepSeek 的接续提示

请作为 DevFlow AI 的技术协作者和导师，从本文件接手。先读取 `AGENTS.md` 和 `docs/PROGRESS.md` 最新记录，核对实际 Git 状态，再按已授权的阶段目标连续完成相关排查、实现、测试和普通修复；遇到必须由用户决定的重大问题、无法自行取得的资源或需要学习的关键新知识再停下。保持现有权限、证据和独立隐藏验收边界，不清理既有更改、不自动提交或迁移已有数据库。每批结束给出简短的修改、实际测试/跳过及限制，方便用户转发给 Codex。
