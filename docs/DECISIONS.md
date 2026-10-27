# DevFlow AI 决策记录

## 2026-10-04：从设计讨论进入本地实现

依据：用户提供的《DevFlowAI_Codex_学习与开发交接提示词》与本地 `SPEC.md` 对照核查。本记录写入已明确的路线选择；实现与验证状态单独记在 `PROGRESS.md`。

1. **V1 接口**：FastAPI 提前提供薄的任务提交、查询和必要审查接口；CLI 保留 worker 启动、调试与 benchmark 入口。两个入口复用应用服务。长任务由独立 worker 执行，先单 worker，随后验证多 worker 一致性。初版不引入 Redis 或 Celery。
2. **环境顺序**：先用 Docker Compose 启动 PostgreSQL；核心流程可运行后再容器化 API 和 worker。数据库容器及应用容器均不充当工具执行沙箱。
3. **质量与观测**：从第一个代码模块开始加入必要测试和基础结构化日志；后续扩展日志查询、指标与 CI。
4. **受控执行**：真实 Agent 读写文件和执行命令前，完成最小 Harness；真实 Agent 接入时同步接入最小 ContextBuilder 和资源限制。
5. **编排落地**：LangGraph 可以先用固定输出节点验证路由和持久化，再逐步替换为真实 Agent；替身执行不算真实修复能力。
6. **真实任务与评测**：第一项 Click 任务提前作为贯穿验证。#3019 是首个待核验候选，#2971 保留候选；先有一项真实闭环，再扩展六任务评测。任务包、基准和验收均需真实证据。
7. **暂缓范围**：前端、Redis、Celery、Langfuse、MCP、动态沙箱容器、Kubernetes、复杂云部署、训练与微调。有阻塞证据时再重新评估。
8. **首个数据库镜像**：阶段 1 的 Compose 练习使用官方 `postgres:17.11` 标签，便于固定 PostgreSQL 小版本；已于 2026-10-05 实际下载、启动并通过健康、SQL 与数据卷持久化检查。镜像标签来源：[Docker 官方镜像标签](https://hub.docker.com/_/postgres/tags?name=17.)。

Policy 阈值、事务及租约设计已合入 `SPEC.md` 第 6、7 节。初始阈值尚未经过真实任务校准。

## 2026-10-05：最小后端入口的运行版本

项目使用本机已有的 uv 管理 Python 3.11.15，并以 `.python-version` 和 `pyproject.toml` 限定 3.11 系列。当前全局 `python` 命令指向 3.9.1，因此项目命令统一通过 `uv run` 执行。先锁定 FastAPI、Uvicorn 与开发测试依赖；SQLAlchemy、Alembic 等在对应练习阶段加入。首个学生实现练习是仅表示进程存活的 `GET /health/live`。

## 2026-10-25：采纳能力扩展与受控 RSI 路线

依据：用户提供的《DevFlowAI_Frontier_Integration_Roadmap_20261025.md》与当前代码、依赖、迁移及 SPEC 对照。用户授权按差异更新文档；本次不实现新增组件，不执行迁移，不调整依赖。下列是采纳的设计方向，不代表学生逐项理解通过、代码完成或评测通过。正式规格见 `SPEC.md` §9.1、§10，运行证据见 `PROGRESS.md`。

1. **保留架构主线**：仍为五个 Agent、自研 Harness/持久化/Context/Evidence、LangGraph `InMemorySaver` 和数据库重建状态。继续使用 Python 3.11、同步 SQLAlchemy/Psycopg 和现有依赖锁定，不借升级改框架。Docker PostgreSQL 不等于执行沙箱；V1 保留 `git clone`、命令白名单和可信受控环境约束。
2. **先闭环，再增强**：工作包 A 至 E 对应迁移与 Git 基线、业务持久化、受控执行、真实修复、恢复与独立评测。在 E 验收通过且满足 SPEC §10.8 的 H 启动门槛后，可先做 H 的首个 ContextBuilder 实验，再完善 F 的长期记忆和 G 的记忆评测/Skills。RSI 不以完整记忆系统完成为前提，也不能早于可靠执行与独立评测。
3. **增强而非替换角色**：MemoryService、EvaluationRunner、EvolutionRunner 是工程组件，不是新增 LLM Agent。Supervisor 决策，RunCoordinator 校验并持久化生命周期，Harness 执行并审计获准操作；Verifier 验单次补丁，EvaluationRunner 比较系统版本。任何新组件不得接管或绕过这些边界。
4. **V1 增量范围**：把结构化输出及有限解析修复、基础动态上下文、证据缺口驱动搜索、关联日志、成本与独立评测融入现有阶段，不等到 V2 才做最小上下文控制。具体修复限额需到对应教学步骤推导，全部调用计入预算。
5. **V2 经验治理**：长期记忆是从正式记录提取的、有来源和适用条件的可修订经验；不能替代 Task/Run/Evidence 真相。保留版本、时间、冲突和失效信息，召回先做范围与权限过滤。摄入和索引失败不得回滚已提交的业务结果。Skills 仅封装已验证的重复操作，仍受 Harness 和 Policy 管理。
6. **受控 RSI**：首轮只优化 ContextBuilder 的日志片段和证据选择。固定基线、可编辑范围、评测任务、正确性/回归门槛及实验总预算，在独立工作区与进程运行候选，实验数据隔离或显式分区。候选不得修改隐藏测试、评分/晋升控制、安全权限、生命周期不变式或放宽硬预算。单独工作区不是安全沙箱；改进 Task 仍需现有人工审核。
7. **执行版本固定**：`policy_snapshot` 不能单独固定代码。RSI 使用前需要不可变执行版本引用，定位代码、配置、模型、提示词、上下文策略及已启用的记忆/Skill 快照；同一 Task 的恢复 Run 继续使用固定版本，通过评测的新版本只供后续新 Task 选择。具体字段与加载机制待对应步骤设计，当前 Task 模型尚未具备。
8. **评测与学习隔离**：区分开发任务、独立保留任务和连续学习协议；不把隐藏测试、标准答案、保留集诊断或同题成功解答写入记忆后声称独立测试。保存失败候选、谱系、接受/拒绝理由、回退点和累计成本，不预设改进或成功率。
9. **不提前扩表或堆栈**：保留 V1 九张业务表设计；MemoryItem/MemoryRevision/MemoryUse、CandidateVersion/EvolutionExperiment/EvaluationResult 仅为候选概念。工程组件的 Artifact 归属、版本字段、索引和实验门槛在对应阶段细化，不伪造 AgentExecution，也不在本轮创建空模块或迁移。
10. **条件性后置**：MCP、A2A、Code Mode、动态 Tool Search、并行探索和图检索需实际需求与对照证据；多模态 RAG 优先在独立 RAG 项目研究。训练/微调、本地推理优化和通用 Computer Use 不进入当前闭环。

**当前教学不变**：接续 `41ff63fe9d8a_create_tasks_table.py` 的 `downgrade()` 枚举清理。学生已理解“先删表，再删类型”，下一步由学生补代码并检查；之后逐步在独立测试数据库验证迁移，建立 Git 基线，完成 Task 创建和查询。上述采纳不授权 AI 包办核心代码。
