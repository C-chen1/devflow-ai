# DevFlow AI — 项目规格说明（SPEC）

> **一句话定位**：面向企业研发团队，把软件 Issue 变成可持久化的自主任务，通过 Multi-Agent 协作完成代码分析、规划、修改与验证，并基于真实环境反馈实现失败恢复、重新规划与人工升级。

2026-10-25：按用户提供的《DevFlowAI_Frontier_Integration_Roadmap_20261025.md》纳入能力扩展路线。第 9、10 节区分 V1 执行基础、V2 经验记忆与受控 RSI 实验线；新增内容属于已采纳的设计，不代表已实现、已测试或学生已经完成逐项理解验证。实际进度见 [PROGRESS.md](PROGRESS.md)。

---

## 目录

1. [Task 生命周期状态机](#1-task-生命周期状态机)
2. [数据模型](#2-数据模型)
3. [验收标准与证据](#3-验收标准与证据)
4. [失败分类与路由](#4-失败分类与路由)
5. [Agent 契约与决策权](#5-agent-契约与决策权)
6. [Policy 与资源控制](#6-policy-与资源控制)
7. [架构、事务与恢复](#7-架构事务与恢复)
8. [Benchmark 与任务包](#8-benchmark-与任务包)
9. [实施路线与阶段验收](#9-实施路线与阶段验收)
10. [能力扩展、经验记忆与受控 RSI](#10-能力扩展经验记忆与受控-rsi)

---

## 1. Task 生命周期状态机

### 1.1 两个层级的状态

Task 和 Run 各有一个独立的状态，回答不同的问题：

- **Task.status**：回答"整个任务走到人生哪个阶段了"（9 个状态）
- **Run.status**：回答"这一次尝试成没成"（3 个状态：`RUNNING` / `PASSED` / `FAILED`）

### 1.2 Task 的 9 个状态

记忆法：**9 = 2 + 2 + 1 + 4**（进门 / 干活 / 等人 / 结局）

| 幕 | 状态 | 含义 | 终态？ |
|---|---|---|---|
| 进门 | `PENDING` | 已创建，等准入判断 | 否 |
| 进门 | `ELIGIBLE` | 准入通过，等启动 | 否 |
| 干活 | `RUNNING` | 正在执行 | 否 |
| 干活 | `RETRYING` | 刚失败，等下一轮 | 否 |
| 等人 | `AWAITING_REVIEW` | 验证通过，等人类审核 | 否 |
| 结局 | `COMPLETED` | 成功（人类接受） | 是 |
| 结局 | `REJECTED` | 人类拒绝补丁 | 是 |
| 结局 | `ESCALATED` | AI 触顶，升级人工 | 是 |
| 结局 | `DECLINED` | 准入未通过，从未开始 | 是 |

### 1.3 转换表（11 条合法转换）

| 当前状态 | 事件 | 新状态 |
|---|---|---|
| `PENDING` | 准入通过 | `ELIGIBLE` |
| `PENDING` | 准入不通过 | `DECLINED` |
| `ELIGIBLE` | 启动 | `RUNNING` |
| `RUNNING` | 验证通过 | `AWAITING_REVIEW` |
| `RUNNING` | 本次失败且 Policy 允许自动恢复 | `RETRYING` |
| `RUNNING` | 本次失败且触发任一硬性停止条件 | `ESCALATED` |
| `RETRYING` | 新一轮开始 | `RUNNING` |
| `RETRYING` | 创建新 Run 前触发任一硬性停止条件 | `ESCALATED` |
| `AWAITING_REVIEW` | 人类接受 | `COMPLETED` |
| `AWAITING_REVIEW` | 人类拒绝 | `REJECTED` |
| `AWAITING_REVIEW` | 人类请求重试 | `RETRYING` |

### 1.4 关键设计原则

1. **自动恢复循环**：失败后的自动恢复在 `RUNNING ⇄ RETRYING` 之间进行；人工从 `AWAITING_REVIEW` 请求重试也会返回 `RETRYING`，但属于人工触发的返工路径。
2. **自动恢复必须有出口**：Task 重试预算耗尽、相同失败指纹触顶、没有合规恢复方案，或未知失败反复重诊断仍无法分类时，任一条件满足即进入 `ESCALATED`；具体规则见 §6。
3. **"失败"是机器的话，"拒绝"是人类的话**：失败发生在 `RUNNING`（测试挂了），拒绝发生在 `AWAITING_REVIEW`（人类不要这个补丁）。两者在不同阶段。
4. **人类审核是三种结果**：接受 / 拒绝 / 请求重试。

---

## 2. 数据模型

九张表，是 Harness 的记忆。每张表回答一个明确的问题："如果没有它，系统就记不住 ______。"

### 2.1 关系树

九张表不是平铺的九个文件柜，而是一棵树。`Task` 是根，层层下挂：

```
Task (根)
├── Run                       ← run.task_id
│   ├── AgentExecution         ← agent_execution.run_id
│   │   ├── ToolCall            ← tool_call.agent_execution_id
│   │   ├── Artifact            ← artifact.agent_execution_id
│   │   └── Handoff             ← handoff.from_agent_execution_id
│   ├── Verification           ← verification.run_id
│   └── FailureRecord          ← failure_record.run_id
└── HumanReview               ← human_review.task_id
```

### 2.2 九张表

**实体表（有 `status`，状态会变）：Task、Run、AgentExecution、ToolCall。**
**记录表（写一次就定格，无 `status`）：Artifact、Verification、FailureRecord、Handoff、HumanReview。**

| 表 | 一句话用途 |
|---|---|
| `Task` | 整个 Issue 变成的自主任务，记 9 状态 |
| `Run` | 一次执行尝试，记 3 状态 |
| `AgentExecution` | 某个 Agent 被调用了一次：谁、喂了什么、吐出什么 |
| `ToolCall` | Agent 调用了一次工具：哪个工具、参数、返回 |
| `Artifact` | Agent 的产物：计划/补丁/分析（内容存文件） |
| `Verification` | 一次验证的结果 + 证据 |
| `FailureRecord` | 失败类别/指纹 + 下次换什么策略 |
| `Handoff` | Agent 之间交接的上下文 |
| `HumanReview` | 人工拍板的结论 + 评语 |

#### Task（实体表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键；配置数据库默认值 `gen_random_uuid()`（SQLAlchemy `server_default=func.gen_random_uuid()`），插入时省略该列，由 PostgreSQL 生成随机 UUID v4 |
| `idempotency_key` | UUID | 创建请求的提交标识；目标模型非空，数据库唯一约束名 `uq_tasks_idempotency_key`，不为新任务设数据库默认值。迁移为旧任务补兼容 UUID；模型、迁移脚本、服务及 HTTP API 已实现；2026-10-31 测试库 devflow_test 已升级 a3d12f2f7ab9 并验收，开发库本次未升级 |
| `title` | TEXT | issue 标题；不允许 `NULL`，由数据库 `NOT NULL` 约束保证所有写入入口均遵守；该约束不拒绝空字符串 |
| `description` | TEXT | issue 内容；不允许 `NULL`，由数据库 `NOT NULL` 约束保证；该约束不拒绝空字符串，也不保证内容充分或任务可修复，准入仍由 Supervisor 判断 |
| `repo` | TEXT | 目标仓库；不允许 `NULL`，由数据库 `NOT NULL` 约束保证；该约束不拒绝空字符串，也不保证仓库地址有效、可访问或已获准处理 |
| `base_commit` | TEXT（可空） | 通过准入时固定的 Git commit ID，与 `ELIGIBLE` 在同一次条件更新中写入。数据库要求 `ELIGIBLE` 记录提供完整的小写十六进制 ID（40 或 64 字符）；`PENDING` 和准入拒绝可以留空 |
| `admission_record` | JSONB（可空） | 准入理由与实际依据；与准入状态同一事务保存。旧记录和待准入任务可为空，不补造历史事实。 |
| `admission_usage` | JSONB（非空） | 按请求 ID 保存准入模型调用预留与实际用量；Task 行锁下记录，旧任务初始为空。 |
| `admission_blocked` | JSONB（可空） | 最近一次成功记录的准入阻塞类别、理由及可选缺失信息；Task 仍为 PENDING。 |
| `admission_blocked_at` | TIMESTAMPTZ（可空） | 获取 Task 行锁后写入阻塞记录时的数据库时间；不是下次可重试时间。 |
| `admission_claim_token` / `admission_claim_owner` / `admission_claim_expires_at` | UUID / TEXT / TIMESTAMPTZ（可空） | 准入阶段的领取令牌、持有者与到期时间；三者同时有值或同时为空，独立于 Run 租约。 |
| `admission_attempt_count` | INTEGER（可空） | 成功提交领取的累计次数，包含成功、失败或崩溃的尝试；新 Task 从 0 开始，历史未知记录为 NULL，不伪造旧次数。 |
| `admission_intervention` | JSONB（可空） | 准入自动尝试达到上限时的人工处理提示，包含原因代码、时间和计数；Task 状态仍为 PENDING。 |
| `status` | 枚举（9 值） | 当前状态，不允许 `NULL`；枚举约束合法取值，状态转换合法性由应用服务中的生命周期控制检查，见 §1、§7 |
| `policy_version` | TEXT | 创建 Task 时固定的 Policy 版本；不允许 `NULL`，由数据库 `NOT NULL` 约束保证；该约束不拒绝空字符串，也不保证版本号与快照内容一致 |
| `policy_snapshot` | JSONB | 创建 Task 时固定的完整预算与权限配置；不允许 SQL `NULL`；SQLAlchemy 使用 `JSONB(none_as_null=True)` 将 Python `None` 按 SQL `NULL` 处理。类型转换与非空约束不保证快照结构和配置项完整 |
| `created_at` | TIMESTAMPTZ | 创建时间；不允许 `NULL`；配置 `server_default=func.now()`，插入时省略该列，由 PostgreSQL 提供当前事务时间 |
| `updated_at` | TIMESTAMPTZ | 最后更新时间；不允许 `NULL`；插入时以 `server_default=func.now()` 初始化，SQLAlchemy 构造更新语句且未显式赋该列时，以 `onupdate=func.now()` 刷新；原生 SQL 更新须显式维护该列 |

V1 约定 Task 的 `title`、`description`、`repo` 保存首次创建时的 Issue 内容，创建后不修改；不同内容作为新的 Task 创建。Task 的 `status` 等生命周期字段仍可变化。该约定使已存 Issue 字段可作为提交幂等的比较基准；当前没有 Issue 字段更新 API，也没有数据库级防更新约束。若未来允许编辑，须重新设计首次提交基准的保存方式。

Task 创建后的初始状态为 `PENDING`。`status` 列配置数据库默认值 `PENDING`（SQLAlchemy `server_default`），在插入时省略该列的情况下由 PostgreSQL 补值；默认值不覆盖显式提供的状态。Task 创建应用服务仍须固定初始状态为 `PENDING`，不接受客户端指定初始状态。

Task 的两个时间字段使用 PostgreSQL 带时区时间类型 `TIMESTAMPTZ`（SQLAlchemy `DateTime(timezone=True)`）。Python 侧使用带时区的 `datetime`，应用内部统一按 UTC 表示和比较，展示时按需转换为本地时间。

#### Run（实体表）

实现进度（2026-10-31）：Run(Base)/runs 的十二字段已全部实现：UUID 主键、Task 外键、尝试编号、RunStatus、创建标识、执行归属和六个时间字段。RunStatus 使用独立原生枚举 run_status；保留命名约束 uq_runs_task_attempt_number、uq_runs_task_creation_idempotency_key，以及 status = RUNNING 时对 task_id 唯一的 uq_runs_one_running_per_task。迁移候选 b312290a866d_create_runs_table.py 衔接 a3d12f2f7ab9，只新增 runs/run_status/相关约束与索引；降级先删索引和表，再删 run_status，不触及 Task。Run 集成夹具现执行真实迁移，不再直接使用 ORM create 创建测试表；十七项 Run 集成测试覆盖约束、字段行为和升级—降级—再升级，完整测试集 105 passed/1 warning。先在独立 schema 验证后，2026-10-31 实际 devflow_test.public 已升级至 b312290a866d；十二列、时间默认/可空、run_status、主外键及三项唯一保护均已核验，四条原 Task 的全部字段逐项不变，runs 记录为 0。alembic check 返回 No new upgrade operations detected；开发库本次未升级。RunCoordinator 的指定 Task 首次启动与创建请求重放已实现并隔离测试；自动扫描首次领取已实现；单次领取 Worker/CLI 已实现；实际 Agent 执行、心跳与失败恢复业务尚未实现，不能把持久化测试当执行闭环完成。

Run 租约与时间字段的已确认语义（2026-10-31，模型已实现）：worker_id 和 lease_expires_at 必须非空，否则无法确认执行归属与到期时间。last_heartbeat_at 初始采用 Worker 成功领取并创建 Run 的确认时刻，不是仅发出分派请求的时间；该时刻已经可确认活跃，后续有效心跳再刷新。创建 RUNNING Run 时 started_at 有启动值，finished_at 允许 NULL，只有本次尝试实际结束后才填写。created_at/updated_at 沿用 Task 已验证的插入默认和应用更新规则；lease_expires_at 必须由协调层根据当前时间与租约时长提供，不把“现在”作为到期默认值，本步不确定租约秒数。上述规则不代表心跳、续约、过期恢复已经实现。

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键，数据库 gen_random_uuid() 默认值 |
| `task_id` | UUID（FK） | 非空，引用 tasks.id，不设置级联删除 |
| `attempt_number` | INTEGER | 非空，无自动编号；与 task_id 组合唯一 |
| `status` | 枚举（3 值） | 非空，RUNNING / PASSED / FAILED，无列默认值 |
| `creation_idempotency_key` | TEXT | 非空无默认值，与 task_id 组合唯一；用于协调层定位原创建请求 |
| `worker_id` | TEXT | 非空、无默认值，保存执行归属；非空不等于已经验证调用者执行权 |
| `last_heartbeat_at` | TIMESTAMPTZ | 非空，插入时默认 now()，后续由有效心跳显式更新 |
| `lease_expires_at` | TIMESTAMPTZ | 非空无默认值，由协调层计算并提供执行权到期时间 |
| `started_at` | TIMESTAMPTZ | 非空，插入时默认 now()，记录 Run 启动时间 |
| `finished_at` | TIMESTAMPTZ | 允许 NULL，无默认值；进入终态时由生命周期服务显式填写 |
| `created_at` | TIMESTAMPTZ | 非空，插入时默认 now() |
| `updated_at` | TIMESTAMPTZ | 非空，插入时默认 now()；SQLAlchemy onupdate=func.now()，不是数据库更新触发器 |

#### AgentExecution（实体表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `run_id` | UUID（FK） | 挂在哪个 Run |
| `handoff_id` | UUID（FK，可空，唯一） | 此次执行接收的 Handoff。无交接的入口执行可为空 |
| `agent_role` | TEXT | 哪个角色（supervisor/explorer/planner/coder/verifier） |
| `status` | 枚举 | running / succeeded / failed |
| `input` | JSONB | 喂给它的上下文 |
| `output` | JSONB | 它交回的结果 |
| `llm_usage` | JSONB | 本次执行全部模型调用的 token 用量及来源；缺失时标明估算或未知 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 最后更新时间 |

#### ToolCall（实体表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `run_id` | UUID（FK，必填） | 调用所属的 Run |
| `agent_execution_id` | UUID（FK，可空） | 发起调用的 AgentExecution，Worker 初始化调用时留空 |
| `tool_name` | TEXT | 哪个工具（git_clone / read_file / run_command） |
| `args` | JSONB | 传的参数 |
| `status` | 枚举 | requested / running / succeeded / failed / denied / timed_out / unknown_result |
| `stdout_excerpt` / `stderr_excerpt` | TEXT | 输出片段；较大原始内容保存为 Artifact |
| `exit_code` | INTEGER（可空） | 命令明确结束时的退出码；未知结果不可伪造 |
| `duration_ms` | INTEGER（可空） | 可确认的执行耗时 |
| `reason` | TEXT（可空） | 拒绝、超时、未知结果等原因 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 最后更新时间 |

Agent 发起的调用通过联合外键 `(agent_execution_id, run_id)` 引用 AgentExecution 的 `(id, run_id)`，由数据库保证执行记录和调用属于同一个 Run。Worker 的初始化调用直接关联 Run。

#### Artifact（记录表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `agent_execution_id` | UUID（FK，可空） | 生成产物的 AgentExecution，Worker 工具产物可留空 |
| `tool_call_id` | UUID（FK，可空） | 产物对应的 ToolCall，Agent 直接生成产物时可留空 |
| `kind` | TEXT | analysis / plan / patch / tool_output（如读文件的完整内容）/ tool_stdout / tool_stderr（分别保存两路完整命令输出） |
| `content_ref` | TEXT | 文件路径（内容存文件，不存 DB） |
| `content_hash` | TEXT | 文件内容哈希，用于完整性核对和跨 Run 引用 |
| `created_at` | TIMESTAMP | 创建时间 |

一次 ToolCall 可以生成多份 Artifact。两个归属字段至少填写一个，每个已填写的 ID 都有独立外键。两者都填写时，`(tool_call_id, agent_execution_id)` 联合引用 ToolCall 的 `(id, agent_execution_id)`，保证两者属于同一次 Agent 执行。Worker 初始化产物通过 `tool_call_id` 追溯到 Run。

#### Verification（记录表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `run_id` | UUID（FK） | 挂在哪个 Run |
| `result` | 枚举 | passed / failed |
| `evidence` | JSONB | 测试输出、diff 等原始证明 |
| `created_at` | TIMESTAMP | 创建时间 |

#### FailureRecord（记录表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `run_id` | UUID（FK） | 挂在哪个 Run |
| `category` | 枚举（9 值） | 失败属于哪一类，见 §4 |
| `signature` | TEXT | 失败指纹（识别反复同一种失败） |
| `recovery_action` | TEXT | 下次换什么策略 |
| `created_at` | TIMESTAMP | 创建时间 |

#### Handoff（记录表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `from_agent_execution_id` | UUID（FK） | 谁交出的（已跑完，有 id 可指） |
| `to_agent_role` | TEXT | 交给哪个角色（还没跑，只有角色名） |
| `context` | JSONB | 交接的上下文（Context Engineering 的产物） |
| `created_at` | TIMESTAMP | 创建时间 |

#### HumanReview（记录表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `task_id` | UUID（FK） | 挂在哪个 Task |
| `decision` | 枚举 | accept / reject / request_retry |
| `comment` | TEXT | 人类评语 |
| `created_at` | TIMESTAMP | 创建时间 |

### 2.3 设计原则

1. **能算不存**：能从正式记录计算的值不单独存业务计数列。额外尝试数由 Run 数量计算；剩余次数由 Task 的 Policy 快照减去额外尝试数得到。
2. **儿子只指爸爸，不指爷爷**：外键只指向直接父表。例：`AgentExecution` 只存 `run_id`，不存 `task_id`（往上走一层就有了）。
3. **状态粗、记录细**：实体表只有粗粒度 `status`；细粒度的不可变历史，全在记录表里（查现在看 status，查历史看 Artifact/记录表）。
4. **结构相同 → 一张表 + 类型字段**：5 个 Agent 用一张 `AgentExecution` + `agent_role` 区分；3 种产物用一张 `Artifact` + `kind` 区分。
5. **大内容存文件、DB 存路径**：补丁/分析等内容存文件，表里只留 `content_ref` 指针，避免大 blob 拖慢查询和备份。

---

## 3. 验收标准与证据

Verifier 不能因为 Coder 声称“已经修复”就让 Run 通过。验收必须建立在可复查的真实证据上，并经过“确定性检查 + LLM 语义审查”两道门。只有两道门都通过，`Run.status` 才能变成 `PASSED`。

### 3.1 确定性检查

确定性检查负责执行可以由程序重复计算、明确判断的项目，不依赖 LLM 阅读自然语言后猜测结果。

1. **目标修复有效**：当前 Issue 对应的失败场景，在修改后必须得到预期行为。验收的是可观察行为，不是“代码能够运行”。
2. **没有引入回归**：修改前通过的测试，修改后仍须通过。修改前已经失败、且与当前 Issue 无关的测试，不归责于本次补丁；当前 Issue 对应的失败场景则必须由失败变为通过。
3. **测试仍然有效**：对比修改前后的测试清单与测试变更，不能通过删除、跳过或削弱目标测试来制造“全部通过”。测试内容发生变化时，交给语义门进一步审查其合理性。
4. **收集改动范围**：从 `git diff` 中提取修改文件名、文件数量、新增行数和删除行数。这些数据只描述改动规模，不单独决定验收结果。

确定性检查一旦明确失败，LLM 无权将其改判为通过。LLM 可以分析失败原因并提出后续建议，但本次 Run 不能标记为 `PASSED`。

Verifier 的门结果组合使用 `passed`、`failed` 和 `blocked` 表达判断。存在确定性失败时结果为 `failed`；缺少确定性证据或语义证据时为 `blocked`，等待补充。只有两道门都有充分通过证据才返回 `passed`。组合函数本身不认证工具记录，也不更新 Run。

### 3.2 LLM 语义审查

确定性测试全部通过，也不能直接证明补丁真正解决了问题。LLM 需要对照 Issue 描述，审查代码 diff、测试 diff/测试内容和确定性测试结果，判断：

1. 补丁是否真正修复了 Issue，而不是只让特定测试变绿；
2. 是否存在针对固定输入写死返回值、删除测试或削弱断言等迎合测试的行为；
3. 修改的文件和代码是否与当前 Issue 相关、是否确有必要；
4. 是否存在确定性测试未覆盖但从代码语义上可见的风险。

LLM 的结论必须附带可复查依据，包括具体文件路径，以及相关行号或代码片段和判断理由。LLM 可以在确定性检查通过后，基于明确依据判定语义审查失败；但不能覆盖确定性检查给出的失败结论。

### 3.3 Evidence 内容

`Verification.evidence` 至少保存以下四类证据：

| 证据类别 | 保存内容 |
|---|---|
| 目标修复证据 | 覆盖 Issue 复现场景和期望行为的测试、实际执行命令、原始输出和退出码 |
| 无回归证据 | 修改前后同一套测试命令的结果，包括每个测试的名称、通过/失败状态和原始输出，用于逐项比较 |
| 改动范围证据 | `git diff` 中的修改文件名、文件数量、新增行数和删除行数 |
| 语义审查证据 | LLM 审查时使用的 Issue、代码 diff、测试 diff 和确定性结果，以及结论所引用的文件位置、代码片段和判断理由 |

原始输出用于复查具体发生了什么；退出码用于 Harness 通过确定性规则判断命令整体成功或失败。只有通过/失败总数不够，因为修改前后失败数量可能相同，但失败的具体测试已经发生变化。

### 3.4 验收结果

| 确定性门 | 语义门 | 结果 |
|---|---|---|
| 通过 | 通过 | `Verification.result = passed`，`Run.status = PASSED`，Task 进入 `AWAITING_REVIEW` |
| 失败 | 不得改判 | `Verification.result = failed`，`Run.status = FAILED`，创建 `FailureRecord` |
| 通过 | 失败 | `Verification.result = failed`，`Run.status = FAILED`，创建 `FailureRecord` |

失败之后选择重试、重新规划还是升级人工，由第 4 节“失败分类与路由”决定。

---

## 4. 失败分类与路由

`Run.status = FAILED` 只表示“这一次尝试没有成功”，不能直接决定下一步动作。系统必须先根据真实证据判断需要修复的对象，再决定重跑同一步骤、重新探索、重新规划、修改补丁或升级人工。

### 4.1 设计目标与分类原则

1. **按需要修复的对象分类**：分类不以“哪个 Agent 发现失败”为依据，也不只记录“测试失败”之类的表面现象。例如 Verifier 的语义审查发现补丁硬编码，验证过程本身是成功的，仍应归为 `IMPLEMENTATION_FAILURE`。
2. **分类与动作分离**：`category` 回答“属于哪类失败”，`signature` 回答“是否又是同一个具体失败”，`recovery_action` 回答“这一次接下来做什么”。相同类别会因预算、安全限制和重复次数不同而采取不同动作。
3. **证据先于路由**：Supervisor 只能依据 `Verification`、`AgentExecution`、`ToolCall` 等记录中的退出码、原始输出、结构校验错误和语义审查依据进行诊断，不能凭 Agent 的自述猜测失败原因。
4. **不允许无信息增量的原样重试**：除已确认的临时错误外，每次恢复必须增加新证据、修正上游产物或改变策略。
5. **自动恢复必须有出口**：预算耗尽、相同失败触顶、没有合规恢复方案或持续无法分类时，停止自动循环并进入 `ESCALATED`。

### 4.2 FailureRecord 字段

`FailureRecord` 使用三个相互独立的字段描述一次失败决策：

| 字段 | 回答的问题 | 示例 |
|---|---|---|
| `category` | 这属于哪一类失败？ | `IMPLEMENTATION_FAILURE` |
| `signature` | 这个具体失败以前是否出现过？ | 由规范化后的测试名、错误类型和关键错误信息生成的指纹 |
| `recovery_action` | 这一次决定采取什么动作？ | `REVISE_IMPLEMENTATION` / `ESCALATE_TO_HUMAN` |

多个不同的失败指纹可以属于同一个类别；同一个类别也可能因为剩余预算不同而选择不同的恢复动作。因此不能把类别塞进 `signature`，也不能用 `category` 直接代替 `recovery_action`。

### 4.3 失败类别与正常恢复路线

下表描述预算充足、未触发硬性安全阻断时的正常恢复路线。实际动作仍须经过 §4.4 的优先级判断。

| 失败类别 | 判断边界 | 默认恢复动作 | 正常路线 |
|---|---|---|---|
| `TRANSIENT_EXECUTION_FAILURE` | 命令和环境配置正确，但因临时网络超时、外部服务短暂不可用或偶发进程中断而失败 | `RETRY_SAME_STEP` | 原 Agent 重新发起同一步骤，Harness 在限制次数内再次执行 |
| `TOOL_INVOCATION_FAILURE` | 工具名、命令、参数或工作目录错误，目标操作没有被有效执行 | `FIX_TOOL_INVOCATION` | 修正工具调用；涉及仓库特定用法时由 Explorer 查明正确方式 |
| `ENVIRONMENT_FAILURE` | 工具调用正确，但依赖、运行时、权限或环境配置不满足 | `PREPARE_ENVIRONMENT` | Explorer 确认仓库支持的准备方式，Harness 执行白名单命令，再由原步骤验证 |
| `EXPLORATION_FAILURE` | 代码定位、依赖关系或上下文分析错误或不足 | `RE_EXPLORE` | Supervisor 携带新证据路由给 Explorer；若定位变化使旧计划失效，再交给 Planner |
| `PLANNING_FAILURE` | 问题已经定位，但计划选择了错误文件、方案或步骤 | `REPLAN` | Supervisor 路由给 Planner 更新计划，再由 Coder 按新计划实现 |
| `IMPLEMENTATION_FAILURE` | 测试已经有效运行或语义证据充分，表明补丁行为错误、存在硬编码或未真正修复 Issue | `REVISE_IMPLEMENTATION` | Supervisor 将失败证据路由给 Coder 修正补丁 |
| `AGENT_CONTRACT_FAILURE` | Agent 输出缺少必填字段、结构无法解析或不符合约定契约，尚不能评价其专业内容 | `RETRY_AGENT_OUTPUT` | Harness 记录校验错误，Supervisor 路由回原 Agent 重新生成，再由 Harness 校验 |
| `POLICY_VIOLATION` | 请求的命令或操作被白名单、安全规则或权限边界拒绝 | `FIND_COMPLIANT_ALTERNATIVE` | Supervisor 优先路由出合规方案；可调整则重新规划或改用安全操作，不可绕过则升级人工 |
| `UNKNOWN_FAILURE` | 当前证据不足，无法可靠归入任何已知类别 | `REDIAGNOSE` | Supervisor 补充证据并重新诊断；仍无法分类或预算耗尽则升级人工 |

Harness 返回失败不等于 `Harness` 自身故障。只要 Harness 正确执行了请求并保存了退出码与原始输出，就应继续根据请求、环境或产物的真实问题分类。

### 4.4 Supervisor 路由优先级

Supervisor 不把所有判断排成固定流水线，而是先检查硬性停止条件，再按失败类别进入不同分支：

```text
收到失败证据
|
|- 预算耗尽 / 相同失败触顶 / 无合规恢复方案
|  `- recovery_action = ESCALATE_TO_HUMAN
|     Task.status = ESCALATED
|
`- 仍允许自动恢复
   |- category = UNKNOWN_FAILURE
   |  `- REDIAGNOSE：补充证据并重新分类
   |- category = TRANSIENT_EXECUTION_FAILURE
   |  `- RETRY_SAME_STEP：在限制次数内重试同一步骤
   `- 其他已知且可恢复类别
      `- 按 §4.3 路由给对应 Agent 或 Harness 执行恢复动作
```

`category` 描述失败本身，不因为预算耗尽而改变；预算等 Policy 条件只会改变本次 `recovery_action`。例如同样是 `IMPLEMENTATION_FAILURE`，有预算时选择 `REVISE_IMPLEMENTATION`，预算耗尽时选择 `ESCALATE_TO_HUMAN`。

### 4.5 Run 与 Task 的失败恢复流程

1. 当前尝试失败后，保留工具输出、Agent 输出或验证证据，将当前 `Run.status` 从 `RUNNING` 更新为 `FAILED`，并创建 `FailureRecord`。
2. Supervisor 依据证据写入 `category` 和 `signature`，按 §4.4 选择并写入 `recovery_action`。
3. 如果仍允许自动恢复，`Task.status` 从 `RUNNING` 进入 `RETRYING`。旧 Run 保持 `FAILED`，不得重新打开。
4. 新一轮开始时，创建 `attempt_number + 1`、状态为 `RUNNING` 的新 Run；Task 从 `RETRYING` 回到 `RUNNING`。系统从数据库重建 state，新图从 Supervisor 开始，再依据上一条 `recovery_action` 路由到所需角色。
5. 如果重试次数、预算或相同失败次数已经触顶，或者没有合规恢复方案，则不创建新 Run，写入 `ESCALATE_TO_HUMAN`，Task 进入 `ESCALATED`。

保留每个失败 Run 可以完整追踪每次尝试的 Agent 执行、工具调用、产物、验证证据和失败指纹，也能可靠计算重试次数并识别没有信息增量的重复失败。`Run.recovery_action` 在创建 Run 的同一事务中记录本次恢复动作（`REVISE_IMPLEMENTATION` 或 `REDIAGNOSE`），只标识动作类型和待启动预留，不是实际启动事件。实际启动以首个重新诊断节点派发之前、在持有 Task/Run 行锁的同一事务中写入的 `Run.recovery_started_at` 为准：该事务提交后才开始节点，因此提交后崩溃仍保留计数，同一 Run 反复恢复派发不重复计数；只创建 Run、Supervisor 提议、FailureRecord 或准备 workspace 都不计数。已追踪但尚未启动的 Run 占用重新诊断预算预留，创建和实际启动都在持锁事务中复核预算、并发与租约，不能因漏算待启动 Run 而越过预算。旧恢复 Run 缺少动作标记或缺少启动追踪标记时，其动作或实际启动无法可靠还原，相关重新诊断次数按未知处理并保守地不自动恢复。`UNKNOWN_FAILURE` 的重新诊断由新 Run 从 Supervisor→Explorer 开始，Explorer 成功后立即由 Supervisor 基于旧失败证据和新探索证据复评并路由（可再次 `RE_EXPLORE` 或交给 Planner）；失败类别本身在下一次正式 Verifier 失败时重新分类，不以复评路由伪造新的 FailureRecord。Planner 成功提交后，恢复链路继续用该已提交结果执行现有 Planner 后 Supervisor 路由：仅当路由为 `BASELINE` 且调用方提供匹配当前 Task/base 的 `VerificationConfig` 时，才通过现有 Handoff 图与 Verifier 接收器执行登记基线并保存既有工具调用与报告证据；基线记录后只再做一次 Planner 后 Supervisor 复路由。复路由为 `CODE` 且调用方提供现有 Coder 网关与正数补丁上限时，才通过现有 Coder 接收器执行一次并保存 `PATCH_READY`/`BLOCKED` 结果；Coder 启动前仍由既有基线证据校验核对报告归属/哈希、Supervisor 审批和基线后工作区操作，Coder 只使用工作区与已提交计划，不接触隐藏测试。无有效基线、配置缺失/不匹配、策略或租约不符、其他路由以及已存在但未完成的执行均安全返回，不重复执行、不伪造补丁或验收成功。已提交的 Coder 结果继续交给现有 Coder 后 Supervisor：`PATCH_READY` 的 `VERIFY` 路由在调用方提供匹配当前 Task/base 的 `VerificationConfig`、Verifier 网关和正数 diff 上限时，才通过现有接收器执行补丁后公开测试与语义审查，并只保存 Verifier 草稿/阻塞证据；`REPLAN` 且 Handoff 指向 Planner 时，仅当调用方提供 Planner 网关且该已提交 Handoff 带有唯一 Explorer 证据引用时，才通过现有 Planner 接收器执行一次并保存 `PLAN_READY`/`NEEDS_EVIDENCE` 结果；该结果随后在调用方提供 Supervisor 网关时，按现有 Planner 后 Supervisor 契约再做一次真实路由：`CODE` 且 Handoff 指向 Coder、调用方提供现有 Coder 网关与正数补丁上限、并带有同一 re-plan Planner 下已提交的可信基线引用时，才通过现有 Coder 接收器执行一次并保存 `PATCH_READY`/`BLOCKED` 结果；Coder 启动前仍由既有基线证据校验核对报告归属/哈希、Supervisor 审批和基线后工作区操作。其他路由（`BASELINE`、`RE_EXPLORE`、`BLOCKED`）只保存路由与 Handoff，不派发 Explorer 或 Verifier。保存 Coder 结果后立即停止，不启动该 Coder 的 Supervisor 或 Verifier，也不形成循环。缺少 Coder 配置、无/无效基线、Supervisor/Handoff 不匹配、运行中/失败 Coder 以及租约、预算、权限拒绝均安全返回；重放按 Coder 的 Handoff 身份复用已提交结果，不重复运行模型或补丁。重放按 Planner 执行身份复用同一 Supervisor 执行与 Handoff，运行中或失败不重复执行；缺失 Supervisor 配置、无效 Planner/Supervisor 证据、其他 Planner 输出以及租约、预算、权限拒绝均安全停止。Coder 的阻塞原因、既有 Explorer 引用及 Coder 证据引用经 Handoff 原样进入 Planner 输入，不传递隐藏材料。缺少 Planner 配置、无效 Handoff/证据、Planner 运行中或失败均安全返回；重复调用复用已提交 Planner 结果而不重复执行。`BLOCKED` 或配置不足安全返回，不伪造通过。该段不生成正式 Verification、不改变 Run 终态、不执行隐藏用例或自动循环；隐藏测试材料不进入 Coder/模型上下文。重复调用复用已提交的 Supervisor、基线、Coder 和 Verifier 结果。旧调用不提供 Coder 配置时仍返回 `code_pending`，不创建 Coder 执行；不提供 Verifier 配置则在分发已批准的 `VERIFY` 前安全停止。补丁后 Verifier 草稿的公开初步结果为 `passed` 且可信调用方额外显式提供校准材料（镜像、命令、控制端内存用例、缺陷基准与已知正确版本）时，恢复入口才按既有配置中的独立测试集版本调用现有 `finalize_with_independent_evaluation`，复用同一校准、补丁/差异哈希、草稿归属、工作区锁与正式通过事务。公开失败、缺配置或材料、校准失败、独立评测失败或不可用均保持阻塞，不写正式 Verification、不改 Run 终态；已提交评测回执直接复用，评测失败不重复评测，已完成终态不重跑。隐藏用例只经可信控制端传入评测执行器，不写入 Agent/模型上下文、草稿、普通日志或测试输出。创建 Run 后中断时，同一创建键可幂等恢复未派发的工作区和已提交 Handoff；不会重复计数或重复派发已关联的执行。

---

## 5. Agent 契约与决策权

Agent 契约位于图编排与具体 Agent 执行之间，规定每个角色可以自主决定什么、不得决定什么，以及越界或证据不足时如何交接。它的目标不是限制 Agent 的专业判断，而是防止职责漂移、修改范围失控和无法追踪的隐式决策。

### 5.1 通用控制原则

1. **专业决策权与流程控制权分离**：Explorer、Planner、Coder、Verifier 分别对探索、计划、实现和验收作专业判断；Supervisor 负责准入、分类、恢复动作、路由和人工升级，不取代其他角色生成专业产物。
2. **最小授权**：Agent 只在当前 Issue、上游证据、已批准计划和策略允许的范围内自主决策。改动大小不是是否越权的判断标准；未授权的一行修改也可能越权，得到批准的多文件修改则可以执行。
3. **越界前停止**：Agent 一旦发现继续工作需要改变目标、计划、授权范围或取代其他角色的专业判断，必须在执行越权动作前停止并提交证据。
4. **证据先于交接**：Agent 不能只声称“计划错误”或“无法完成”，必须提供文件位置、调用关系、工具输出、产物引用或其他可复查事实。证据不足或上游产物互相矛盾时，不允许靠猜测继续执行。

Explorer 的读取证据引用包含 `path` 和 `tool_call_id`。RunCoordinator 在提交 AgentExecution 结果的同一事务内校验引用，要求工具调用属于当前 Run 和当前 AgentExecution，工具为 `read_file` 且状态为 `SUCCEEDED`，路径须与审计参数一致。任一引用无效时拒绝提交本次结果。该校验确认读取来源，内容覆盖范围仍取决于实际读取结果及其截断标记。

持久化读取证据时，RunCoordinator 从 ToolCall 的审计结果补入 `truncated`。ContextBuilder 向 Planner 提供读取范围时以工具审计为准，旧引用缺少此字段时回查 ToolCall。若预览已截断，即使存在完整文件产物，也只能把本次回执标记为部分内容可见。

`read_file` 支持可选的行范围：`offset`（1 基起始行）与 `limit`（行数），Explorer、Coder 与 Verifier 的读请求契约都带这两个可选字段——三者都会读源码，大文件靠换窗口而不是重复读开头。路径边界、角色权限与字节上限都不变。**请求与实际分开记录**：请求写入 args 的 `offset`/`limit`，实际结果写入同一条 args 的 `observed`（`start_line`/`end_line`/`total_lines`/`has_more`/`byte_truncated`/`partial_line`）；`total_lines` 只在真的扫描到末尾时才有值，未知一律为 null，不得由请求推算；文件头读取不做整文件扫描，因此被截断时总行数标为未知。`truncated` 仍表示“这次读取不是整个文件”（范围没有覆盖全文、或者字节先用尽，都为真）。**超过字节上限的单行不会被整行丢弃**：按块流式读取（不把超长行读进内存），返回该行的有界前缀并置 `partial_line=true`，这样模型不会拿到空预览；UTF-8 在截断处按增量解码，不会切出半个字符。Explorer 的读请求契约因此允许直接给出范围；路由视图的 `already_read` 与 Explorer 的输入都带上每个文件**实际**覆盖的窗口，并按文件**合并**成 `covered_ranges`（相邻或重叠的窗口并成一段）、附带读取次数与 `total_lines`（未知为 null），只含路径、区间与截断标记、不含正文——逐条窗口列表要模型自己比较区间，一次更窄的重读会看起来像一次新探索；Supervisor 与 Planner 的提示要求在被读文件的更深处缺证据时指明行范围，而不是重复索要同一段开头。`already_read` 是“已经拿到什么”的记录，不得阻止有证据需要的重读。Supervisor 的路由输入还带 `remaining_model_tokens`：由该 Run 已结算的实际用量与未结算请求的预留量算出（与预留路径同一口径），legacy 用量无法可靠计费时该值为 null 而不是 0，路由据此判断是否还有预算再探一次，而不是等 Planner 才发现预算耗尽。范围读取不新增任何旁路：同一个 `resolve_workspace_file` 边界、同一条工具调用记录、同一条证据链。

Explorer 的首个模型执行流程限定为一次 `read_file`。第一份模型输出只选择一个相对路径，经契约校验后交给 Harness。成功读取后，第二份模型输出汇总预览并引用该次读取。两个模型请求共享 Run 的 token 预算。工具请求在 Run 锁下检查剩余额度，Harness 依据角色权限决定是否执行。

探索证据在角色之间传递时有明确口径：`truncated` 只表示**工具自身**截断了读取（`reason=OUTPUT_TRUNCATED`），上下文构造器对预览做的裁剪另记在 `preview_clipped`/`preview_chars`/`preview_limit` 上，不得把两者混为一谈——否则 Planner 会把“我们只发了一部分”误读成“这次读取本身不完整”。早期探索在后续 Planner 调用里会再次发送，因此它们的预览共享一个总量预算（`PRIOR_PREVIEW_TOTAL_CHARS`，按新近程度填满），而不是对每个文件做一个与证据无关的定长截断。Supervisor 的**路由视图**不携带预览正文：它拿到引用、工具截断结论、字符数与 `already_read`（按路径去重的已读清单），因为路由决定的是“下一步做什么”，正文是 Planner 的输入；这样同一次探索不会在三个 Supervisor 提示里被重复发送。“是否存在可审阅的读取”这一守卫按 `preview_chars>0` 判定，而不是按提示里有没有正文，避免把“正文被有意省略”误判成“没有证据”。**提示词必须与它实际收到的内容一致**：Supervisor 的两处路由提示明确说明它拿到的是引用与标记而非文件正文、不得声称自己读过源码，并解释 `truncated`（工具截断）与 `preview_clipped`（本次提示被裁剪）的差别——工具读全了不等于模型看到了全文；Planner 提示同样说明这两个标记，并要求在被裁掉或矛盾的内容影响判断时返回 `NEEDS_EVIDENCE` 而不是假定其余部分；`already_read` 只是历史记录，任何提示都不得用它阻止为补足裁剪或缺失证据而进行的必要重读。
5. **建议不等于授权**：专业 Agent 可以提出带证据的下一步建议，但不能自行决定路由。Supervisor 根据证据决定接收角色和恢复动作。
6. **决策与执行分离**：Agent 产生工具请求；Harness 负责结构校验、策略拦截、真实执行和证据记录。Harness 是确定性执行层，不是 Agent，不负责专业诊断和流程路由。
7. **自动处理必须有出口**：预算耗尽、相同失败指纹触顶、没有合规恢复方案，或 `UNKNOWN_FAILURE` 多次重新诊断后仍无法分类时，任一条件满足即停止自动处理并升级人工。具体阈值由第 6 节 Policy 定义。

### 5.2 五个 Agent 的决策边界

| Agent | 可以自主决定 | 不得越权决定 | 必须停止或交接的情况 |
|---|---|---|---|
| **Supervisor** | 作出 Issue 准入决定；依据证据确定失败类别和恢复动作；决定下一步路由；检查停止条件并决定是否升级人工 | 不代替 Explorer 定位代码，不代替 Planner 制定修改计划，不修改补丁，不代替 Verifier 作专业验收结论 | 当前证据不足以可靠分类时不得猜测，应记录 `UNKNOWN_FAILURE` 并执行 `REDIAGNOSE`；触发任一硬性停止条件时必须升级人工 |
| **Explorer** | 决定探索策略、阅读范围和查询顺序；定位相关文件、符号、调用关系、数据流和依赖关系；基于证据形成根因假设并提出非约束性方案建议 | 不把建议直接变成正式修改计划，不修改补丁，不作验收结论，不决定流程路由 | 无法获得必要代码事实、证据互相矛盾、需要越过策略或权限边界，或现有证据不足以支持可靠探索结论时 |
| **Planner** | 在 Issue、Explorer 证据和授权范围内比较技术方案；确定修改位置、实施步骤、范围限制和预期验证方式 | 不直接修改代码，不擅自扩大 Issue 目标或授权范围，不作验收结论，不决定流程路由 | Explorer 证据不足或矛盾、方案需要扩大授权范围、需要新增未批准依赖或操作，或无法在合规边界内形成可执行计划时 |
| **Coder** | 在已批准计划和范围内选择具体实现方式、内部代码结构和局部实现细节，并生成补丁 | 不改变 Issue 目标、正式计划、技术方案或授权范围；不决定验收结果和恢复路由；不自行准备或安装环境 | 发现计划定位或方案可能错误、完成修复必须修改范围外代码、需要新增未批准依赖、环境不满足，或所需操作被策略拒绝时 |
| **Verifier** | 按验收规则请求确定性检查并审阅结果；基于 Issue、diff、测试和真实输出进行语义审查；在证据充分时作出通过或失败结论 | 不修改补丁或环境，不用 LLM 结论推翻确定性失败，不决定补丁修复方案和恢复路由 | 验收所需证据缺失或互相矛盾、环境或工具问题使检查无法有效执行，或发现实现问题但修复它会越过验收职责时 |

代码提供 `AgentRole` 枚举作为权限配置使用的角色标识，仅对应上述五个既有角色；枚举本身不授予任何工具或文件操作权限。

Supervisor 拥有的是流程控制权，而不是全部专业权力。应用层与 Harness 负责校验、持久化并执行 Supervisor 的合法决策；Supervisor 本身不直接绕过这些机制修改数据库状态或真实环境。

### 5.3 越界交接契约

正常完成与越界交接必须区分：正常完成表示 Agent 已在授权范围内产出有效结果；越界交接表示继续执行需要新的证据、授权或其他角色的专业能力。越界交接不是“先做了再申请”，而是在越权动作发生前停止。

`Handoff.context` 至少应包含：

| 内容 | 说明 |
|---|---|
| `completed_work` | 已完成的探索、计划、实现或验证工作，以及相关 `Artifact` 引用 |
| `blocker` | 当前无法继续的具体步骤、缺失信息或权限边界 |
| `evidence_refs` | 文件路径、符号或行号、代码片段、调用关系、`ToolCall`、测试输出或其他可复查证据 |
| `decision_required` | 继续处理所需要的新授权、事实确认或专业能力 |
| `scope_impact` | 可能新增的文件、依赖、公共行为和回归风险 |
| `recommendation` | 可选的带证据建议；它不具有路由或执行授权 |

普通 Agent 可以说明自己认为需要哪类能力，但不能自行批准路由。Supervisor 根据交接证据选择接收角色；应用层由 RunCoordinator 协调持久化 `Handoff.to_agent_role`，提交成功后再执行路由。Harness 只处理工具请求，不负责角色路由。

对已完成的上游执行，RunCoordinator 在提交 Supervisor 的成功结果时一并写入其批准的 Handoff。交接来源必须已结束并属于同一 Run，生成的交接 ID 写入 Supervisor 的执行输出。调用方只能在提交成功后取得该 ID。

准入决策契约区分 `ELIGIBLE`、`DECLINED` 与 `BLOCKED`。`BLOCKED` 表示信息不足以作决定，不是新的 Task 状态；例如网络超时本身不能证明准入不通过。模型引用的依据须解析到调用方提供的观测，引用存在不证明依据充分或观测本身可信。一次准入尝试已接入可信仓库检查、模型网关与条件状态更新；已以真实模型、本地 Git 仓库及隔离测试 schema 验证 BLOCKED 和 ELIGIBLE 两种结果。有界单次扫描已实现，持续循环和完整失败恢复仍未接通。

单次准入 CLI 要求操作者指定 Task ID 和与 Task 仓库值完全一致的可信仓库；它调用一次准入，不自动选择任务或领取 Run。默认 Policy 未启用准入模型额度，须在创建 Task 时由可信内部配置显式设置。真实模型、隔离 PostgreSQL 和临时本地 Git 的独立 CLI 进程已验证；此测试没有运行专业 Agent 或验收。

准入阻塞不新增 Task 状态：对 PENDING 条件更新保存最近一次成功提交的阻塞类别、理由、可选信息缺口和数据库时间；迟到阻塞结果不能覆盖已经准入的状态。它不等于完整准入尝试历史：系统异常、进程崩溃和本字段引入前的历史尝试可能没有记录，空值不能证明从未尝试。已实现单次有界扫描与重试条件，但持续调度和完整恢复尚未实现。

阻塞记录含重试提示：仓库检查明确超时记为 `transient`，表示可考虑有界延迟重试，不等于已经授权重试；信息不足、预算不可用、仓库未获信任及其他未明确分类的结果记为 `needs_review`，不得只因等待一段时间就自动重试。Task 的 Issue 内容在创建后固定；当前要补充描述须创建新 Task，旧 Task 记录保留，不假定二者已有自动关联。

阻塞记录中的 `consecutive_timeouts` 统计从当前计数开始连续成功记录的仓库超时，Task 行锁保证并发写入不丢失，其他已记录的阻塞归零。系统异常、崩溃或历史未记录的尝试不在计数内，不能将其当作完整尝试历史；旧的 transient 记录缺少计数时保持未知（null），不猜测次数。上限判断已实现，自动重试尚未实现。

Run 的首次交接是上述来源规则的特例：学生确认由本次 Run 的 Supervisor 作为来源，目标为 Explorer。仅当当前 Run 尚无专业 Agent 执行、也没有任何既有 Handoff 时，允许 Supervisor 在同一事务中结束自己的执行并创建这条交接；两者提交或回滚。Task 的准入发生在 Run 创建前，不把本次 Run 内的 Supervisor 执行假定为执行过准入的同一条记录。后续交接仍引用已结束的上游执行。

首次路由已提供结构化 Supervisor 决策入口：模型只可选择启动 Explorer 或报告阻塞，不生成专业探索结论。Worker 使用数据库中的 Issue、基准和 Task Policy 构造输入；模型请求经现有预算网关，格式修复受 Task 快照限制。Explorer 的读文件权限或可用工具预算不足、图步骤不足，以及模型预算无法接纳请求时先记录阻塞，不创建交接。当前阻塞只作为中间结果，尚未自动完成 Task 升级或失败恢复；Task 准入和部分后续路由见相应说明，完整停止条件仍需接入。

后续已有两类单次 Supervisor 路由：Explorer 成功提交后可建议交给 Planner、明确补查目标或阻塞；Planner 成功提交后可建议交给 Coder、补查或阻塞。Supervisor 与 Planner 的探索上下文都包含同 Run、同 Explorer 成功读取的预览和截断标记；摘要是模型观点，不代替原始预览。Planner 报告 NEEDS_EVIDENCE 时，确定性检查不允许直接路由 Coder；Coder 缺少补丁权限或预算时阻塞。监督决策及对应 Handoff 在同事务提交，交接来源分别指向已完成的 Explorer 或 Planner。真实 DeepSeek 已在隔离数据库与模拟 Explorer 证据中验证过 Explorer 后 PLAN 决策；一次模拟模型集成测试已经在临时真实 Git 工作区写入补丁，但尚未完成 Verifier 验收。连续多轮调度、补查是否形成信息增量以及完整停止条件尚未接通。

在临时合成 Git 仓库和隔离数据库的真实 DeepSeek 尝试中，一次 Coder 补丁因原文件末行没有换行且补丁遗漏相应标记而无法应用；独立检查补充标记后的补丁可以应用。读取证据现在标注 `ends_with_newline`：完整可用的读取据实际内容判断，截断或不可用时为 null。已验证的 Explorer 预览也可传递给 Coder，补丁应用失败时保存 Git 有界标准错误。另一独立真实模型尝试在临时仓库中提交了补丁，人工读取的文件内容与合成任务的目标一致；尚未进行独立 Verifier 验收，不能计为真实 Issue 修复成功。

接收方启动时，在同一事务内校验 Handoff 的 Run 归属和目标角色，并把 `handoff_id` 写入新的 AgentExecution。每条 Handoff 最多关联一次接收执行。再次提交同一交接的启动请求时，RunCoordinator 返回已关联执行的 ID 作为冲突信息。

`Handoff` 与 `FailureRecord` 不能混用：`Handoff` 记录角色之间传递的上下文；`FailureRecord` 只在一次 Run 已经失败后记录失败类别、指纹和恢复动作。Agent 及时发现证据不足并交接，不必然意味着当前 Run 已失败。

### 5.4 Agent 与 Harness 的执行权边界

Agent 不直接操作真实仓库或命令环境，而是产生结构化工具请求。请求至少表达工具名、参数、工作目录和执行目的。Harness 按以下顺序处理：

1. 校验 Agent 输出契约、工具名、参数结构和工作目录；
2. 根据命令白名单、安全规则和当前授权机械地允许或拒绝请求；
3. 只执行通过校验和策略检查的操作；
4. 保存 `ToolCall` 的请求参数、执行状态、标准输出、错误输出、退出码和相关产物引用；
5. 将结果作为证据返回给当前 Agent 或 Supervisor。

Harness 可以依据确定性规则判断“请求是否合法”和“命令是否执行成功”，但不能判断失败的专业根因。例如 `pytest` 退出码非零只证明命令失败；它究竟属于环境、计划还是实现问题，必须由 Supervisor 结合证据分类。Harness 也不能自行修改计划、补丁、环境准备方案或恢复路由。

命令被白名单拒绝时，正常链路为：

```text
Agent 提交工具请求
        |
        v
Harness 校验失败，拒绝执行并保存拒绝证据
        |
        v
Supervisor 分类为 POLICY_VIOLATION
recovery_action = FIND_COMPLIANT_ALTERNATIVE
        |
        |- 仓库支持的准备方式不清楚 -> Explorer 查证
        |- 需要改变实现方案         -> Planner 更新计划
        |
        v
Harness 执行最终获准的合规操作
        |
        `- 没有合规替代方案 -> Supervisor 升级人工
```

### 5.5 典型边界流程

#### Coder 发现计划可能错误

Coder 不能先修改计划外代码来验证自己的猜测。它应停止执行，提交原计划无法继续的步骤、相关文件和符号、调用或数据流证据、可能受影响的文件与风险。Supervisor 根据证据分类：若为探索错误则路由 Explorer，若为计划错误则路由 Planner；正式计划更新后再由 Coder 实现。

#### Verifier 发现实现问题

Verifier 可以依据具体文件位置、代码片段、Issue 预期和测试证据判定语义审查失败，但不能顺手修改补丁。它保存失败结论与证据，由 Supervisor 决定恢复动作；需要修改实现时路由 Coder。

#### 证据不足，无法区分探索错误和计划错误

Supervisor 不得凭猜测记录 `EXPLORATION_FAILURE` 或 `PLANNING_FAILURE`。当前记录为 `UNKNOWN_FAILURE`，选择 `REDIAGNOSE`，由 Explorer 补充代码位置、调用关系和依赖事实；新证据返回后，Supervisor 再归入真实类别并路由。

---

## 6. Policy 与资源控制

本节阈值是初始配置，尚无真实评测证明它们最优。Policy 根据正式记录、Task 的配置快照和当前权限给出允许性判断及理由；它不重写失败类别，也不自行扩权。

### 6.1 两层预算与停止条件

Task 外层预算限制额外 Run 数；Run 内层预算限制 Agent 节点、工具请求、模型 token 和时长。初始 Run 不计重试。从 Run2 开始，每新建一个 Run 消耗一次额外尝试；已创建 Run 数为 `n` 时，额外尝试数为 `max(0, n - 1)`。未创建新 Run 的人工升级不消耗重试次数。

同一 `signature` 在 Task 全生命周期累计，中间出现其他失败也不清零。出现以下任一条件时停止自动恢复，不创建下一 Run，Task 进入 `ESCALATED`：

- Task 的额外 Run 预算耗尽。
- 同一 `signature` 累计达到上限。
- 没有符合授权、角色权限和 Harness 规则的恢复方案。
- `UNKNOWN_FAILURE` 的重新诊断达到上限，仍不能分类或形成合规方案。

当前 Run 如果已经失败，应将其结束为 `FAILED` 并在同一事务写入 `FailureRecord`。允许新 Run 还需要剩余预算、合规方案以及新证据或新策略。确认的临时执行错误可以在受控额度内重试同一步骤，但仍须检查副作用、幂等性和预算；若已结束当前 Run，恢复必须进入新 Run。

### 6.2 初始配置

| 配置键 | 初始值 | 计量口径 |
|---|---:|---|
| `max_retries_per_task` | 3 | 最多 3 个额外 Run，总尝试最多 4 次；合法范围为非负整数，0 表示仅允许初次 Run、不允许额外重试，负数配置应拒绝 |
| `max_same_signature_occurrences` | 3 | 同一 Task 内相同失败指纹的累计次数；合法范围为正整数，最小值 1 表示首次出现即触顶，0 不表示关闭或取消该限制 |
| `max_rediagnose_attempts` | 2 | `UNKNOWN_FAILURE` 的重新诊断次数；非负整数，0 表示没有重新诊断额度 |
| `max_graph_steps_per_run` | 30 | 已记录的 AgentExecution 次数，包括 Supervisor；非负整数，0 表示没有可用节点额度 |
| `max_tool_calls_per_run` | 60 | 已持久化的 ToolCall 请求数，包括拒绝、超时和失败请求；非负整数，0 表示没有可用工具请求额度，拒绝审计的计量口径不变 |
| `max_llm_tokens_per_run` | 80000 | Run 内所有模型调用的输入与输出 token 总和；非负整数，0 表示无可用 token、不得发起模型调用，不表示无限制 |
| `max_admission_llm_tokens_per_task` | 未配置 | Task 准入阶段独立的 token 上限；缺失或 null 不允许准入模型调用，0 表示没有额度；设置为非负整数后由准入网关计量 |
| `max_admission_consecutive_timeouts` | 3 | 连续已确认的仓库准入检查超时达到此数停止自动尝试；包含首次检查。旧快照缺失时为未知，不自动授予重试。 |
| `max_admission_attempts` | 5 | 同一 Task 累计成功领取准入的次数上限，包含首次及人工领取；达到后不再自动领取。旧快照或计数缺失时不自动授权；人工单次操作仍可进行。 |
| `admission_timeout_retry_delay_seconds` | 60 | 仓库超时的最短等待时间（秒）；正整数，旧快照缺失时不作自动就绪判断。60 秒是可配置的起始值。 |
| `max_output_repairs_per_agent_execution` | 1 | 单次 AgentExecution 内因解析或契约校验失败而发起的额外模型请求数。非负整数，0 表示禁止修复，首次生成不占用修复次数。每次请求仍受 Run 的剩余 token 与时长预算约束 |
| `tool_call_timeout_seconds` | 300 | 单次工具调用最长时间，V1 使用正整数秒，仍受 Run 剩余时长限制；拒绝 0、负数及非整数秒，不以 0 表示无限等待 |
| `max_run_duration_seconds` | 1800 | 单个 Run 的最长执行时间，沿用同类时长约束，V1 使用正整数秒，拒绝 0、负数及非整数秒 |

这里的 graph step 是项目定义的 AgentExecution 计数，不直接等于 LangGraph 内部 super step。一个 AgentExecution 可以包含多个模型请求与 ToolCall。ToolCall 的计数和是否真正执行分别记录。模型服务未返回 usage 时标明估算或未知，不当作零。

恢复策略纯判断已区分重新诊断次数未知：没有其他已确认停止条件时返回 `undetermined`，不假定次数为零或已触顶；其他已确认停止条件触发时仍可判定升级人工。正式 UNKNOWN_FAILURE 失败事务从同 Task 已实际启动（`recovery_started_at` 已写入）且带 `recovery_action = REDIAGNOSE` 标记的 Run 统计重新诊断次数，创建 Run、提议和 FailureRecord 不计入。已追踪但尚未启动的 Run 作为预算预留计入消费，避免崩溃后待启动 Run 漏算而越过预算。旧恢复 Run 缺少动作或启动追踪标记时次数为未知，协调器保守地不启动新重新诊断并升级人工。启动事件在首个重新诊断节点派发前、与预算/并发复核相同的 Task/Run 行锁事务中提交，提交后崩溃仍保留计数；同一 Run 反复派发只记录一次。`start_rediagnosis_run` 在 Task 行锁内复核租约、历史、预算、同指纹次数和 Explorer 工具权限，创建幂等键重放不重复计数。

### 6.3 检查、审计与版本

- 创建新 Run 前核对 Task 的预算、重复指纹、重新诊断和合规方案。Agent 节点、模型请求和工具调用前检查各自额度。
- 工具执行期间监控超时，Run 执行期间监控总时长；Run 到期要停止继续执行，不能只在结果返回后标记超额。
- 模型请求限制可用输出额度并设置超时。每次判断保存所用配置、计数、证据引用和理由。
- Run 数量用于计算额外尝试数；`Run.recovery_action` 标记恢复动作类型，`Run.recovery_started_at` 记录首个恢复节点实际启动的事务事件，后者用于统计重新诊断次数；AgentExecution 提供节点和 token 用量；ToolCall 提供请求数与耗时；Run 起止时间提供时长；FailureRecord 提供失败指纹及失败决定历史。
- Task 创建时保存 `policy_version` 和完整 `policy_snapshot`。同一 Task 的所有 Run 使用该快照；修改系统默认配置只影响后续 Task。
- 生成快照时复制配置内容，避免写入数据库前与系统默认字典共享嵌套的可变容器；`build_policy_snapshot` 使用标准库 `deepcopy` 生成独立字典。复制不等于配置完整性校验，正式创建仍须保证预算与权限配置完整。
- 预算配置拒绝未定义字段；字段名拼写错误应在配置校验时明确报错，不得静默忽略并继续使用默认额度。已定义字段未提供时仍可使用本节明确的默认值。`PolicyBudget` 通过 `ConfigDict(extra="forbid")` 表达此约束。
- 工具权限采用显式允许语义：明确的空允许列表表示没有相应工具权限，漏配或未声明的权限不得推断为允许全部。工具允许名单也不替代 §5.4 的其他授权与安全检查。
- `PolicyPermissions.allowed_tools_by_role` 保存角色到工具名称列表的映射，角色键使用 `AgentRole`。必须明确提供五个角色的条目；缺任一角色、缺整份映射或出现未知角色时配置校验报错，不能将漏配静默补成空权限。五个角色都明确提供空列表是合法的无工具权限声明。当前模型只校验该结构和角色完整性，工具是否注册及操作是否获准仍由相应校验与 Harness 落实。
- `PolicyConfig` 将 `policy_version`、`budget` 和 `permissions` 作为必填字段组织在同一配置对象中，并拒绝未知字段；预算和权限各自保留其嵌套校验。`policy_version` 必须包含至少一个非空白字符；校验时用 `strip()` 判断，合法输入保留原文。Task 表自身仍只有 `NOT NULL` 约束。此结构本身不证明版本标签与具体内容的语义对应，创建 Task 时仍须从同一配置取得版本并独立复制快照。
- 新 Task 使用启动策略 `bootstrap-no-tools-v6`，采用 §6.2 的默认预算，为五个角色配置空工具允许列表；准入模型预算未配置。既有 `bootstrap-no-tools-v1` 等快照缺少 `max_output_repairs_per_agent_execution` 时按 0 解析，缺少准入预算、超时上限、累计尝试上限或等待时间时按未配置处理，不改写原快照。调整默认预算或权限时使用新的 `policy_version`，供后续新 Task 采用。
- 可信内部调用可向 `TaskService` 提供产生 `PolicyConfig` 的工厂，以便为新 Task 使用明确配置的策略；创建前重新校验并保存独立快照。同一提交标识的重放继续使用已保存的原策略，不调用新工厂。现有 HTTP 接口没有接收客户端策略参数，默认仍使用 `bootstrap-no-tools-v6`；可信任务包如何选择策略尚未接通。

准入超时重试就绪检查在取得 Task 行锁后读取数据库当前时间，再比较持久化阻塞时间和快照中的等待间隔；上限达到、信息未知或非临时阻塞不能视为就绪。`READY` 只是查询时刻的判断，锁释放后不构成执行许可。指定 Task 的单次自动重试入口在持锁事务中重新检查状态、上限及等待时间后领取，随后核对可信仓库并执行；有界单次扫描已实现，持续循环尚未实现。

准入期间独立领取 Task，使用随机令牌区分每次领取，使用数据库时间判定到期；结果提交和模型预算预留重新检查当前令牌及到期时间。过期或被接管的执行不能提交结果，但模型真实用量仍可结算。当前准入领取无心跳，租约使用至少 90 秒的起始值；到期不会自动终止已经发出的模型请求。领取提交与准入累计尝试计数在同一事务中发生，崩溃后的已领取尝试仍被计数，但不伪称已经超时。自动重试在领取前同时检查累计尝试上限和已确认超时上限；此机制不代替完整崩溃恢复。

准入触顶时在 Task 保存人工处理提示，不更改 PENDING，也不伪造 Run 内 Handoff。`GET /tasks/{task_id}/admission` 查询提示和最近阻塞；如果最后一次领取崩溃并到期，查询或单次扫描在 Task 行锁下按快照与计数刷新并保存提示，正在有效执行的最后一次领取不提前报触顶。人工明确发起新准入会清除旧提示，若再次触顶则重新记录。查询会进行必要的数据库写入；当前没有后台循环、主动推送、人工任务队列或完整人工审查 UI。

单次扫描接受显式可信仓库集合，按 Task 创建时间和 ID 分页，每页最多检查配置数量且最多启动一次准入。扫描结果包含游标；调用方需推进游标并在一轮结束后重新开始，单页未找到任务不代表全队列空闲。首次自动尝试只考虑已知从零开始且配置准入预算的 Task；已确认超时按快照的等待时间和上限重试；无阻塞记录的首次尝试若持有的领取过期，等待间隔并在预算和次数允许时可恢复。模型等执行异常记录为需要检查，不静默自动重试。旧历史未知、未获信任或条件不满足的任务不自动执行。

准入预算在 Task 行锁下预留并在模型调用前提交；结算使用供应商报告的真实用量，超时或无可信用量时保留预留。迟到用量可结算，但不能以此取得准入决定的提交权。输入 token 为估算值，输出受请求上限约束；实际用量仍可能超过估算，应如实结算并阻止进一步超额请求。准入模型网关已接入内部准入尝试并在隔离测试中调用真实模型。

评测时同时检查合理任务是否被提前停止，以及重复失败是否持续消耗资源。六任务评测只能用于小规模工程校准，不能证明任意仓库的泛化能力。

## 7. 架构、事务与恢复

### 7.1 组件层次与持久化真相

1. 接口与任务入口：FastAPI 接收、校验、提交和查询；CLI 启动 worker、调试与 benchmark。两者复用应用服务。
2. 编排层：LangGraph 调度当前 Run 的节点和条件边。
3. 控制层：状态机、Supervisor、Policy、RunCoordinator、执行权和恢复。
4. 专业 Agent 层：Explorer、Planner、Coder、Verifier。
5. Harness 与工具层：工具注册、权限与路径校验、工作区、命令执行、超时、ToolCall。
6. 持久化与产物层：Repository、SQLAlchemy、PostgreSQL 和文件型 Artifact。

这些层是职责边界，不表示每次请求都按六层顺序串行调用。RunCoordinator 控制事务和生命周期；Repository 只封装数据读写，不自行提交生命周期事务。PostgreSQL 是唯一持久化真相来源。LangGraph 的 `InMemorySaver` 只保存进程内、当前 Run 的临时图状态，不再引入独立的持久化 checkpointer。

新执行周期从 PostgreSQL 正式记录重建 GraphState，图从 Supervisor 开始，再根据正式恢复动作路由。重建状态不能原地继续已失去执行权的旧命令。

首个 LangGraph 交接周期从 Supervisor 节点开始，根据 PostgreSQL 已提交的 Supervisor 结果及其 Handoff 选择已注册的接收节点。接收节点在调用角色前重新核对执行权和交接记录。每次调用使用独立的 InMemorySaver，图状态仅暂存本次路由所需标识。

Task 创建入口为 `POST /tasks`，首次成功提交任务记录后返回 HTTP 201，同标识同内容复用返回 HTTP 200；该状态码不表示 Issue 已修复。响应契约使用 `TaskCreateResponse`，包含 `id: UUID` 和 `status: TaskStatus`；两个值均从 TaskService 返回的已提交 Task 中取得。请求输入为 `TaskCreateRequest` 的 title、description、repo，缺必填字段时由 API 模型校验拒绝，不调用创建服务。接口使用 `Depends(get_db_session)`，将本请求的 Session 交给 TaskService；API 不重复提交事务。

Task 查询入口为 `GET /tasks/{task_id}`，路径参数使用 UUID 类型校验，格式错误返回 HTTP 422 且不调用查询服务；ID 合法时调用 TaskService。成功 HTTP 200，响应契约 `TaskDetailResponse` 包含 `id: UUID`、`status: TaskStatus` 及字符串字段 `title`、`description`、`repo`，用于核对任务内容与目标仓库。任务不存在时服务返回 None，由 API 转换为 HTTP 404，detail 为 Task not found，使服务可供 CLI 等入口复用。接口已实现；跨请求真实创建/查询链路的最新验证状态见 PROGRESS，不以替身测试证明数据库链路成功。

### 7.2 原子领取与并发约束

Task 准入的状态保护：Supervisor 给出准入判断，应用服务校验并持久化；准入通过只允许 PENDING → ELIGIBLE，准入不通过只允许 PENDING → DECLINED。迟到的准入决定不得覆盖已经变化的状态，也不负责将任务启动为 RUNNING。不能只根据之前读取的 PENDING 在 Python 中检查后无条件更新；持久化时应在同一条数据库更新中限定 Task ID 及当前状态 PENDING。条件不匹配时更新 0 行，不覆盖既有状态。该机制已讲解并于 2026-10-31 验证学生理解；仓储 update_admission_status(task_id: UUID, target_status: TaskStatus, *, base_commit: str | None = None) -> int 已实现，目标仅允许 ELIGIBLE/DECLINED，其他目标抛 ValueError。通过 update(Task).where(Task.id == task_id, Task.status == PENDING).values(status=target_status, base_commit=base_commit) 执行并返回 rowcount，不提交或回滚事务。0 行不等于任务不存在：服务按 ID 查询，区分 None 与已存在但准入状态冲突；查询通过 populate_existing=True 重新读取，不能把 Session 缓存的旧状态当作数据库当前值。仓储已有隔离 PostgreSQL 验证，包含回滚、旧对象及实际并发；准入服务的方法名确定为 apply_admission_decision；学生已确认两种失败使用不同业务异常：按 ID 查不到任务使用 TaskNotFoundError，任务存在但准入状态冲突使用 TaskStateConflictError，避免统一 None 丢失业务原因。业务异常不携带 HTTP 响应，具体入口负责转换。准入服务与异常类已实现并经隔离 PostgreSQL 测试；Supervisor 内部准入尝试已接通，尚未完成真实模型验收。apply_admission_decision(task_id: UUID, target_status: TaskStatus, *, admission_record: AdmissionRecord, base_commit: str | None = None) -> Task 使用 with session.begin() 管理完整事务；先条件更新，再调用仓储 get_by_id(task_id, populate_existing=True) 读取。任务不存在抛 TaskNotFoundError（LookupError 子类，消息带任务 ID）；任务存在但影响 0 行抛 TaskStateConflictError（ValueError 子类，消息带 ID 和读取时状态）。异常离开事务块回滚，成功在退出事务块并提交后返回 Task；其他执行/提交错误向外传播，不伪造成功。已有普通查询保留默认 populate_existing=False，此可选参数只在需要刷新时显式使用。重复或并发的准入调用若任务已非 PENDING，按状态冲突处理，不把同目标视为新的成功准入；本方法只保存传入的准入决定，不生成判断、不创建 Run，也没有新增允许客户端设置状态的 API。

Task 首次创建可以独立提交，此时没有 Run 是合法状态。worker 领取已有 `ELIGIBLE` Task，或符合恢复条件的 `RETRYING` Task 时，在短事务中以 `SELECT ... FOR UPDATE SKIP LOCKED` 取得候选，重新核对状态和 Policy，创建 Run、记录执行归属与租约，并把 Task 转为 `RUNNING` 后一次提交。任一步失败则整体回滚。

指定 Task 的首次启动协调器（2026-10-31 已实现）：coordinator.py::RunCoordinator(session, lease_config=None) 提供 start_initial_run(task_id, worker_id, creation_idempotency_key) -> tuple[Run, bool]，沿用已有服务的 created 调用标记，新建 True、重放 False，不新增数据库字段。它是内部定向启动接口，先保证 Task 范围清楚；自动扫描首次领取现已组合，恢复入口仍后置，不新增公开启动 HTTP 接口。

该方法拒绝空白 owner/key，保存原字符串不静默改写。所有查询与写入放在外层同一个 with session.begin()：先查既有创建结果；需要新建时用 TaskRepository.get_by_id_for_update 按 ID 执行 FOR UPDATE SKIP LOCKED（不先筛掉非 ELIGIBLE，便于区分状态错误），锁后再次查询原创建标识，处理首次查询与加锁之间已经提交的请求。锁查询无结果且仍无已保存结果时，以普通刷新查询区分不存在（TaskNotFoundError）与暂时锁繁忙（TaskClaimBusyError），后者保留状态、允许稍后重发原请求，不等于 Run 失败。

真正新建要求当前 Task 为 ELIGIBLE 且没有任何 Run 历史；仅允许 attempt_number=1，其他状态或不一致历史抛 TaskStateConflictError。从 Task 已保存的 policy_snapshot 校验 PolicyConfig，并确认与根 policy_version 一致，不重建默认策略；错误传播且不创建 Run。首次尝试不消耗额外重试额度，max_retries_per_task=0 仍允许 Run1。本步读取 Run 总时长用于租约上限；节点、工具、token 的运行时预算执行仍待后续接入，不能据此宣称完整 Policy 执行器完成。

创建 Run 时记录 RUNNING、owner/key、同一数据库时间的 started_at/last_heartbeat_at，以及初始 lease_expires_at = started_at + min(lease_duration_seconds, max_run_duration_seconds)，使执行权不超过本次 Run 的固定截止时间。写入 Run 并把 Task 改为 RUNNING 后统一提交；任一步或提交失败则回滚两者，其他数据库错误不伪装成创建参数冲突。重放只返回旧结果，即使原租约过期或原 Run 已终态，也不更新状态/owner/起止时间/租约；它不代替执行权检查。协调层已有 36 项真实隔离 PostgreSQL 测试，包含实际并发、锁后重查、提交失败及固定快照，不代表真实 Agent 已执行。

自动扫描首次领取（2026-10-31 已实现）：RunCoordinator.claim_next_initial_run(worker_id) -> Run | None 在同一个 with session.begin() 内调用 get_next_eligible_for_update，持锁取得一条 ELIGIBLE Task 后直接复用 _create_initial_run，记录执行权、Run1 与 Task.RUNNING 一并提交；不调用另一个开启事务的公共启动方法，不先提交查询再创建。空集合或候选全部被锁时返回 None，不代表全部任务已完成；其他错误传播并回滚，不静默当成空队列。

一次 claim-next 调用表示取下一项新工作，不是前一次调用的幂等重发；协调器作为自动调度请求的发起方，在选中 Task 并持锁后、创建前生成新的 UUID 文本创建标识并保存，字段本身仍是 Text、没有数据库随机默认值。返回的 Run 含 task_id 与创建标识，需要查询/重放同一次创建时使用已有 start_initial_run 的明确 Task/标识范围；不把“取下一项”做成跨 Task 复用同一请求。重复调用可分别领取不同任务，不会重开已 RUNNING 的 Task，也不执行准入或 RETRYING 恢复。本轮新增十五项隔离 PostgreSQL 测试，协调器专项共51项通过，覆盖事务/锁连续性、并发分配、回滚、重放定位、配置使用和错误传播；单次领取 Worker 进程入口已实现，实际 Agent 执行尚未实现。

单次领取 Worker/CLI（2026-10-31 已实现）：worker.py::Worker 保存 Session 工厂、worker_id 与 LeaseConfig；claim_once() 创建本次独立 Session，调用 claim_next_initial_run，并在关闭 Session 前将已提交 ORM 结果转换为独立、不可修改的 RunClaim 回执（Task/Run ID、编号、状态、归属、创建标识、UTC 租约到期时间）。返回回执可在 Session 关闭后读取，不是实时执行权凭证，不把已提交 Run 因关闭读取事务而回滚。

命令入口为 python -m devflow_ai.worker --once，必须显式选择单次模式，支持可选 worker-id 与两个租约参数，不新增依赖或公开 HTTP 接口。未提供 worker-id 时每次进程生成 UUID 标识；参数先校验，CLI 自己创建/最终 dispose Engine。成功领取输出 worker_run_claimed，当前无可用候选输出 worker_idle，均退出0；配置错误 stderr/worker_configuration_error 退出2，运行失败 stderr/worker_claim_failed 退出1，不把错误伪装为空闲；配置字段提示不含原始输入，运行错误只输出安全错误类型，不打印DSN/SQL参数/完整异常。主调用层报告失败，不替代协调层决定 Run 的正式结果。

控制端另有 `python -m devflow_ai.control --inspect-expired-run <RUN_ID>`：只读取一条 Run 的控制面快照并输出 JSON（状态、归属、租约到期、固定时长截止、仍停留在 RUNNING 的孤儿执行、未决的写类工具调用，以及 `takeover_blockers` 列表，取值为 `run_not_running`/`lease_not_expired`/`task_not_running`/`duration_exhausted`/`unsettled_mutation`/`orphaned_executions`），供操作者决定"接管续跑"还是"关闭并恢复"。V1 不新增审计表：控制端操作只通过 Run 归属、FailureRecord 与 Verification 记录间接留痕，**这些记录不能可靠证明是谁操作了控制端**，多人使用或正式运营前需另行设计操作者身份与审计。该命令不调用模型、不加锁、不写库、不迁移；工作区 advisory lock 刻意不在此探测，因为获取它本身就是副作用，接管入口会重新核验。另有 `--close-expired-runs --max-runs N`：循环发现过期 RUNNING Run 并按派生的 `UNKNOWN_FAILURE` 决策关闭，逐个输出 `control_expired_runs_closed`；它只关闭、不创建恢复 Run（恢复 Run 需要模型网关，由具备网关的 worker 通过待恢复发现入口接手），发现与关闭之间若已被其他回收者处理则该条记入 `skipped` 而不重试。成功退出 0，配置错误 stderr/`control_configuration_error` 退出 2，执行失败 stderr/`control_command_failed` 退出 1，均不打印 DSN、SQL 参数或完整异常。需要模型网关的入口统一由 `devflow_ai.model_gateways.model_gateways_from_env` 装配 Supervisor、Explorer、Planner、Coder、Verifier 五个按角色命名的网关（同一适配器、各自独立的包装，避免"用 Supervisor 网关跑 Coder"这类角色混用）：凭据只从环境变量 `DEEPSEEK_API_KEY` 与 `DEVFLOW_DEEPSEEK_MODEL` 读取，三个网关共用一个 HTTP 客户端并在退出时关闭，输入 token 先用保守估算预留预算、再由服务端回报用量结算；缺凭据抛 `ValueError` 让调用方按配置错误处理，密钥不打印、不落盘、不进入日志。该装配允许注入客户端或生成函数，便于在不触网的情况下验证装配本身。需要恢复 Run 时用 `--reclaim-expired-runs --max-scans N --workspaces-root ... --artifacts-root ...`：它有界地执行既有的单次回收（关闭 + `REDIAGNOSE` 时启动恢复 Run），一旦某次扫描返回 `idle` 就提前结束，因此空闲系统只花一次扫描；每轮结果压平成 `run_id`/`outcome`/`recovery_action`/`policy_reasons`/`rediagnosis_outcome` 输出到 `control_expired_runs_reclaimed`。缺凭据或目录不存在按配置错误退出 2，不把配置问题伪装成执行失败；异常上抛而不是吞掉成不完整的报告。需要长跑时用 `--run-recovery-loop --run-for-seconds N [--poll-interval-seconds S] [--max-consecutive-failures K]`：它在给定墙钟预算内反复执行同一次回收，空闲扫描不结束循环，连续失败达到 K 次才把异常上抛，其余瞬时故障按线性退避重试；预算到点即返回 `control_recovery_loop_finished`，因此该命令不会变成长时间无人监管的服务。工作区与产物根目录、读取上限、网关配置要求与有界回收相同。需要接管同一条 Run 时用 `--take-over-expired-run <RUN_ID>`：先取工作区 advisory lock 并核对没有未决写操作，取得成功后才换所有者与租约；只有该 Run 没有任何仍停留在 RUNNING 的孤儿执行时才就地续跑，否则只报告阻塞（`blocked_by_orphaned_nodes`）。输出 `control_expired_run_taken_over`（含 `previous_worker_id`、`unknown_tool_calls`、`orphaned_executions`、`continued_outcome`）；租约仍有效报 `control_run_not_expired`、工作区有活操作报 `control_takeover_refused`，两者都退出 1 并区分事件名，因为一个是"等会儿再试"、另一个是"该走关闭路径"。

当前命令仅领取并保存 Run 后退出，不发送定时心跳、不执行 Agent、不标 PASSED、不连续领取。领取后 Run 保留 RUNNING，租约正常流逝；实际心跳/执行/到期恢复仍需后续组件，不把此命令当长期 Worker 部署。已完成十四项 Worker 专项测试，包括独立 Python -m 进程在预检隔离 schema 中真实领取/再空闲和失败回滚，未操作用户 public 数据。操作说明见 WORKER.md。

Run 仓储最小接口（2026-10-31 已实现）：RunRepository 保存外层传入的 Session；add(run: Run) -> Run 执行 add/flush，取得 ID 与默认值后返回，不提交或回滚。get_by_creation_idempotency_key(task_id: UUID, creation_idempotency_key: str) -> Run | None 同时限定 Task ID 和创建标识，读取时刷新旧 ORM 对象，不改变原记录、不决定重放动作；同一标识在其他 Task 下的 Run 不得混用。这些接口经真实隔离 PostgreSQL 验证，但创建 Run、修改 Task、判断是否能重放和提交事务仍由 RunCoordinator 负责，仓储本身不授予执行权。

Run 创建重放语义（2026-10-31 理解已验证，首次启动协调层已实现）：应先按 task_id/creation_idempotency_key 识别已经保存的创建结果，再对确实需要新建的请求检查首次启动资格，避免已创建后 Task 为 RUNNING 导致合法重发被误拒绝。创建标识绑定某一次创建请求；原 Run 后来 FAILED/PASSED，原标识仍关联原 Run，重发返回其读取时结果，不自动创建 Run2，不改变标识映射。失败后的新尝试需另经 Supervisor/Policy 恢复判断并使用新的创建标识；仅有新标识并不替代状态、预算或权限检查。返回已有结果不等于重新执行或授予新的执行权。首次启动请求的创建参数包含 worker_id；同一 task_id/creation_idempotency_key 下，worker_id 与原 Run 相同才可按原创建请求重放，worker_id 变化属于创建参数冲突，使用 RunCreationConflictError 表达，不覆盖原执行归属或租约。学生已正确指出冲突原因是 worker_id 改变，而不是重复提交本身。该异常与首次启动协调层逻辑已实现并测试。

首次领取候选查询（2026-10-31 已实现）：TaskRepository.get_next_eligible_for_update() -> Task | None 目前仅筛选 ELIGIBLE，按 created_at/id 稳定排序并 limit(1)，使用 with_for_update(skip_locked=True)，并通过 populate_existing=True 刷新可能存在的旧 ORM 对象。查询仅读取和锁住候选，不修改状态、不创建 Run、不提交事务；全部候选被其他事务锁住时也可返回 None，不能据此认定所有任务已结束。RETRYING 与恢复资格在恢复阶段接入，既定最终规则不变。已用真实 PostgreSQL 多 Session 验证跳过、无候选、提交/回滚释放及状态不变，指定 Task 与自动扫描首次启动的原子事务均已实现，Worker 已接入单次领取，持续处理循环尚未接入。

领取事务与执行权的边界（2026-10-31 理解已验证）：SELECT FOR UPDATE 的行锁保护短事务，提交或回滚即释放。领取成功提交后，已保存的 Run、worker_id 与有效租约继续作为事务外执行权依据；行锁释放不等于执行权消失。首次领取/创建 Run 的事务若回滚，新 Run 与租约没有持久化，Worker 不得凭内存中的 RUNNING/未来到期时间开始执行。该结论仅指本次首次创建回滚，不能混同已经提交的 Run 在另一事务失败后的状态。

Run 保留 `UNIQUE(task_id, attempt_number)`；以 `status = 'RUNNING'` 为条件，对 `task_id` 建部分唯一索引，保证一个 Task 最多一个活跃 Run。Run 创建请求保存稳定的 `creation_idempotency_key`，在同一 Task 范围内用唯一约束防止重发造成重复 Run。Task 创建 API 使用下面单独定义的提交幂等键，不能借用 Run 的键。

Task 提交幂等的已确认规则：随机唯一 Task.id 标识任务记录，不能识别同一次提交的重发。客户端应在首次请求发送前准备提交标识，网络重试沿用原标识；用户主动创建另一个任务使用新标识，即使 Issue 内容相同。同标识携带不同请求内容时报告冲突，不能静默返回原任务或创建另一条任务。提交标识与 Task.id 的对应关系须持久化，并与 Task 在同一事务保存，避免部分提交；业务层先查再写不能排除并发，数据库须通过唯一约束保证同一提交标识不能关联不同 Task。并发请求中一方因该唯一约束冲突回滚后，应读取已提交任务并核对内容；仅在标识与请求内容均相同时复用已有任务。目前创建请求的 `title`、`description`、`repo` 三个字段均纳入内容比较，Task 状态的生命周期变化不属于提交内容冲突。字段模型、迁移脚本、仓储、幂等服务及 HTTP API 已实现并经隔离 PostgreSQL 测试；2026-10-31 实际测试库 devflow_test 已升级并通过 HTTP 验收，开发库本次未升级。请求头按通用 UUID 校验，不额外限制版本；缺失或格式错误返回 HTTP 422，内容冲突返回 409，具体错误格式见下文。

进一步确认：提交标识作为 `tasks` 表的字段，与任务记录一起持久化，并由数据库对该标识实施唯一约束。新创建请求必须携带客户端预先准备的标识；漏传应明确报告缺失，不能由服务器临时生成并当作重试识别依据。迁移 `a3d12f2f7ab9` 先新增可空列，用 `gen_random_uuid()` 为旧行回填兼容标识，再启用非空和命名唯一约束，不设新任务服务端默认值；发生重复时约束拒绝而非接受重复。兼容标识并非旧客户端原始提交标识，不能恢复旧请求的重试关系。隔离 schema 的升级、降级及再升级测试已通过；2026-10-31 实际测试库已升级 a3d12f2f7ab9，四条原任务的既有字段不变且各获不同兼容 UUID；服务幂等与 HTTP API 已接入并在隔离 schema 验证。此迁移降级会删除该列及其中标识，无法恢复已建立的重试关系，不能对需要保留幂等记录的数据库随意降级。请求头使用通用 UUID 类型，具体错误响应见下文。

API 输入位置：HTTP 创建请求通过必填请求头 `Idempotency-Key` 携带提交标识，请求体继续描述 Issue；放在请求头本身不会自动完成幂等，客户端仍需在重试时复用同一值。学生认可此方案；接口已用 `Header(..., alias="Idempotency-Key")` 和 `UUID` 类型实现必填与格式校验，不额外限制 UUID 版本。缺失、空字符串或格式错误由 FastAPI 返回 HTTP 422，错误的 loc 指向 header/Idempotency-Key，且不调用 TaskService。内容冲突只捕获 `IdempotencyConflictError` 并返回 HTTP 409，响应为 `{"detail": "Idempotency key is already used for different task content"}`；其他异常不转换为内容冲突。OpenAPI 已声明 200/201/409，422 使用框架校验响应。

Issue 内容比较按字段名分别比较解析和校验后的三个字段值与已存 Task 首次创建内容；字段值中的空格应保留，不自动删除首尾空格，以免丢失代码缩进等内容。JSON 对象属性顺序不同、但字段值相同，不视为内容冲突。V1 Task 的 Issue 字段按创建时内容固定，不另外保存首次请求副本；未来允许编辑前须重新设计比较基准。服务已用三个字符串值逐项相等实现判断，并验证任一字段变化及正文尾部空格变化均触发业务冲突。

并发错误分类：只有 PostgreSQL 唯一约束违反（SQLSTATE `23505`）且诊断信息指向提交标识唯一约束 `uq_tasks_idempotency_key` 时，才进入该标识的重试核对；不能把所有 SQLAlchemy `IntegrityError` 视作重复提交。写入失败后须先回滚失败事务，再查询已有 Task；使用 `with session.begin()` 时，异常应离开事务块，使其回滚，再在外部识别和处理，避免在失败事务中继续查询。其他完整性错误保留原异常向外传播，例如 `title` 的非空约束错误可用诊断列名辅助排查。命名约束已在 ORM 与迁移脚本中定义；服务异常分类和回滚后查询已实现并经过实际并发测试。

提交标识采用 UUID 类型，客户端在首次发送前生成，重试复用该值；HTTP 请求头以文本传递并转换为 UUID，数据库列以 UUID 类型存储并受唯一约束保护。它与服务器创建任务时生成的 Task.id 是两个用途不同的字段。旧记录兼容 UUID 由迁移生成，不能恢复原客户端的历史重试关系。ORM 和迁移脚本已实现并做隔离测试，且实际测试库已于 2026-10-31 应用该迁移，服务幂等与 HTTP API 已适配；HTTP 使用通用 UUID 校验，格式错误响应为 422。

已确认的创建/重试响应语义：首次创建 Task 成功返回 HTTP 201；同标识同内容的重试复用已有 Task 时返回 HTTP 200。重试复用同一 Task ID，`status` 返回读取已有任务时的当前状态，而非固定返回创建时状态。任务状态可能在读取后再次变化；该约定不要求重试响应体与首次响应逐字相同。同一标识对应不同请求内容时返回 HTTP 409；服务冲突异常及 HTTP 状态码转换、错误响应均已实现并测试。

TaskService.create_task 已要求传入 `idempotency_key: UUID`，成功返回 `tuple[Task, bool]`，即 Task 与本次 `created` 标记；新建 True，复用 False。该标记只描述一次调用，不作为 Task 的持久化字段，也不为更新此标记而修改已有 Task。内容冲突抛服务业务异常 `IdempotencyConflictError`，服务不依赖 HTTP；API 已解包该元组，通过 `Response.status_code` 区分 201/200，并将业务冲突转换为 409。

服务实现先在 `with session.begin()` 内按标识查询；存在则比较原始三个 Issue 字段，复用时不重建 Policy、不更新 Task；不存在才构造 Policy 快照、创建并提交新 Task。指定唯一约束冲突必须在事务块外捕获，失败事务已回滚后用新事务读取并比较；如未能查到已有 Task，则继续抛出原数据库错误，不能伪造成功。其他完整性错误保留原异常。13 条服务集成测试在独立迁移 schema 验证上述行为，包含两个 Session 的实际竞争；测试修改状态只为模拟后续生命周期，不代表已实现 Supervisor 准入或 Run。

仓储已实现 `get_by_idempotency_key(idempotency_key: UUID) -> Task | None`：通过 `select(Task).where(Task.idempotency_key == idempotency_key)` 与 `Session.scalar()` 查询，不提交事务。已在隔离 schema 的真实 PostgreSQL 测试中核对回填标识能定位原任务、不存在返回 None；实际测试库 public schema 已升级，服务幂等与 HTTP API 已接入；显式加载 .env.test、使用真实数据库且不替换服务的 TestClient 验收通过，不能据此声称开发库已升级或已完成独立 Uvicorn/浏览器验收。

### 7.3 Agent 与工具的短事务

准入记录补充：`apply_admission_decision` 现在要求调用方提供 `AdmissionRecord`，包含非空理由及实际依据列表；状态即准入结论。依据列表允许为空，不得凭空补造。记录与状态及基准提交在同一条件更新及事务中写入；旧任务的记录可为空。服务不生成 Supervisor 判断，数据库迁移与集成验收以实际执行结果为准。

Agent 产物先写到唯一文件位置并计算哈希。Artifact、Handoff、Evidence 引用和 AgentExecution 完成状态在同一 PostgreSQL 事务提交；提交成功后才执行正式路由。数据库回滚无法撤销文件，未获正式记录确认的孤立文件不自动成为有效产物。

Coder 的 `PATCH_READY` 完成事务支持同时登记补丁 Artifact 和执行结果，要求产物 ID 与结果一致。数据库回滚后该文件可能仍存在，但没有正式 Artifact 记录。

ToolCall 使用两个短事务：先持久化稳定 ID、请求及初始状态；在事务外执行受控工具；再检查执行权，保存最终状态、stdout、stderr、退出码和产物引用。被拒绝的请求也记录，且按第 6 节口径计入工具预算。不能把数据库事务一直保持到 Shell 或模型调用结束。

Agent 完成与工具登记使用同一个 Run 行锁。Agent 仍有 `requested` 或 `running` 的工具调用时，完成请求退出事务并可在工具结果提交后重试。属于当前 Run 的 Agent 已经进入终态时，迟到的工具请求在登记事务中直接提交为 `denied`，原因是 `AGENT_EXECUTION_NOT_RUNNING`。

执行中进程崩溃时，记录无法证明命令是否启动或完成。租约失效后，相关 ToolCall 标记 `UNKNOWN_RESULT`，原因记为 `WORKER_LEASE_EXPIRED`；这两者不是新的 Run 失败类别。不能伪造退出码，也不能默认重放有副作用的命令。

RunCoordinator 可主动发现有未决 ToolCall 的过期 RUNNING Run。发现与标记在一个短事务中完成，使用行锁与 `skip_locked` 协调并发调用。此步骤只记录不确定的工具结果，结束旧 Run、FailureRecord 和恢复动作仍由恢复流程负责。

RunCoordinator 已具备过期 Run 的内部结束事务，在锁定 Run 并使用数据库时间确认租约到期后，同一事务标记未决工具、结束 Run 并插入 FailureRecord。公开入口 `close_expired_run` 现接受调用方提供的决策并先做策略复核：过期 Run 无可靠归因，因此只允许 `UNKNOWN_FAILURE`，且只允许再次诊断或升级人工，具体重试/补丁/环境动作需要本次尝试无法产生的证据；`REDIAGNOSE` 时在同一事务内从持久历史复核重新诊断额度、同指纹次数与计划可用性，决策不能自行授予额度。重复调用因 Run 已非 RUNNING 而抛 `RunLeaseNotExpiredError`，不写第二条 FailureRecord。另提供 `close_expired_run_with_derived_decision`：决策由系统在同一 Run/Task 行锁事务内按持久历史与 Policy 生成，调用方无法传入结论换取额度。分类固定 `UNKNOWN_FAILURE`——租约过期只证明 Worker 未续约，不能证明根因是环境还是任务；同一 Task 的每次过期共用签名 `lease-expired:v1`，它表示事件类别而非相同根因，因此只作保守的预算控制：重复过期会触达同指纹上限并转为升级人工，Run ID 仅作为记录身份单独保存。重新诊断启动历史不可判定或存在无 FailureRecord 的失败 Run 时，直接升级人工而不消耗额度。两个入口都在同一事务内按与其他失败提交相同的规则更新 Task 状态：`REDIAGNOSE` 置 `RETRYING` 以允许后续恢复 Run 启动，其余置 `ESCALATED`；两者都仍不启动新 Run，恢复 Run 的创建仍由调用方另一次幂等请求完成。关闭同时写入一条 `result = failed` 的 Verification，因为恢复入口要求上一个 Run 存在失败验收记录；该记录 `evidence.scope` 为 `no_verification`、`reason` 为 `worker_lease_expired`，它记录的是"没有验收证据"而不是"验收失败"，读取方必须先看 `scope` 再决定是否当作裁决，不得只凭 `result` 推断执行结果。Worker 侧另有 `close_expired_run_and_recover` 把关闭与恢复串起来：先由协调器派生决策并关闭，`REDIAGNOSE` 时以调用方提供的创建幂等键启动恢复 Run，`ESCALATE_TO_HUMAN` 时直接返回、不启动任何东西。关闭与启动分属两个事务——决策与关闭同一事务，恢复 Run 由既有幂等创建入口在另一事务建立——崩溃后重放只会重读已提交的 FailureRecord（不再关闭一次），并由创建键返回已存在的 Run。另有 `take_over_expired_run`：租约过期只证明旧 worker 不再续约，不证明它的进程已退出，因此接管前必须先真正取得该 Run 的工作区 advisory lock（活的旧操作会占着它），并确认工作区没有未决的写类 ToolCall（`git_clone`/`git_checkout`/`apply_patch`/`run_command` 处于 requested、running 或 unknown）；任一检查不通过即抛错，不猜。关闭与接管都还要求该 Run 的 Task 仍是 `RUNNING`：Run 已过期但 Task 已被别的路径推进到受理、拒绝、升级或完成时，这条 Run 不再描述当前尝试，用它的结果改写 Task 状态会覆盖更新的状态，因此一律拒绝并保持原状。通过后才在 Run 行锁内、按数据库时间把执行权转给新 worker，提交前再声明 advisory lock 仍被持有；同时把无法归因的未决工具记为 `UNKNOWN_RESULT`，并报告仍停留在 RUNNING 的孤儿执行。半途节点不重写也不重派：`agent_executions` 上有 `handoff_id` 唯一约束，一个 Handoff 只能对应一条执行，而所有重放查询都靠这对身份定位，允许第二条会使"哪条执行产生了这份证据"不可判定。因此同一 Run 的续跑只适用于崩溃发生在派发之前、没有孤儿执行的情形，由 `take_over_expired_run_and_resume` 处理；存在孤儿执行时该入口只返回 `blocked_by_orphaned_nodes`，既不重写节点也不自行关闭 Run，关闭仍由调用方走既有路径完成。新租约不超过 `started_at + max_run_duration_seconds`，接管不能换取超出 Policy 的实际时长；两个接管者并发时只有一个成功，另一个因租约已有效而被拒。自动侧由 `Worker.reclaim_next_expired_run` 提供扫描入口：先用 `find_next_expired_run_id`（行锁 + `skip_locked`，`skip_locked` 让并发回收者取到不同的 Run）取一条过期 RUNNING Run，再走关闭加恢复路径；自动路径一律不接管同一 Run，因为无法判定孤儿节点能否重放。恢复 Run 的创建键由过期 Run 派生（`expired-close:<run_id>`），重复扫描不会建立第二个恢复 Run；没有过期 Run 时返回 `idle`。

### 7.4 worker 租约与恢复

协调层/Worker 的租约参数暂定初值（2026-10-31）：heartbeat_interval_seconds=10、lease_duration_seconds=30，均为正整数秒，租约时长应严格大于心跳间隔并留有延迟余量。该初值根据当前教学场景提出，尚无实际 worker/故障注入校准，不把测试夹具五分钟沿用为运行默认。租约过短可能在正常下一次心跳之前失效，过长会延后崩溃后的失效判断；实际恢复还取决于扫描、停止和恢复流程，不保证租约一到即恢复。有效心跳只续租，不重置 started_at；本次 Run 仍受 Task 固定 Policy 中 max_run_duration_seconds 的硬上限约束，不能通过续约无限运行。这些运行参数不修改既定八项预算或已有 Task 快照。独立配置模型 src/devflow_ai/config.py::LeaseConfig 已实现：两个字段使用严格正整数秒，默认 10/30，通过 ConfigDict(extra="forbid") 拒绝未知字段；validate_lease_window 使用 model_validator(mode="after") 校验 lease_duration_seconds 严格大于 heartbeat_interval_seconds，失败由 Pydantic 报 ValidationError，成功返回配置对象。拒绝布尔值、小数、字符串和 None，不在这里解析环境变量或修改数据库。配置校验已测试，并接入首次启动和续约协调层；续约方法已于2026-11-01通过测试，独立心跳调度组件见下文，实际Worker运行集成尚未接入。

时间源规则（2026-10-31 理解已验证）：创建 Run 的启动、初始心跳和租约计算统一依据数据库时间，后续执行权有效性也按数据库时间判断，避免混入各 Worker 本机时钟偏差。UTC 统一时间表示，不保证不同机器时钟相同；Worker 本机时间不作为租约授权真相。已实现 database.get_database_time(Session)：查询 PostgreSQL clock_timestamp() 并转换为 UTC，在协调层同一事务里供启动/初始心跳/租约计算复用。clock_timestamp() 返回调用时的当前时间，now() 固定为事务开始时间；选择依据见 [PostgreSQL 17 官方时间函数文档](https://www.postgresql.org/docs/17/functions-datetime.html)。字段原有 now() 插入默认保留，协调器显式提供启动与心跳时间。已接入续约方法并实现独立周期心跳组件，实际Worker处理器及工具前/结果提交的运行时执行权检查仍待接入。

Run 保存 `worker_id`、`last_heartbeat_at` 和 `lease_expires_at`。worker 在执行工具前检查执行权，在提交结果的事务中再次确认 Run 仍为 `RUNNING`、worker 身份匹配且租约有效。旧 worker 的迟到写入不能覆盖终态 Run 或新 Run。

心跳续约边界与异常契约（2026-11-01，理解已核对，已实现并测试）：协调层方法为 RunCoordinator.renew_lease。不能先查到有效执行权，稍后再无条件更新；续约资格必须在实际更新时原子校验，包含目标 Run、RUNNING 状态、worker 身份、按数据库时间判断的有效租约及 Run 总时长边界。续约更新 last_heartbeat_at 和 lease_expires_at，不自行改变 Run 状态、worker_id 或 started_at，到期时间仍不得超过固定 started_at 加 Task Policy 的 Run 总时长上限。正常执行但影响 0 条记录，抛业务异常 RunExecutionDeniedError，表示本次续约未获执行许可；数据库连接或执行异常保留 SQLAlchemyError 向上传递，不伪装成执行权条件拒绝。两种情况均不能被 Worker 当作续约成功或继续执行的许可；异常类型不同用于保留准确诊断原因，不代表数据库故障时可以继续执行。额外查询可用于诊断，不替代执行权确认，也不授权 Worker 自行改失败或创建恢复 Run。学生已正确复述续约更新字段、未成功应停止继续执行，以及混淆两类异常造成的误判；不重问这些基础边界。成功回执与实现、验证证据见下文。

续约加锁与取时顺序（2026-11-01，理解已核对，加锁仓储及续约协调层已实现并测试）：协调层在同一短事务中先锁住目标 Run、读取其当前值，再读取数据库当前时间，用于判断租约和 Run 总时长资格。等待行锁期间租约可能到期，不能沿用等锁之前获取的时间，也不能仅因状态仍为 RUNNING 就续活过期租约。读取数据库时间不会暂停租约倒计时。学生已正确复述先取锁、再查看数据库时间，超过租约应拒绝续约。RunRepository.get_by_id_for_update(run_id) -> Run | None 已实现，只负责指定 Run 的加锁刷新查询，使用普通 FOR UPDATE 等待锁与 populate_existing=True 刷新，不在仓储中取时、提交或作续约业务判断；协调层保有事务边界。六个隔离 PostgreSQL 加锁场景覆盖字段不变/缺失、旧对象刷新/删除、调用方提交或回滚才释放锁，以及通过 pg_blocking_pids 确认实际等待后读到另一事务提交的 FAILED；该阶段相关测试曾为79 passed in5.58s。续约及独立心跳组件的后续验证见下文，实际Worker处理循环和崩溃恢复仍未实现。

续约实现与验证（2026-11-01）：RunCoordinator.renew_lease(run_id,worker_id) 使用调用方 Session 的短事务先锁定并刷新 Run，读取 Task 固定 Policy 并校验版本，锁后取得数据库时间，检查 owner/状态/租约及固定总时长。RunRepository.renew_lease_if_owned 在实际 UPDATE 中再次以 clock_timestamp 检查有效期，避免取时后暂停造成过期续活，并用数据库时间计算心跳与 min(当前时间加租约窗口,固定 started_at 加总时长)；RETURNING 两个时间，0 条由协调层映射 RunExecutionDeniedError。成功返回独立冻结的 LeaseRenewal(run_id,worker_id,last_heartbeat_at,lease_expires_at)，只在提交成功后返回，不作为永久执行权凭证；数据库异常原样向上传递。tests/test_lease_renewal_integration.py 新增26项场景，覆盖提交后独立回执、状态/归属/时效/总时长拒绝、精确边界、固定Policy、异常分类、更新后/提交前失败回滚、UPDATE重新检查时效、真实等待锁后判断时间/终态及同owner并发。Docker环境恢复后完整测试232 passed/1条既有依赖警告 in17.91s、退出0，包含上述场景。只读核验devflow_test/public Task4/Run0、版本b312290a866d、无临时测试schema。尚未接实际Worker心跳/处理器、工具前/结果提交的运行时执行权检查及恢复，不将续约集成测试当真实Issue修复或崩溃恢复证据。

Worker 心跳调度理解（2026-11-01，理解已核对，独立组件已实现）：首版采用独立后台心跳线程调度续约，与调用Agent/执行工具的主流程分开，避免长时间等待结果阻塞心跳。学生已正确复述独立调度目的及Session隔离目的。主流程与心跳各用自己的Session，共享session_factory/Engine；心跳每轮由工厂创建Session并关闭。独立调度不改变续约资格和Run总时长上限。

2026-11-18 心跳与正式终态：心跳续租遇到 `RunExecutionDeniedError` 时，先读取该 Run 的当前状态与归属——**只有「状态为 PASSED 且 `worker_id` 仍是本执行」**（PASSED 只能由持有执行权的正式事务写入，接管会改变 worker_id）才按「本次执行已完成」处理：心跳安静退出、不记录失败、也不置执行停止事件；租约过期、归属变更（包括别的 Worker 接管后把 Run 做成 PASSED）、Run 被别处关闭（FAILED）等其它失权仍是失败，状态读取本身失败同样按失败处理。PASSED 只能由仍持有执行权的一方写入、而接管只发生在 RUNNING 且租约过期时，因此「状态 + 归属」足以区分本次执行自己的提交与别人的提交。这样正式提交后紧接着的心跳续租不会把正确结果改写成错误，也不会吞掉真正的失权。

RunHeartbeat已实现，实际Worker处理器/CLI尚未接入。

心跳失败通知（2026-11-01，理解已核对，组件通知已实现，Harness/结果集成未实现）：后台异常不会自动停止主流程。RunHeartbeat保留原异常并设置execution_stop_event，主流程须停止启动新Agent/工具步骤；已启动工具由Harness停止清理，信号或停止等待不等于进程已结束。迟到结果在正式保存事务中再次检查数据库执行权，不能凭旧授权提交。心跳不自行改Run终态或创建恢复Run，也不将数据库故障包装成业务拒绝。学生已正确复述这些行为；当前仅有心跳组件通知与原因传播，尚未有实际处理器/Harness进程控制/结果事务联动。

心跳线程收尾与信号（2026-11-01，关键行为理解已核对，独立组件已实现并测试）：生命周期绑定本次Run的Worker处理作用域；主流程finally通知心跳停止并等待实际结束，覆盖正常与异常退出。heartbeat_stop_event请求停止心跳，execution_stop_event通知主流程停止执行，避免正常收尾被误判失败；学生已正确区分方向及误判风险。Event.set不会强制中断数据库操作，数据库SQL与主流程join均设有界等待；join超时仍活须抛HeartbeatShutdownError，调用方不得宣布清理完成或接下一个Run。学生明确活线程不能视为清理完成，不重问。线程停止不决定Run终态或恢复路线。

心跳组件实现与证据（2026-11-01）：src/devflow_ai/heartbeat.py::RunHeartbeat提供start/stop、is_alive、raise_if_failed、failure/last_renewal及上下文管理；实例只用一次，Event.wait按LeaseConfig间隔调度，独立Session调用既有renew_lease，回执在锁保护下保存。HeartbeatConfig位于config.py，statement_timeout_seconds/shutdown_timeout_seconds均为必须显式提供的严格正整数，拒绝缺失/未知字段，无新生产默认值，不进入Task Policy。Session after_begin用事务局部set_config设置statement_timeout，包含SQL和锁等待；这不是连接池/网络全链路总超时，既有Engine connect_timeout=5保持不变。stop设置停止信号后有界join，检查仍活则报错并设置执行停止；即使稍后线程退出，原收尾失败仍通过raise_if_failed保留。daemon仅允许宿主进程退出兜底，不证明线程/资源已清理。后台原异常保留，日志仅安全类型和Run/Worker关联；with正常退出检查后台失败，主流程已有异常不被普通后台异常覆盖。tests/test_heartbeat.py新增25项、test_heartbeat_integration.py新增6项，含真实后台续约/Session事务隔离/SQL超时与真实锁阻塞后的收尾超时，全量263 passed/1条既有警告 in27.91s。测试短时值不是生产初值。实际Worker处理器/CLI、Harness进程取消、工具与正式结果事务执行权检查尚未接入。

租约到期后，恢复流程结束旧 Run 并保存 FailureRecord，再由 Policy 判断创建新 Run 或升级人工。数据库租约本身不能停止操作系统进程；Harness 还需监控和清理已启动的子进程。先以单 worker 打通链路，随后通过故障注入验证租约、心跳、多 worker 竞争和旧 worker 拒写；未经测试不得声称具备崩溃恢复能力。

### 7.5 Run 工作区、ContextBuilder 与循环

每个 Run 使用独立工作区、临时目录、缓存和测试输出，从 Task 固定的基准 commit 建立干净副本。上一 Run 的正式历史可以作为证据引用；残留工作区不自动继承。复用旧补丁须有明确恢复计划，校验 Artifact 哈希，通过 Harness 在新工作区应用并留下新审计记录。

Worker 使用分别配置的工作区根目录和产物根目录，在两者下按 Run ID 生成子目录。两个根目录解析后的实际路径必须分离，Run 子目录不能通过链接重定向到其他位置。清理仓库副本时，正式 Artifact 保存在独立的产物目录。

ContextBuilder 根据 task_id、run_id 和目标角色，从正式记录中筛选、排序、裁剪上下文，并为事实附来源。公共信息包括 Issue、验收标准、基准 commit、授权范围、Policy 快照和剩余额度；专业 Agent 只获得各自工作所需证据，Supervisor 获得正式完成情况、失败历史及 Policy 判断。完整日志留在 Artifact，模型上下文使用必要摘要和引用；隐藏验收源码及参考修复不进入修复 Agent 上下文。

首次构建 Planner 的探索上下文时，使用调度方指定的、同一 Run 内已成功提交的 Explorer 执行。上下文单独提供探索报告与正式引用的读取证据，证据范围来自 ToolCall 审计。缺少影响方案选择的事实时，Planner 在交接中指出需要补查的问题及受影响的决策，由 Supervisor 决定是否安排补查。

Worker 在登记 Planner 的 AgentExecution 前构建探索上下文。若指定的 Explorer 执行无法提供上下文，Worker 拦截调用，本次不产生 Planner 执行记录。

Planner 的模型输入从当前 Run 对应的 Task 读取 Issue 标题、正文及固定的基准提交，并与正式探索上下文一并保存。默认提示词包含角色约束和输出契约，修复提示词沿用同一份输入并附带无效输出和校验反馈。

Coder 交接给 Planner 重新规划时，Handoff 的来源指向 Coder 执行，`evidence_refs` 显式引用同一 Run 中已成功提交的 `explorer_execution_id`。Planner 同时接收 Coder 的交接上下文与所引用的探索结果。引用缺失或无效时停止启动，等待补充证据。重新规划的 Planner 结果提交后，恢复链路按 Planner 执行身份再执行一次现有 Planner 后 Supervisor 路由，保存路由与 Handoff：`CODE` Handoff 交给现有 Coder 接收器执行一次，`BASELINE` Handoff 经可信 Verifier 接收器记录一次基线，其他路由只保存后停止。基线记录并校验后，同一个重新规划 Planner 再执行一次基线后 Supervisor 路由：`CODE` 派发一次 Coder，其提交结果继续进入 Coder 后 Supervisor 路由，`VERIFY` 时经既有补丁后 Verifier 生成公开测试与语义草稿并保存，不写正式 Verification、不改 Run 终态；该 Coder 的 Supervisor 请求 `REPLAN` 时可以再启动一轮 Planner，失败循环不新增专用开关，而由既有图步数与工具调用预算约束；同一 Run 内已有非拒绝补丁调用时，下一轮可信基线会被既有未打补丁检查拒绝，循环据此终止，不重置也不清理工作区；再次要求 `BASELINE` 只报告基线已记录，不重跑基线也不启动 Coder；缺少 Supervisor 或 Coder 配置、缺少可信 Verifier 配置、Supervisor 运行中或失败、`BLOCKED` 与其他路由均安全停止。重放复用同一 Supervisor 与已提交结果，运行中或失败不重复执行。同一个重新规划 Planner 可以先后产生基线前与基线后的 Supervisor 执行，恢复链路按 Supervisor 输入里记录的 `observed_baseline_verifier_id` 与 `baseline_available` 判定它属于哪一段，基线前的路由不会把基线后的决策当作重新规划决定；缺少该标记的旧行回落到创建时间比较，与基线同一时刻、或两条候选同一时刻时不得猜测，直接判为身份不可区分并停止；没有已提交基线时两者是同一路由。已存在基线执行但其输出不是 `BASELINE_RECORDED`（没有记录到基线，例如报告未被捕获）报 `baseline_evidence_unavailable`；记录到基线但报告不能再核验报 `baseline_evidence_invalid`；两者都不得启动 Coder、写正式 Verification 或清理工作区。若某条 Supervisor 的 `observed_baseline_verifier_id`/`baseline_available` 指向的基线不再具备 `BASELINE_RECORDED`，那是「历史证据自相矛盾」而不是「基线无效」，按身份不可区分直接报错停止。

区分三种循环：单个 AgentExecution 内的工具循环、当前 Run 内 Supervisor 通过 LangGraph 的角色调度、旧 Run 失败后的跨 Run 恢复。正式角色交接须先持久化，再继续路由。

Git 工作区和命令白名单提供受控操作边界，但不能隔离任意 Python 程序的全部能力。V1 只处理经人工准入的可信仓库和受控测试环境；数据库或应用容器化不等于工具执行沙箱。

修改前基线：新增接收已批准 Verifier Handoff 的单次基线执行入口，以服务端注册的 pytest 命令运行并保存报告。该入口在工作区操作锁内拒绝同 Run 中已有非拒绝补丁调用的情况；测试断言失败并不等于 Verifier 执行失败，但需取得报告并进一步判断失败是否对应 Issue。此项检查不能单独证明工作区与固定提交完全一致，也不能发现所有外部修改。Supervisor 单次路由可安排基线；Coder 启动前独立复核基线引用、来源、报告哈希和已记录操作的顺序。

Planner 后的 Supervisor 路由现支持安排 BASELINE：若决定进入 Coder 但当前 Planner 对应的已提交基线缺失，先交接给 Verifier；缺少 Verifier 命令权限时阻塞。基线完成后再判断时，交给 Coder 的 Handoff 引用该基线 Verifier 执行。Graph 接收器可绑定可信注册的基线测试命令。Coder 启动前验证同 Run、同 Planner 的 Supervisor 授权基线，重新读取并校验报告与哈希，拒绝基线之后已记录的其他工作区修改类调用；这次检查不覆盖随后发生的外部工作区变动或所有未记录操作。持续自动循环和正式验收仍未完成。

Worker 的 Coder 路径在提交补丁请求时，Harness 持有工作区锁并再次核验基线报告及已记录操作，避免仅依赖模型运行前的检查；该锁不约束工作区外部进程。当前低层 `RunCoordinator` 补丁接口在未传入基线 Artifact 根目录时仍可跳过此复核，历史调用与 Artifact 补丁入口尚未统一，不能将此机制声称为覆盖所有补丁入口的硬约束。

## 8. Benchmark 与任务包

Click 为首选目标仓库，Flask 为备选。首个候选 Click #3019 的公开问题描述已经核对：`click.prompt` 与 `click.confirm` 的空 `prompt_suffix` 会多出空格。候选基准为 Click 8.3.0 的 `00fadb8904387158ce6e9aa1573be770446895c1`。2026-11-11 以真实 deepseek-flash、隔离数据库和可信控制端完成一次真实任务尝试：公开基线 2 失败/2 通过，补丁后 4 通过，语义审查与经 8.3.1 参考包校准的 8 项独立用例通过，正式结果为 PASSED/AWAITING_REVIEW。此为一次有限用例下的尝试，不表示已合并或总体成功率；测试 schema 清理后保留导出证据，具体范围和限制见 PROGRESS。先完成真实任务的人工审核与评测记录，再扩展任务集；测试替身只用于验证系统链路，不计真实修复成功。

每个任务包保存版本、许可证与来源、修复前 base commit 完整哈希、运行环境和依赖、净化后的问题描述、可观察错误与正确行为、允许和禁止的修改范围、公开测试命令、重复执行的基准结果、Policy 与模型配置，以及评测器掌握的隐藏验收版本或哈希。未核验的值标为待核验，不补造。这些字段现由 `devflow_ai.task_pack` 的 Pydantic 清单（`benchmarks/<pack_id>/task.json`）承载：公开检查除命令、命令名与目标测试外，还可声明 `pythonpath_relative`（相对被 pin 的 checkout），用于"必须导入该 checkout 而不是已安装版本"的情形，`to_verification_config` 会原样传入 `VerificationConfig.pythonpath_relative`，由 harness 校验为相对路径并据此设置 PYTHONPATH。注册命令的解释器由可信配置的 `python_executable` 指定，必须是绝对路径（Windows 还需 `.exe`），装配后的命令与基线、补丁后验证、正式验收共用同一份 `VerificationConfig.command`，因此三处命令证据一致，Agent 不能指定解释器；命令错误的终态只能由**进程创建边界**提供证据：只有负责创建子进程的那段代码能在失败时证明"没有进程存在"，它在错误对象上留下未启动标记（`devflow_ai.process_evidence`），被标记的调用记为 `FAILED`（原因前缀 `COMMAND_START_FAILED:`）；其余任何错误——包括同一类型的异常、超时与管理进程异常——一律记为 `UNKNOWN_RESULT`（前缀 `COMMAND_RESULT_UNKNOWN:`），因为此时无法证明它没有执行过，也不能凭异常类型推断。两者都不留在 `REQUESTED`；记录失败本身不得覆盖原始异常，随后 `finish_agent_execution` 也不会再因待决调用抛出二次异常。公开测试文件可以与 checkout 分离：`VerificationConfig.public_tests_root` 由可信调用方给出任务包目录时，Worker 把该目录下的 `*.py` 复制到本 Run 的 artifact 目录 `public_tests/`，把注册命令中指向测试文件的参数替换为副本路径，并追加 `--rootdir` 与 `--confcutdir`；因此受控命令在 checkout 内运行时也能找到公开测试，checkout 本身不被改动（基线工作区完整性检查仍然有效），节点名保持相对 rootdir 的稳定形式，且不加载副本之上的任何配置。装配本身是由可信配置推导的**确定性函数**（`verification_config.public_check_command`），执行侧与正式验收侧调用同一函数，不允许各自拼装：执行时在该 ToolCall 参数中记录装配后命令的 `command_sha256`（与 `pythonpath_relative` 一并摘要）以及副本逐文件的 `public_tests_sha256`；正式验收（就绪检查与三个终局入口）用同一函数从配置与本次 Run 的 artifact 目录重算这两个值并与记录比较，同时核对磁盘上的副本仍与任务包逐文件一致。这样"实际执行了哪条命令"与"执行的是哪份测试内容"都被钉住，基线、补丁后验证与正式验收共用同一份证据，公开测试位于 checkout 之外也不再使证据无法核对。局限须如实说明：摘要覆盖的是声明的命令本身，pytest 报告路径由 `--junitxml` 在摘要之后追加，不进入摘要，改由"报告 artifact 必须归属同一 ToolCall"约束；另需注意 `public_tests_root` 指向的目录若在运行与验收之间被改动，重算出的命令或内容摘要会随之改变并导致验收拒绝，这是预期行为，不是配置错误；`extra="forbid"` 只能拒绝未定义字段，无法阻止把隐藏内容写进 `notes` 这类自由文本字段，因此它不构成隐藏材料的隔离措施；隔离由可信控制端保管并注入材料来保证。隐藏材料的引用只允许独立套件版本、`sha256`（须同时写明被摘要的具体字节与计算口径）以及"由可信控制端提供"的声明，且摘要与声明只是标识，不替代来源与隔离核验。清单里每个尚未确定的部分都必须在 `unverified` 中写明字段与原因，否则校验失败；外部连接声明默认 `unknown`，只有可信调用方核验后声明 `none` 才可用于正式评测，`to_verification_config` 原样传递该声明，因此 `unknown` 无法通过正式评测的准入——这是预期结果，不是待填的配置。校验通过只表示格式与可核验字段符合要求，不代表任务、环境或隐藏材料已被信任。命令行 `python -m devflow_ai.task_pack <包目录>` 输出 JSON 报告，结构不合法退出 1。每次运行另写一份运行记录（`devflow_ai.benchmark_record.BenchmarkRunRecord`）：包标识、base commit、尝试序号、尝试类别（`attempt_kind`）、起止时间、模型与 Policy 版本、四态结果（`formal_pass`/`public_only_pass`/`failed`/`timeout`）或显式的 `incomplete`（运行仍在进行、被阻塞、或执行被中断——三者都不得记为失败）。四种标签各有自己的证据要求，任何一条都不得顶替另一条：`public_only_pass` 需要**已核对为通过**的公开结果——用 checkpoint 草稿里的**测试比较与语义审查重算**，且必须与 checkpoint 记录的一致，只把草稿文件读回来不算核对过（记录 `verified_outcome`、Verifier 执行 id 与草稿 id）；同时本次尝试已结束，独立评测**实际被尝试过**、回执可读回、套件版本等于清单固定版本、且回执指向同一份草稿（记录 `independent_evaluation.verified=true`），结果为 `unavailable` **并写明原因**；`failed` 需要独立评测**实际执行且通过校准**、回执能从磁盘读回并显示失败，记录里同时保留“公开检查通过”与“独立评测失败”两侧证据，不得只留一个笼统失败标签；`incomplete` 表示仍在进行、独立评测**没有被尝试**（skipped、或在可信套件运行前就停下）、回执缺失或无法核验、套件版本未固定或不一致、回执指向别的草稿，或公开/独立两侧中任一侧缺少可核对证据——每种情况都保留具体原因（例如 `public_passed_but_evaluation_not_attempted`、`public_passed_but_receipt_unverifiable`）；`timeout` 按既定超时规则记录，不因公开测试曾经通过而被覆盖。正式通过时，公开与独立两份摘要**从该 Verification 引用的草稿与回执重建**：正式事务会把 Verifier 的检查点输出替换成裁决，因此不能再去读 checkpoint，也不能因为标签是 `formal_pass` 就把字段补成“通过”；若草稿已不再重算出 passed，只留说明而不填 `verified_outcome`。记录还支持**可追溯修正副本**：`correction` 块用 sha256 分别指向原记录与人工审核副本，并写明修正依据、执行人与 UTC 时间；修正副本由 `python -m devflow_ai.benchmark_record correct` 生成，只替换派生摘要、保留原有人工结论、绝不改写原记录或审核副本，也不允许借修正改变结论；记录带显式 `schema_version`：新记录写 2；缺失时按版本 1 读取（旧字段含义不变，版本 2 才定义的字段缺失即用默认，**不隐式升级**），高于本代码认识的版本直接拒绝读取。一次尝试可能同时存在原记录、人工审核副本与修正副本三份文件，汇总按 `run_id`（缺省用 pack/base/attempt/started_at）归组，每组默认只计一份：先排除被 `correction` 取代的，再优先修正副本，其次带人工结论的副本；被弃的计入 `superseded_excluded`，同一尝试同时存在多份修正或无法排序时报 `conflicts` 并**不计入样本**（`--include-superseded` 才全部计入）。独立评测证据记在记录的新字段 `independent_evaluation` 上（`outcome`、`calibrated`、`receipt_artifact_id`、`suite_version`、`reason`），`unavailable` 必须带原因，未校准的独立失败不算裁决。、公开检查计数、正式验证引用（验证 id、独立套件版本、回执摘要）、人工审核结论与者、artifact 位置、操作者；记录文件以排他创建写入、拒绝覆盖。只有"正式通过且人工审核通过"才计为已解决（`counts_as_solved`），且判定 `formal_pass` 必须先核对完整证据链——结果为通过、`evidence.scope` 为 `trusted_v1`、草稿与独立评测回执齐备、`suite_version` 与任务包一致，并且这两份 artifact 必须能用既有加载器从磁盘读回（校验归属到同一 Verifier 与 Run、按记录哈希重读字节，回执还须指向同一草稿且结果为通过），任一项不满足只能记为 `incomplete` 并保留具体原因，不得仅凭任意一条通过记录下结论；审核结论为 `accepted` 时必须同时记录审核人与审核时间，否则记录无效；记录中的正式验证引用仍只是记录方的声明，统计前需由可信流程核对对应的正式验收结果与回执。正式通过待审与被审核否决分别单独统计，汇总必须同时报出样本数与 `solved/sample`。原始运行证据以记录与 artifact 为准，`docs/PROGRESS.md` 只写摘要并引用记录路径。首轮准备与公开基线各有一个不写证据的入口：`python -m devflow_ai.preflight` 先核验 Policy 快照、解释器、checkout、模型配置是否就位（只报存在与否，不读也不打印值）、Docker 守护进程是否可应答（不启动容器）、可选的只读数据库探测，再按真实 Run 的同一装配方式跑一次公开检查，并用正式验收同一个 JUnit 解析器读结果。几条边界必须如实理解：（1）`--import-name` 的归属判定在**子进程内**用实路径比较完成（大小写、分隔符与目录联接按实际平台解析），并回报实际导入位置与搜索路径，避免父进程重演路径语义造成误判；（2）`command_sha256` 覆盖装配后的命令，其中含本次 Run 的副本绝对路径，因此**只在同一 artifact 根内可比**，跨运行能比的只有逐文件内容摘要 `public_tests_sha256`；两者都随结果输出，但各有明确范围说明；（3）公开检查的子进程在启动前移除名字含 `API_KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL` 的环境变量（只处理名字、不读值、不记值），子进程输出默认**不**随结果返回，只有显式 `--show-output` 才带输出尾部，避免把凭据或敏感信息带进日志；（4）`--material` 与 `preflight --material` 都是**先算文件原始字节的 SHA-256、再与清单 `materials_sha256` 比对**，通过后才解析内容：清单未固定摘要（如 `independent_suite` 为空）或摘要不一致一律拒绝，绝不降级成“任何材料都行”；通过后只核对身份（摘要只标识被固定的字节，不证明来源、隔离或用例质量；“缺陷版本必须失败、参考版本必须通过”只能由正式评测的 `calibrate_stdio_suite` 实际跑出来，preflight 明确不做也不宣称）——套件版本与清单一致、两个校准工作区存在且互不相同、corrected 工作区的 HEAD 等于声明 commit、候选镜像是否已在本机（只做 `docker image inspect`，从不拉取）、用例条数——用例字节经同一个严格加载器读入但从不回显、进入日志或记录；（5）数据库探测只报告“能连上、库名、当前 schema 里有多少张表、记录的 Alembic revision 与本次 checkout 期望的 head 是否一致”，revision 与 head 不一致时直接判失败（避免在代码不认识的 schema 上跑）；但它**不**据此宣称 schema 完整、与其他数据隔离或连接指向符合预期，这些是操作者的核验项，不是该探测的结论；探测本身不迁移任何数据库。该入口不创建 Task/Run/ToolCall/Verification、不调用模型、不迁移数据库，因此它的通过只证明“任务包、解释器、checkout 与装配彼此一致”，不能替代真实 Run 自己记录的补丁前失败基线；它给出的网络线索只是静态观察，不能把 `external_connections` 从 `unknown` 改成任何值。这些步骤各有命令行入口：`python -m devflow_ai.benchmark_run` 装配可信配置、按固定限额领取并驱动**一次**尝试、写运行记录（一次调用即一次尝试，没有重试参数；`--confirm-run` 是对“本次将花掉预算”的显式确认，`--policy` 要求 Task 里实际保存的快照与给定文件逐节一致（`budget`/`permissions`/`policy_version` 任一不同即拒绝，避免用一份被审过的文件去描述另一份实际生效的策略）；退出码 0 表示记录已写出、2 表示配置不可用且未开始、1 表示运行期失败且记录通常已写出）；人工审核现在有两层，二者不能互相替代：**记录层**由 `benchmark_record review` 把结论写进**新文件**（原记录不改，`accepted` 必须带审核人与时间）；**数据库层**由 `python -m devflow_ai.task_review --record <记录> --reviewer <姓名>` 在**同一事务**里写入一条追加式 `task_reviews` 行（绑定 Task、Run、正式 Verification 与记录 sha256）并把 `Task.status` 从 `AWAITING_REVIEW` 改为 `COMPLETED`（accepted）或 `REJECTED`（rejected）。证据校验：Run 必须属于该 Task 且 PASSED、Verification 必须属于该 Run、结果为 passed 且 `evidence.scope=trusted_v1`、记录声明的 `formal.independent_suite_version` 与 `Verification.evidence.suite_version` 必须都存在且相等（任一缺失或不一致即拒绝；这条检查在幂等分支之前执行，重放同样绕不过去）、记录的 reviewer 与提交值一致且非空、`reviewed_at` 非空；任一不符即拒绝且不写任何行。幂等以数据库唯一约束 `(task_id, record_sha256)` 为准，但**只比摘要和结论是不够的**：同一摘要下 Run、Verification、审核人、审核时间或结论任一与既有评审不同，都属于“同一份记录、不同的审核内容”，一律冲突拒绝；完全一致才返回既有评审。绑定的 id 方面，命令行给的 `--task-id`/`--run-id`/`--verification-id` 只能**补记录里缺的值**，与记录里已有的值不一致就拒绝——不能让调用方悄悄替换早已绑定的证据。记录只读一次：解析与 sha256 取自同一份字节，避免文件在两次读取之间被换掉导致“摘要是一份、解析的是另一份”；V1 不支持改判、历史回填或自动审核。`reviewed_at` 取自记录（真人时间），`created_at` 取入库时的数据库时间，两者不互相覆盖。人工审核由 `python -m devflow_ai.benchmark_record review` 写到**新文件**，原记录是证据、绝不改写，`accepted` 必须同时给出审核人与审核时间；`python -m devflow_ai.benchmark_record summarize` 对审核副本汇总并同时报出样本数与 solved 计数；记录的 `attempt_kind` 是**正式字段**（`formal`/`rehearsal`/`unknown`），不再从自由文本 `notes` 猜测：入口用 `--attempt-kind` 写入，旧记录缺这个字段时一律读作 `unknown`；只有 `formal` 进入正式样本，`rehearsal` 与 `unknown` 默认排除并分别以 `rehearsals_excluded`/`unknown_kind_excluded` 报出（`--include-non-formal` 才计入），`counts_as_solved` 也要求 `attempt_kind=formal`——演练里即便出现正式通过也不算解决。独立评测材料以仓库外的 JSON 由可信方在运行时注入，格式严格、字段多余即拒；用例内容不进入 repr、日志或记录。准备、驱动、判读与自查步骤见 `docs/BENCHMARK_RUNBOOK.md`；两次尝试的用量对账由 `python -m devflow_ai.usage_report` 提供：它只读已提交的 `llm_usage`，按角色汇总输入/输出 token、请求数，并把 `actual_usage` 为空的预留**单列**（可能仍被计费，因此不并入总量也不当作 0），`--compare` 给出两次 Run 的逐角色差值与总差值；它不读提示词与回复内容、不修改任何行，也不把“token 更少”当成“做得更好”的结论。单任务入口为 `devflow_ai.benchmark_run.run_one_task`：一次显式调用内从清单装配 `VerificationConfig`、校验 Task 的 base commit 与清单一致、在领取事务内以确定性创建键（`benchmark:<task_id>:1`）领取**指定 Task** 的 Run（不领取队列中的下一项，也不在领取后靠比对补救）、在限额下由 orchestration 层的阶段驱动推进并写出运行记录。补丁后 Verifier 的“阻塞”是**检查点状态**而不是半成品：它提交草稿后把执行留在 RUNNING，等正式评测把它结清（`finalize_passing_verification` 在同一事务里把执行置为 SUCCEEDED）。因此阶段驱动遇到“RUNNING 且输出为 `VERIFICATION_BLOCKED` 的 Verifier”时按 `verification_blocked` 停止（附 `draft_pending_finalization=true` 与缺失证据），不能再报成 `last_stage_incomplete`；而没有草稿的 RUNNING 阶段仍如实报 `last_stage_incomplete`。正式评测入口 `finalize_benchmark_run` 同样不能按“执行已 SUCCEEDED”去过滤候选——那样会使每一份可用的检查点草稿都看起来不存在；它按最新一条 Verifier 执行读取输出：`VERIFICATION_BLOCKED` 且公开初步通过才转交，否则给出`verifier_still_running`（仍在跑、还没有草稿）或 `no_blocked_verifier_draft` 等具体原因，且不提交任何正式结果。这条接线只让“公开初步通过”真正到达可信独立评测，不改变任何验收规则，也不把草稿当成正式通过。阶段驱动 `run_task_stages` 每轮读取**最新一条非 Supervisor 执行**（而不是"最近一条成功的"，避免旧的成功记录掩盖后来的失败），按它的角色、状态、输出与已提交 Handoff 的来源决定下一步：仍在运行或未成功即停止；Explorer、Planner 分别交回对应的 post-explorer/post-planner 路由；Coder 的 `PATCH_READY` 与 `BLOCKED` 都交给 Coder 自己的 Supervisor 判断（不由驱动按角色名判读）；Verifier 记录 `BASELINE_RECORDED` 时，按其 Handoff 来源找到提出该基线的 Planner 并执行一次 post-planner 路由（首次计划与 REPLAN 后的基线同一规则），来源不是同 Run 的 Planner 则停止；`VERIFICATION_BLOCKED` 保留缺失证据原因后停止；`VERIFICATION_PASSED` 视为终态结果而非路由。每轮重新核对 Run 是否仍 RUNNING、执行权与图步数预算，并在下列任一情形停止并给出原因：Run 已离开 RUNNING、执行权被拒、图步数预算耗尽、上一跳仍在运行（`last_stage_incomplete`）或未成功（`last_stage_not_succeeded`）、基线来源不是同 Run 的 Planner（`baseline_source_unknown`）、Verifier 阻塞（`verification_blocked`，保留缺失证据）或已达正式通过（`formal_pass_recorded`）、Coder/Verifier 输出无法判读（`coder_outcome_unsupported`/`verifier_outcome_unsupported`）、Supervisor 未派发、该角色之后无对应路由、达到 `max_stages` 上限或收到停止信号。BLOCKED、缺路由与上下文不明一律保留具体原因，不当作成功，也不自动恢复或重试。限额由入口实际执行而非只写在文档：计时从 `execute_run` **之前**开始，覆盖工作区初始化；到点同时设置两处——`execute_run` 的 `external_stop_event` 与执行自身的停止事件（`process_run` 收到的那个）。初始化操作拿到的是把心跳事件与外部事件合并的联动停止对象（`LinkedStopEvent`），因此初始化过程中也能观察到截止时间，而不是只在初始化前后各查一次。初始化阶段到期以 `timeout` + `deadline_during_initialization` 记录，其余到期记 `timeout` + `deadline_reached`；同时单独记录"超时是否真的触发"，以便区分"已请求停止"与"执行已结束"。**协作式停止的限制**：到期只表示已请求停止，不证明进行中的外部调用已退出；不得把仍在运行的操作标记为已清理完成，也不允许失去执行权的迟到结果提交。结果判为 `timeout` 仅在计时器确实触发时成立。token 上限在启动前与 Task 的 Policy `max_llm_tokens_per_run` 比对，超出即拒绝启动且不写记录；`BenchmarkLimits` 只接受 `attempts=1`、`auto_retries=0`。同一请求重放（创建键相同）返回 `existing_run` 而不重复执行，避免把它变成自动重试。阶段驱动停止后，入口在**同一次心跳、同一租约与同一截止时间**内把 Verifier 草稿交给 orchestration 层的 `finalize_benchmark_run`：该入口不新增任何验收规则，只定位"可推进的草稿"（Verifier 已提交、`VERIFICATION_BLOCKED`、公开检查通过、有草稿 id）并转交既有 `finalize_with_independent_evaluation`，并复用执行自身的停止事件（因此不会传入空事件、也不在计时范围之外）；材料缺失、清单未固定套件版本、证据上限缺失、公开检查未通过或无草稿时返回具体原因且不提交任何正式结果；正式评测自身抛出的异常会先记入运行记录，再原样抛出。独立评测材料由可信调用方显式注入，不进入 Agent 上下文、公开日志或仓库。任务本身由 `python -m devflow_ai.benchmark_task` 按清单建立：读取调用方提供的 Policy 快照并为 Task 保存该快照，创建前校验快照能承载本次限额（运行时长、`max_llm_tokens_per_run`、最小图步数）以及 Explorer/Coder/Verifier 各自必需的工具权限，任一不满足按配置错误退出 2；该命令不内置任何权限，快照内容与审查由可信方负责，未定稿前清单中的 `policy` 仍标为未核验。首轮基准只运行一次 Run，因此快照本身必须 `max_retries_per_task = 0` 且 `max_rediagnose_attempts = 0`：若只靠入口不重试而预算仍留额度，别的调用方仍可凭同一快照创建额外 Run；两者任一不为 0 时按配置错误拒绝。该约束只作用于基准任务的快照，不改变其他任务的恢复能力；置零后失败按既有策略处理——`assess_recovery_policy` 会给出 `retry_limit`/`rediagnose_limit` 并升级人工，这一点由策略层与过期关闭路径的既有测试覆盖，不由配置本身推断。

修复 Agent 只看到修复前源码、净化问题、公开成功标准、授权范围、安装方法、公开测试及基准证据。参考补丁、后续修复 commit、解法记录和隐藏验收源码由独立评测端保管；工具可访问范围也落实隔离。隐藏测试应在缺陷基准失败、在已知正确版本通过，并检查相关正常行为。验收针对行为，不要求补丁与参考实现一致。

实现进度：首次已分类 IMPLEMENTATION_FAILURE 可在复核证据和策略后原子保存正式失败、FailureRecord 与状态；可按 Task 历史和预算创建幂等新 Run，并将上一轮公开失败提供给新 Run 的 Supervisor、Explorer、Planner 和 Coder。复用字节完全相同的失败补丁会被拒绝，但不同字节不证明实际策略或语义改进。合成任务已覆盖失败、重试和通过；当前仍以单次调度组装流程，不表示持续自动循环、真实任务成功或其他失败类别已实现。

Agent 的基准工作区不能包含可从 Git 对象库、分支、远端记录或共享 worktree 元数据取得的参考修复。评测者持有的答案目录与 Agent 可访问目录分开。

基准本来失败的无关测试不算 Agent 引入的回归。比较具体测试与错误，保存重复试验的原始输出、退出码、耗时、通过/失败/跳过项和样本量。比较系统版本时固定任务包、模型、预算和环境，说明变量与限制；简单、中等、困难各两项只是选题目标，不为凑数量接受无法稳定验收的任务。

## 9. 实施路线与阶段验收

测试、基础结构化日志、资源限制和文档进度记录从相应模块开始同步加入。只把实际执行过的步骤记为已验证。

| 阶段 | 最小交付与验收 |
|---|---|
| 0. 项目核对 | 核对文件、Git、依赖和环境；合并规格与决策记录，确认最早缺口。建立 Git 基线前检查忽略规则及待纳入文件，排除凭据、虚拟环境、数据库数据与大产物。 |
| 1. PostgreSQL 环境 | 用 Compose 启动数据库，健康检查通过，执行简单查询；正常停止和重启后记录仍在。保留数据卷。 |
| 2. 后端骨架 | 增量建立 Python 包、配置、薄 FastAPI 入口、Pydantic 模型、数据库连接、pytest 与基础结构化日志；分别验证进程存活和数据库就绪。 |
| 3. Task/Run 数据链路 | 先修复迁移并建立 Git 基线，在独立测试数据库验证升级、真实 Task 读写与约束、降级后再次升级，以及 Session 生命周期和异常回滚；保留主开发数据库数据。随后实现 Repository、应用服务与 RunCoordinator，API 创建和查询 Task，准入后单 worker 原子领取并建 Run1。创建 Task 时保存完整 Policy 默认配置，不用空占位快照；分别验证 Task 提交幂等、Run 创建幂等、非法转换和唯一约束。 |
| 4. 最小 Harness | 在小型受控仓库实现受限列目录、读文件、搜索、diff、补丁和注册测试命令；验证角色权限、路径越界、非零退出、超时、输出截断、进程清理与 Run 间隔离。 |
| 5. 图与真实 Agent | 先用固定输出节点验证路由和提交顺序，再逐个接入 Agent、基础动态 ContextBuilder、ToolRequest/ToolResult 与基本预算；验证结构化输出、有限解析修复和角色权限，产物和用量可追溯。 |
| 6. 首个真实修复 | 核验 Click 任务包并保存基准；从固定版本探索、产生补丁，由独立 Verifier 运行确定性和语义验收，保存完整轨迹和费用证据。验证上下文按需读取、阶段摘要、裁剪与证据引用；区别链路可运行与任务实际成功，保留失败结果。 |
| 7. 失败恢复 | 补齐 FailureRecord、完整 Policy、人工审查、租约与心跳；故障注入覆盖新 Run、指纹触顶、预算耗尽、执行中崩溃、响应丢失和双 worker 竞争。 |
| 8. 评测与诊断 | 独立 EvaluationRunner 从首个真实任务开始建立基线，再扩展任务集与重复实验；固定任务包、模型、提示词、执行代码及配置版本。报告样本量、成功率、升级率、Run 和额外尝试数、token、耗时与失败分布；环境准入失败和无效试验单独报告。满足 §10.8 前置条件后可启动首个 RSI 实验，不以 V2 记忆系统完成为前提。 |
| 9. 交付与演示 | API、worker、db 通过 Compose 启动，迁移独立运行，正式记录重启后可查；README、基础 CI、真实评测报告与一次成功及一次恢复/升级演示可复现。 |

CLI 保留 worker、调试和 benchmark 入口，长任务不在 FastAPI 请求中执行。应用容器化安排在核心流程可运行后。首个业务闭环以 Task 持久化与查询、准入、原子领取、Run1 创建及关联查询为准；不要求先接入真实模型。

2026-11-13 容器化契约：`compose.yaml` 提供 `db`、`api`、`migrate` 与 `worker` 四个服务；`api`、`migrate` 与 `worker` 用同一镜像，构建输入只有 `pyproject.toml`、`uv.lock`、`alembic.ini`、`migrations/` 与 `src/`，镜像内不含 `.env`、模型密钥、隐藏材料或独立评测材料（Worker 初始化工作区要克隆仓库，故镜像装 `git`）。`api` 复用五项 `DEVFLOW_DB_*` 配置，容器内 `DB_HOST=db`、`DB_PORT=5432`，启动路径不含建表或升级，因此启动容器不等于迁移；`migrate` 位于 `tools` profile，默认命令是只读的 `alembic current`，升级只能显式执行。

2026-11-13 一次性 Worker 入口：`python -m devflow_ai.worker_run` 执行**调用方指定**的一个 Task 的 Run，复用 `orchestration.drive_run_stages`（与基准入口同一份接线）、`Worker.execute_run` 的心跳与租约、`RunCoordinator` 的执行权与原子领取、`model_gateways_from_env` 与同一份 `BenchmarkLimits`（1,200 秒 / 80,000 token / 32 阶段 / 一次尝试 / 零重试）。它**先校验后领取**：任务包可解析且能提供注册命令要跑的公开测试文件、Task 状态为 `ELIGIBLE`、`base_commit` 与清单 40 位一致、Task 已固定 Policy 且与 `--policy` 文件逐节一致、策略 token 上限不超过本次限额、两个目录存在、解释器为存在的绝对路径、该 Task 尚无 Run；任一项不满足即退出 2 且不创建 Run。准备只是检查：真正的创建在 `RunCoordinator.start_initial_run` 的 Task 行锁事务内进行，并在同一事务内复核准备时的 `base_commit` 与 Policy 快照，Task 在准备之后被修改即拒绝创建。重复调用统一返回 `existing_run`（同一幂等键 `worker:<task>:1` 的 Run 已存在时不再执行、不建第二个 Run），CLI 与直接调用 API 得到同一答案。调用方的停止信号与本次尝试的截止时间合并为同一信号，同时交给工作区初始化与阶段驱动，因此初始化期间和**每个阶段运行期间**都可见，心跳失效信号也在其中；停止是协作式的，不强制中断已在进行的模型或子进程调用；调用前已触发停止时在领取之前返回 `stopped`/`stop_requested`，不创建 Run、不改 Task。一次调用只领一次，没有重试开关；被自己的停止或截止打断时报告 `stopped`（`reason` 为 `stop_requested` 或 `deadline_reached`，`interrupted_mid_stage=true`、`stages=null` 因为阶段数不可诚实统计），不接管或关闭历史 Run，停止或失败时保留数据库真实状态、不写正式 Verification、不写基准记录，只在输出里报告 Run id、停止原因与 Run 状态。`--attempt-kind formal` 在不能记录可信基线报告的平台上直接拒绝（否则必然停在 `BASELINE_UNAVAILABLE` 而白耗预算），`--attempt-kind rehearsal` 允许在任何平台执行同一套阶段但明确不是正式样本。常驻调度（从库里选出下一个可运行 Task、多 worker 分工、崩溃后续租或接管的持久契约）仍未实现，`control --run-recovery-loop` 只负责过期 Run 回收。

2026-11-14 常驻调度 V1：`python -m devflow_ai.worker_scheduler` 按可信清单执行，清单是本地 JSON，逐项绑定 Task ID、任务包与 Policy 文件（未知字段、重复 Task、空清单一律拒绝；清单不授予执行权），共享路径、解释器、角色网关与限额由启动参数/环境给出。每个登记项各做一次 `worker_run` 领取与驱动，领取成功或发现已有 Run 后即结束该项，进程内不再执行第二次；配置或状态不可用的项记 `refused` 与原因后跳过，不影响其余登记项；Task 行被其他事务锁住时记 `waiting`，按 `--poll-interval-seconds` 等待后重试**领取**（没有创建 Run，因此不是重试尝试）。全部项结束时退出（`idle_exit`），收到 SIGINT/SIGTERM 后不再领取并把信号传给当前任务的阶段（`stopped`）。不自动重试、不接管、不重放孤儿节点、不关闭不是自己创建的 Run，租约或执行权丢失导致的失败记为该项 `failed` 并保留 Run 的真实状态。调度范围仅限登记项，没有自动发现、优先级或公平调度，也没有独立调度状态表；进程重启依靠数据库状态与既有幂等键识别已领取任务。2026-11-15 常驻模式：默认仍是有限批次（读一次清单、全部结束即退出，清单不得为空）；显式 `--watch` 才按有限正数间隔（NaN/无穷大拒绝）重读同一份清单，允许空清单、空闲等待、停止信号唤醒等待。启动时清单无效退出 2；运行中清单缺失或无效时暂停领取并报告 `worker_scheduler_paused`，不把上一次成功读取的清单当作最新授权，修复后报告 `worker_scheduler_resumed` 继续。已从清单删除的项不再领取（未被领取过则重新登记后重新待处理，已被领取的即使删了再加也不会再跑）。被拒项在常驻模式下只在其任务包 `task.json` 或 Policy 文件字节变化时重新检查一次，不按间隔反复重试，也不把已创建的 Run 当新尝试。2026-11-16 可信验收接线：控制端可在清单条目里给出可信材料的**路径**（材料本身不入仓库、镜像、任务包或日志），调度任务在满足既有门禁时复用 `orchestration.finalize_benchmark_run`，且该调用发生在**同一次** `Worker.execute_run` 的心跳、租约、执行权与停止事件之内；材料按任务包的 `materials_sha256` 与套件版本绑定，不匹配即在该项领取前拒绝，避免跨任务串用；无材料、公开检查未通过、评测不可用、停止或执行权失效均停在草稿并如实报告，`formal_acceptance` 只有真正提交正式 Verification 才是 `committed`；重放只返回 `existing_run`，不重复评测。2026-11-17 运行记录：可信调用方可在清单条目给出 `record` 路径（一次性入口用 `--record`），运行结束后用既有 `BenchmarkRunRecord`、`classify_outcome` 与排他创建写出待审核记录——`formal_pass` 必须具备可从磁盘读回的正式 Verification 与证据；演练、未完成、失败、超时按实际结果记录；记录含 Task、Run、`attempt_kind`、模型、Policy 与证据引用，人工审核结论留空，由真人另行 `review`/`task_review`；记录路径已存在则在领取前拒绝，重放不写记录也不重跑，同一清单内不允许多个条目共用记录路径。2026-11-19 记录补写：`python -m devflow_ai.record_backfill` 只读地重建指定 Task/Run 的待审核记录——复用同一 `classify_outcome` 与排他创建，`formal_pass` 必须能读回正式 Verification、草稿与独立回执；截止时间、`attempt_kind`（默认 unknown，显式声明时注明来源）、模型与人工审核结论不从证据推测，无法重建的独立评测尝试如实记为未记录（故通常得到 incomplete）；仍在 RUNNING、Task/Run 不匹配、缺结束时间或目标文件已存在均拒绝；不建 Run、不重跑、不改数据库与原产物。2026-11-16 演练边界：`rehearsal`/`unknown` 即使收到材料也不进入正式事务——不写 Verification、不把 Run 标成正式 `PASSED`，只报 `finalization = skipped / attempt_kind_not_formal`，基准入口同一规则。材料路径不得位于任务包目录、工作区根、产物根或**本地 `Task.repo` 源码 checkout** 之内，按真实路径（解析符号链接）与平台大小写规则判断，worker 与 benchmark 两个入口共用同一实现；`Task.repo` 的 `file://` 形式（含 `file://localhost/C:/…`、网络共享 `file://server/share/repo` 还原为 UNC、百分号编码与空格）按本地路径处理；为远程 URL 时本地文件系统无法据此定位 checkout，该情形只做前三项检查并明确记录为适用边界；含材料路径的调度清单属于可信控制端文件，不得放入公开任务包、镜像或 Agent 上下文。容器内可以运行 API 与 Worker 演练，但受控 pytest 的 JUnit 报告采集依赖 Windows Job，非 Windows 上拿不到可用报告，故容器不能作为可信基线或正式验收环境；正式结论仍只能在 Windows 复核。

环境阶段先核定项目运行 Python 版本和依赖版本，benchmark 的目标仓库 Python 版本单独管理；不静默升级到最新。基础结构化日志在可用时携带 `request_id`、`task_id`、`run_id`，后续补 AgentExecution 和 ToolCall 标识；日志不得输出数据库口令或模型密钥。

API 与 Alembic 复用项目的数据库连接配置读取逻辑，由 `builder_database_url()` 读取当前环境的五项 `DEVFLOW_DB_*` 配置；迁移不在 `alembic.ini` 中另行硬编码真实连接地址或凭据。不同环境可提供不同配置值，复用配置读取逻辑不要求各环境的数据或配置值相同。

最小 Harness 只接受结构化工具名与受约束参数。读、搜、diff、补丁和测试命令按角色和计划范围授权；命令使用注册模板与参数约束，不接收任意 Shell 字符串。执行前解析实际路径并校验允许根目录和符号链接，输出过大时保存 Artifact 及截取片段，超时或 Run 停止后清理相关子进程。

注册命令的基础执行器由可信调用方提供命令名到固定参数序列的映射，Agent 只能选命令名，不能附加参数。执行器要求绝对路径的可执行文件，并复用有界输出与进程清理。Worker 可将登记的命令绑定到单次 Verifier 执行；协调器核对角色权限、执行权和工作区锁，并记录 ToolCall 与超长输出 Artifact。该入口尚未接入完整 Verifier 流程，可信命令注册配置、具体验收判断与语义审查仍待实现；命令退出码本身不等同完整验收结论。

对可信调用方标记的 pytest 命令，Harness 可在固定命令后追加由系统生成的 JUnit 输出路径，Agent 不得指定该路径或追加参数。每次 ToolCall 使用独立、排他创建的报告目录，正常结束或以测试失败退出时采集本次报告为 Artifact；超时、报告缺失或不可信时不得借用旧报告。当前报告采集依赖 Windows Job 等待进程树退出，非 Windows 暂不支持；该机制不构成执行沙箱。

逐测试比较要求两次命令使用同一个登记名称和固定命令摘要，且已记录的基准测试、成功补丁、修改后测试顺序明确，期间没有其他已登记的工作区变更操作。该检查只是必要条件，不能单独证明环境、工作区内容或测试本身未变化；固定基准和环境证据、测试变更审查仍需独立验证。

对通过 `Python 解释器 -m pytest` 或 `Python 解释器 -B -m pytest` 注册的受控测试命令，执行前用同一解释器和对应的 `-B` 选项采集 Python 版本、位数、是否禁用字节码、实现、操作系统名称及版本和已安装 Python 分发包版本，作为本次 ToolCall 的 Artifact。前后比较要求两份记录可核验且所记录字段一致；缺失或不同则不将结果作为可比较证据。采集与测试共用一次工具调用的时间预算。可信注册命令可显式使用 `-B` 与 `-p no:cacheprovider` 避免生成常见缓存，系统不自动更改已注册命令。该观察不读取环境变量值，也不保证测试期间的其他环境因素、进程状态或外部服务一致。

为支持 `src` 布局，可信调用方可为受控命令指定工作区内的相对 Python 源码目录；Harness 只对子进程设置对应 `PYTHONPATH`，不修改 Worker 进程的全局环境。该相对目录参与命令摘要，Agent 不能在工具请求中自行设置；目录越界或不存在时拒绝执行。

测试数据库的身份只能依据目标测试实际使用的连接来确认；系统自己另建连接并附上相同 ToolCall ID 不足以证明测试使用了该目标。当前尚未建立通用连接来源和覆盖证明，因此语义上下文及草稿将外部连接、基准工作区和完整测试有效性明确标为未验证，`finalization_allowed` 固定为 false。协调器可在重验执行权、工作区锁和草稿证据后返回 `blocked` 与缺失项；它不写正式 Verification 或改变状态。该标记不代替未来正式验收入口的证据检查，也不推断测试一定使用了数据库。

2026-11-09 调整方向：用户指出当前安全检查过度影响进度。V1 按经核验的可信任务包确定实际需要的环境证据；明确不依赖数据库的任务不以数据库连接记录作为验收前置条件。任务依赖未知时仍不能假定“不需要”。当前可信调用方可声明任务不需要任何外部连接；复核时核对 Task、基准、目标测试和命令摘要匹配后，不将外部连接来源列为缺失，并记录该声明。该声明是调用方提供的信息，并非系统对实际连接的观测；可信任务包尚未完成核验，基准工作区和测试完整性仍阻止正式验收。未知、需要连接或未提供配置时仍保留连接来源缺失项。

Verifier 的 `git_diff` 从 Task 固定的 base commit 读取当前工作区的已跟踪改动，并将完整未截断结果和原始 `numstat` 保存为关联 ToolCall 的 Artifact；由后者提取文件名、文件数和增删行数。执行前核验角色权限、租约和工作区锁。当前空 diff、二进制差异、超限输出或存在未跟踪文件（包括 Git 忽略文件）时停止该次取证，不自动删除文件。多次 Git 读取之间的外部修改及子模块内容仍不能由此证明完整，后续须单独处理。

语义审查的中间执行要求 Supervisor 批准来自 Coder 的 Verifier 交接，并从已记录的测试、补丁和 diff 构建上下文；模型可以在预算内请求读取文件。通过或失败的语义意见必须引用本次 Verifier 读取内容或同一次 diff 中可核对的单行文件片段，diff 引用还须区分新增、删除或上下文；若 diff 之后存在已记录的工作区修改操作则不接受该审查。当前可在工作区锁与事务内重新核验执行权、引用和相关记录后保存语义草稿 Artifact，但它不是正式 `Verification`，也不更改 Agent、Run 或 Task 状态。最终持久化事务仍须重新检查执行权与证据；引用内容真实不保证模型推理正确。

补丁后交接可选择绑定 Verifier 接收器：可信调用方提供已登记的测试命令、目标测试名称与语义模型，入口核对 Coder 交接、批准计划对应的基线和补丁引用，运行修改后测试、采集 diff 并保存语义草稿，再复核就绪状态。该接收器记录 `VERIFICATION_BLOCKED` 并等待后续独立评测；它本身不会创建正式 Verification 或结束 Run。

可信配置启动的基线测试在工作区锁内先观察 HEAD 是否为任务固定提交，并检查相对该提交的跟踪文件差异和未跟踪文件；不满足时不执行测试。通过检查并生成有效测试报告的调用记录供后续就绪复核使用。该观察不证明测试执行过程中或外部进程没有改变工作区，也不覆盖子模块内容；没有该检查的旧基线仍不能被当作已检查。

独立评测已有控制端内存用例及 stdin/stdout 候选适配器的基础执行能力；在工作区锁内复核关联草稿与运行前后 diff，生成绑定 Run、补丁、diff 的汇总 Artifact。可在同次评测中使用同一用例、适配器和镜像，对调用方提供的缺陷基准与正确版本校准；两者提交和干净工作区先行核验，缺陷失败且正确版本通过才继续评测，记录不暴露参考提交。可信 V1 显式通过入口可在复核该记录、公开测试、语义草稿、任务配置和当前工作区后，在事务内写入正式通过与状态转换。Click #3019 首次真实任务的参考材料取自 PyPI 8.3.1 wheel，控制端源文件与 wheel RECORD 一致；测试集及材料仍由可信调用方管理。UNKNOWN_FAILURE 重新诊断的补丁后 Verifier 草稿在公开初步结果为 `passed` 且调用方显式提供校准材料时，可复用该入口完成一次独立评测与正式通过；公开失败、缺配置/材料、校准或评测失败/不可用仍阻塞，已提交回执不重复评测。重新诊断恢复在同一 Run 的恢复调度锁内串行完成回执查询、独立评测与正式提交；评测/V1 通过入口自身的 workspace 锁使用独立 namespace，锁等待后重新读取 Run 终态和已提交回执，因此并发恢复不会重复评测，也不会因终态 Run 重新进入派发。失败恢复仍不包含失败后的自动循环；Coder 后 Supervisor 的 `REPLAN` 经现有 Planner 接收器单次执行后，仅再执行一次现有 Planner 后 Supervisor 路由：`CODE` Handoff 经现有 Coder 接收器执行一次并保存结果后停止，`BASELINE` Handoff 经可信 Verifier 接收器记录一次基线，随后同一个重新规划 Planner 再执行一次基线后 Supervisor 路由，其 `CODE` Handoff 派发一次 Coder，该 Coder 的提交结果继续进入其 Supervisor 路由，`VERIFY` 时经既有补丁后 Verifier 生成草稿并保存，仍不写正式 Verification、不改 Run 终态；该 Coder 的 Supervisor 请求 `REPLAN` 时在预算允许下再启动一轮 Planner（不新增循环开关，受既有图步数与工具调用预算约束；工作区已有非拒绝补丁时下一轮基线被既有检查拒绝，循环随即终止）；基线派发要求调用方提供匹配当前 Task 与固定提交的可信 `VerificationConfig`，缺失或不匹配、已有非拒绝补丁调用、Verifier 运行中或失败均保守返回，不清理工作区也不写入正式 Verification；基线后路由另需 Supervisor 与 Coder 的配置，缺失时分别停在 `post_baseline_route_pending` 与 `code_pending`，补丁后 Verifier 还需可信配置与 Verifier 网关，再次要求基线只报告 `baseline_already_recorded` 而不重跑。其他 Handoff 只保存，不继续 Coder 后 Supervisor、Verifier 或自动循环，仍不代表持续自动恢复完整。

Coder 的 `patch_path_check` 补丁路径预检从其已提交交接追溯同一 Run 内 Supervisor 批准的 Planner 计划，使用计划的 `target_paths` 校验 Git 解析得到的补丁路径，并记录 ToolCall 和补丁哈希。预检本身不写入工作区。

`apply_patch` 采用同一批准范围校验，并在同一工作区锁内重新核对执行权后调用受控 Git 进程。该工具要求独立的角色授权及停止信号。被拒绝的补丁不会进入 Git 应用步骤；运行中断且无法保存结果时，未决 ToolCall 使旧工作区保持隔离。

使用补丁 Artifact 的入口检查它属于当前 Coder 执行且类型为 `patch`，从受信任的产物根目录读取并校验大小、路径和哈希，然后对同一份字节执行路径检查与应用。Artifact 缺失、归属错误或内容不匹配时记录拒绝。

当前 Worker 的仓库克隆与检出、受控读取和补丁预检使用同一 Run 的 PostgreSQL 会话级 advisory lock 协调。锁等待支持停止与超时，操作发生异常时释放。该锁只协调主动取得同一锁的操作；实际补丁应用仍需解决锁连接失效和受控子进程存活期间的处理。

当操作传入停止信号时，锁监测线程周期性检查持锁连接。检测到连接故障时设置该停止信号，监测线程退出需经过有界等待。检测存在时间差，仍须依靠未决修改类 ToolCall 隔离旧工作区，并确认进程树清理结束。

补丁工具提交结果时，在结果事务中核对原持锁数据库会话仍持有该 Run 的 advisory lock，并检查停止信号。持锁连接已失效时，结果事务回滚，未决工具记录继续隔离旧工作区。

进入上述工作区操作时，在取得锁后检查同一 Run 的修改类 ToolCall。若克隆、检出、补丁或命令仍为 `REQUESTED`、`RUNNING` 或 `UNKNOWN_RESULT`，工作区保持隔离，操作不得开始。该检查依赖工具启动前提交的记录，不能证明旧进程已退出。

评测指标至少说明 Task 成功数及分母、首 Run 与最终成功率、升级人工率、总 Run 数、额外尝试数、恢复成功率及其分母、graph step、ToolCall、token、各 Run 耗时及 Task 总时长。Task 总时长应说明是否包括排队与人工等待；环境准入失败、取消和无效试验单独报告。

前端、Redis、Celery、Langfuse、MCP、动态沙箱容器、Kubernetes、复杂云部署及训练/微调暂缓。遇到实际阻塞时再根据证据评估。CI 先运行锁定依赖、lint、单元测试和必要的 PostgreSQL 集成测试，真实模型评测单独触发。

### 9.1 升级路线与原阶段的对应

保留上述 0 至 9 的 V1 阶段编号。附件中的 A 至 H 是对应工作包及扩展分支，不是另一套项目进度来源。当前顺序为 A → B → C → D → E；E 的评测基线稳定后，优先启动 H 的上下文选择实验，再推进 F、G，并把成熟的记忆与技能策略纳入后续 H 实验。评测与日志随相应模块加入，V1 交付整理持续进行。

| 工作包 | 对应阶段与依赖 | 主要交付与验收 |
|---|---|---|
| A：迁移与版本基线 | 当前阶段 3 的入口，补齐阶段 0 的 Git 基线 | 核对枚举所有权并补齐降级；独立测试数据库中 schema、迁移版本和真实读写一致；升级—降级—再升级及回滚有证据。 |
| B：Task/Run 闭环 | 阶段 3，依赖 A | Task 创建、查询、准入、原子领取、Run1 及关联查询；完整 Policy 快照；状态与幂等约束验证。 |
| C：Harness 与角色协作 | 阶段 4、5，依赖 B | 受控工具、结构化契约、预算与进程清理；先替身图再真实 Agent；所有角色交接提交后路由。 |
| D：动态上下文与真实修复 | 阶段 5、6，依赖 C | 按需证据、阶段/恢复摘要、搜索停止条件；固定真实任务的完整执行和独立验收轨迹。 |
| E：恢复、评测与诊断 | 阶段 7、8，依赖 D | 故障注入验证恢复；固定版本重复评测；由失败记录定位角色、工具、证据和恢复决定。 |
| H：受控 RSI 首轮及后续实验 | 首轮依赖 E 和 §10.8 的版本/隔离/预算门槛，不依赖 F、G 完成 | 优先优化 ContextBuilder 的日志与证据选择；保留 V0、候选、独立测试、接受或淘汰理由、累计成本及 V1 参与下一轮的轨迹。 |
| F：长期经验记忆 | V2，建立在正式执行记录和 E 的评测设施上 | MemoryService、来源/范围/时效/冲突治理、跨任务和长历史召回；独立事务、幂等与隔离测试。 |
| G：记忆对照与 Skills | V2，依赖 F | 固定快照对照无记忆、普通检索、治理后记忆；验证有条件、有版本的 Skill 在多个任务中复用及拒绝错误套用。 |

当前继续使用 Python 3.11、同步 SQLAlchemy/Psycopg、PostgreSQL、Alembic、FastAPI 和 uv 锁文件。LangGraph 与模型 SDK 到实际接入阶段再选择兼容版本；不因路线新增或单条弃用警告升级整套依赖。当前九表的详细实现以及新增实体仍逐项教学和验收，不预建空模块或未来表。

## 10. 能力扩展、经验记忆与受控 RSI

### 10.1 范围与完成口径

V1 包含五角色 Agent Loop、可靠恢复、Harness Engineering、结构化输出、基础动态上下文、独立验收、评测和可观测性。V2 在这些基础上增强长期经验记忆、冲突与时效管理、跨任务与长历史召回，以及 Skills。RSI 是依赖稳定执行和评测设施的实验线，首轮可以先于完整 V2。

采用“已设计、已实现、已测试、实际运行通过”记录进度；教学理解验证单独记录。采纳本路线不等于已经推导完未来字段、阈值和 API。只有实际测量的数据可用于成果和简历，单元测试通过率不能代替真实修复成功率。

### 10.2 工程组件与信息职责

第 5 节的五个 Agent 和控制权保持不变。新增组件不自动成为新的 LLM Agent，先按实际需要建立少量模块。

| 组件 | 职责 | 边界 |
|---|---|---|
| MemoryService | 经验摄入、冲突与版本处理、受范围约束的检索、使用记录 | 经验不能覆盖 Task/Run 正式状态；不决定角色路由或工具授权。 |
| EvaluationRunner | 独立运行固定任务包、重复评测并汇总系统表现 | 与 Verifier 的单次验收分工；评分与隐藏验收由受保护控制端管理。 |
| EvolutionRunner | 调度系统改进实验、候选版本、评测、谱系与停止条件 | 复用五角色处理获准的改进任务；生命周期仍由 RunCoordinator 管理，工具仍经 Harness，候选不能自我批准晋升。 |

PostgreSQL 保存正式任务、记忆版本及实验记录，是唯一持久化真相；Artifact 文件保存大内容并由正式引用和哈希确认。经验记忆辅助决策，AgentContext、摘要与搜索索引是派生材料。索引必须可由正式记录重建。已知记录 ID 时优先准确读取，不用语义检索猜测正式状态。

### 10.3 结构化输出与有限修复

使用 Pydantic 等校验 Supervisor 决策、专业 Agent 输出和 ToolRequest/ToolResult 的必填字段、类型及允许值；结构校验后仍要检查角色权限、状态转换和 Policy。格式正确不代表证据充分或操作获准。

Planner 的执行状态与分析结论分别记录。对于产出计划与证据不足这两种正常完成的情况，结构化输出使用 `outcome` 区分。`PLAN_READY` 携带技术方案、目标文件、实施步骤、范围边界与预期验证方式。`NEEDS_EVIDENCE` 携带要补查的事实与受影响的计划决策。Supervisor 根据结论决定下一步，计划仍需满足批准与授权要求。

Coder 的结构化输出区分 `PATCH_READY` 和 `BLOCKED`。前者引用补丁 Artifact 并附摘要，后者描述已完成工作、阻塞、证据引用、所需决定和范围影响。结构校验不等于 Artifact 归属或证据真实性校验。

Coder 的执行入口接收已提交且经 Supervisor 批准的 Planner 计划，经模型网关按需请求 `read_file`、生成补丁草稿或提交阻塞结论。读取经 Harness 校验并返回带截断标记的回执。补丁草稿受大小限制，经 Harness 应用并提交补丁 Artifact 后，才提交 `PATCH_READY`。

解析或契约失败允许在单次 AgentExecution 内按 §6.2 限额重新生成，记录原始输出引用、校验错误、修复次数及用量。所有模型请求、AgentExecution 与工具请求仍按 §6 计量，不允许隐藏的无限修复循环。恢复路线沿用 `AGENT_CONTRACT_FAILURE`；只有 Run 已失败时才写 FailureRecord，运行中的校验事件不能伪装成已失败 Run 的记录。未经有效校验的 Supervisor 输出不得直接触发路由、创建 Run 或修改数据库。

修复请求发出前，模型调用层估算实际发送的完整输入，包含原上下文、无效输出和校验反馈。输出上限取配置上限与 Run 剩余额度扣除预计输入后的较小值，并传给模型适配器。剩余额度未知或扣除输入后没有正数输出额度时拒绝发起请求。

Planner 的首次生成与修复请求共享同一个 Run 的 token 预算。每次请求都在 Run 锁保护的事务内核对预算并将预计输入与输出上限写为 `AgentExecution.llm_usage` 中的单笔预留，提交后再调用模型。首次请求无法准入时记录预算阻塞，不发起生成或格式修复。客户端超时或缺少可信用量时，该笔预留继续占用额度。收到实际用量后，只结算与原请求匹配的记录；即使租约过期也允许结算。Planner 输出的正式提交仍须重新校验执行权。

首个 Planner 供应商适配器使用 DeepSeek Chat Completions 的 JSON 模式，通过 `DEEPSEEK_API_KEY` 和 `DEVFLOW_DEEPSEEK_MODEL` 配置。适配器透传网关授予的输出上限，将供应商报告的输入与输出用量交回网关，并将生成文本交给本地契约校验。Worker 和预算逻辑保持供应商无关。

验收覆盖合法输出、缺字段/错误类型、越权但结构合法的请求，以及修复次数或预算耗尽；保存实际拒绝和交接证据。

### 10.4 动态上下文与 Agentic Search

基础 ContextBuilder 从现有正式记录起步，不依赖 MemoryService。公共上下文保留目标、授权范围、验收约束、角色、当前阶段、剩余预算、必要证据及来源。Supervisor 使用正式完成情况、失败历史和允许性判断；Explorer 侧重仓库、复现与探索历史；Planner 侧重根因、约束、已尝试方案和验证要求；Coder 使用正式计划、相关代码和范围；Verifier 使用最终 diff、测试要求及真实结果。

增加阶段摘要、恢复摘要、相关日志片段选择、按需读取和 token 预算裁剪。摘要保留未决事项、关键约束、来源及版本，定位后能够回查原文；摘要不修改原始证据或替代正式状态。上下文不足时交接或补证据，不能为了适应预算删掉安全边界、验收条件后继续猜测。

Agentic Search 由当前角色围绕明确的证据缺口发起，通过 Harness 搜索获准的代码、文档和经验。记录查询目的、已查范围、来源、未解决问题，以及证据已足够、额度耗尽或缺少访问权限等停止条件。起步使用关键词、路径和调用关系线索；向量、混合及图检索需要后续实际问题和对照结果支持。搜索结果和检索到的流程不自行授予新权限。

验收包括角色相关性、摘要到原文的可追溯性、关键约束保留、预算裁剪、按需补证据和停止；同时观察重复探索、上下文 token、耗时与真实修复结果。

### 10.5 独立评测与可观测性

Verifier 依据 §3 验收单次补丁；EvaluationRunner 衡量系统版本在任务集上的表现。评测从首个真实任务起步，固定仓库 commit、环境、模型标识、提示词、工具、预算、代码/策略版本和任务包版本；记忆与 Skills 接入后再固定其快照和版本。预先定义验收与排除规则，同一配置重复运行，保留所有结果、人工介入和排除原因。

开发任务用于改进，独立保留任务用于回归和晋升。隐藏测试、标准答案、评分与晋升代码放在候选和修复 Agent 无权读取或修改的位置；允许查看的公开测试和诊断与这些材料分开。不得把答案、同一测试任务的既往成功解答或保留集诊断写入记忆/Skills 后仍声称独立测试。记录保留集使用次数，避免反复针对同一保留集调优；连续学习另行规定时间顺序、可见历史和评测协议。

结构化日志逐步关联 request_id、task_id、run_id、agent_execution_id、tool_call_id，实验阶段再关联候选、实验与评测标识。记录事件、耗时、错误、证据引用、模型用量及代码/配置版本；不得输出密钥。完整大日志走 Artifact，常规日志与数据库汇总先满足诊断，Langfuse 保持后置。

指标沿用 §9，并补上下文开销、重复失败/探索、工具成功情况和每个成功任务的成本。额外 Run、失败候选和评测都计入实际消耗；usage 未知时说明未知，金额换算记录所用价格版本。环境失败、取消、无效试验与人工介入分别报告，所有比率说明分子、分母和样本量。

验收要求可从失败记录追到角色、工具、证据及恢复决定，并重跑同一基线。基础 CI 运行 lint、单元与真实 PostgreSQL 集成测试，真实模型评测独立触发。六任务可检验工程流程，不足以直接证明跨仓库泛化。

### 10.6 长期经验记忆、冲突与召回

2026-11-18 适用条件：候选可声明成对的完整 base commit 与环境标签；候选预览会显示条件是否匹配，仅在目标 Task 的 base commit 精确一致且操作者声明的目标环境标签一致时才通过适用性预检，新使用声明也要求两者匹配。环境标签并非运行环境观测，匹配不证明内容真实、权限或独立评测安全。缺少条件的旧候选不获新使用声明，旧摘要和历史使用声明保留兼容；原样重放只返回历史记录。迁移经隔离数据库测试，未用于已有业务库；未接入 Agent 或正式评测。

V2 保留现有九张业务表，不创建重复的 Evidence 真相。按实际查询和事务需要考虑 MemoryItem、MemoryRevision、MemoryUse：分别表达经验身份/范围/内容/来源/条件/时间/状态、修订与冲突历史、具体使用版本和观察结果。MemoryRevision 可以先并入版本化 MemoryItem；除首批 MemoryItem 外，这些概念尚未锁定表结构。2026-11-17 实现记录（首批，仅摄入与读取）：新增 `memory_items` 一张表（迁移 `c4a7e2f19b30`，仅在隔离测试 schema 执行）与 `devflow_ai.memory`（`MemoryService.ingest/read/retire` 与 CLI）。**来源状态与内容可信度分开记录**：`source_state` 只说明所引用的行建立到什么程度（`trusted_passed` = 有 `trusted_v1/passed` Verification；`review_accepted` = 同一 Run 的 TaskReview 结论为 accepted），由服务从行推出、不接受调用方声明；`content_status` 说明**这条陈述本身**已知什么，首批没有任何内容核验机制，因此恒为 `unverified_candidate`，数据库检查约束固定该值——记忆是候选，不是已核实事实，也不表示有人审阅过这句话（人工审核的是那次 Run 的补丁）。`identity_key` 唯一即幂等身份，同身份不同提交拒绝而非覆盖，已停用身份不能靠重放复活；`scope_kind` 只允许 `repository` 且必须与来源 Task 的 `repo` 完全一致，读取与停用都要求调用方声明同一范围；仓储字符串只是范围标签，**不是身份认证或租户授权**。并发一致性：准入事务依次对 Task、Run 及所引用的 Verification/TaskReview/Artifact 行取 `FOR SHARE`，在锁下核对范围和来源；Task→Run 与审核服务锁序一致，也避免插入时 Task 外键隐式加锁造成锁序倒置。锁仅保护事务期间，不能阻止提交后外部修改。首批**未**做：内容核验、修订/冲突历史与 MemoryUse、从完成或失败的 Run 自动提取、Agent 上下文注入、跨任务召回与检索、项目级范围、Artifact 字节内容的读取与核对。

2026-11-17 操作者候选预览：允许显式候选附带成对的精确主题和带时区过期时间；可信调用方可按目标 Task 仓库、精确主题预览未过期、未停用、来源 Task 不同的候选。结果仍全部为未核实内容，同主题结果一起展示而不自动判定冲突；超过数量上限时拒绝截断，以免把局部列表误当完整结果。旧候选缺少主题与过期时间时不参与预览，身份读取仍保留。该接口仅供可信操作者，不构成权限认证、完整冲突检测或可用于独立评测的召回：不同 Task ID 可能是同一道题，也无法识别答案和保留集污染；不得接入 Agent 或评测上下文。主题、适用条件与有效期均为调用方声明，未经事实核验。

2026-11-17 候选人工审核记录：`memory_reviews` 保留可信操作者声明的单条 accepted/rejected 决定、理由、审核人标签和数据库时间；读取候选提供规范化快照 SHA-256，写入时锁住候选并比对摘要，同一候选的并发或重复提交按完全一致幂等、不同结论拒绝。审核不改原候选，不证明内容真实、不认证审核人，不自动允许召回；读取审核时报告快照是否仍匹配及候选当前状态，停用后不得新写或重放审核。更正需新的候选与审核，本批不支持追加修订历史。迁移仅在隔离测试 schema 验证，未迁移已有业务数据库。

2026-11-17 冲突记录：可信操作者可对同仓库的两条活跃候选显式声明冲突，绑定双方候选快照摘要、声明人和理由；数据库对排序后的候选对唯一约束，并发按候选 ID 的固定顺序加锁，反向重复声明幂等。操作者可记录一次解决说明，保留原候选与原声明，不自动选择事实；记录读取报告双方快照是否匹配与当前状态。预览附带已登记的未解决冲突 ID，不代表未登记的冲突不存在。仓库标签与操作者名不构成认证；冲突检测、冲突答案判真、自动召回和评测隔离均未完成，同一候选对暂不支持重新开案。

2026-11-17 候选替代关系：可信操作者可声明同仓库两条候选的前后关系，绑定双方快照摘要、声明人及理由；后者必须为活跃候选且数据库创建时间严格晚于前者（这是防环约束，不证明事实的有效时间）。固定 ID 顺序锁住双方并以唯一约束限制每条候选至多一个前驱和一个后继，完整重放幂等，不同关系拒绝；保留双方原始内容、来源和审核，既不自动停用旧候选也不自动选用新候选。预览列出相关关系 ID，读取关系报告摘要是否匹配和两端状态。关系仅为操作者声明，身份未认证，缺少有效时间判断、分支与合并；不能由该关系推断事实正确或独立评测可用。

2026-11-17 操作者声明的使用记录：`memory_uses` 可记录可信操作者对正在运行的目标 Task/Run 声明使用某候选的目的、审核引用和候选摘要。新记录要求来源 Task 不同、仓库标签匹配、候选活跃且有主题和未过期期限、候选摘要与 accepted 审核匹配、无已登记未解决冲突、没有已声明的后继；记录与源记录分离，按 UUID 幂等。原样重放只返回历史记录，不能证明当前仍可使用。记录是操作者声明，不证明模型实际收到上下文、内容正确、权限已认证或评测独立；不同 Task ID 可能仍为同一测试题，本入口不能作为独立评测记忆接入。停用或新发现冲突不回写历史声明。

2026-11-18 使用声明锁序修正：先锁目标 Run，再以非等待方式锁目标 Task，随后锁来源 Task 与候选。Run 完成事务持有 Run 时声明等待，完成后重核状态；若审核等事务先持有目标 Task，声明返回可重试的忙碌错误并回滚，避免两种相反锁序形成死锁。该处理不重试模型调用，调用方应检查已有 Run 状态。

2026-11-18 可信操作者单条召回准备：`MemoryService.prepare_recall` 复用使用声明事务与来源、范围、有效期、审核、适用条件及已知 benchmark 拒绝检查，在提交新声明后返回对应的未核实候选快照及声明引用；提交失败不返回候选，同一声明身份重放不再次返回候选，新请求须重新检查。该记录仍只是操作者声明，不证明模型收到内容、调用者已认证、内容真实或评测独立；结果返回后条件可能变化，未接 Agent 或正式评测。

可信操作者可以用 `python -m devflow_ai.memory preview`、`review`、`prepare-recall` 和 `read-use` 完成显式的预览、候选审核、单条准备及声明追溯；具体参数用各命令 `--help` 查看。候选内容会输出到终端，操作人员须选择合适的环境处理；这条流程不自动注入模型上下文。

`list-uses --scope-repository <仓库> --task-id <Task ID>` 可按任务查看操作者使用声明及候选来源引用；候选停用后历史声明仍可查看，列表中的候选状态是查询时的当前状态。列表不输出候选正文，也不证明模型实际使用过候选。

2026-11-18 Explorer 显式记忆接线：可信控制端可向 `Worker.execute_explorer` 提供 `MemoryRecallRequest`，选择一条已通过现有审核、范围、版本及有效期检查的候选；Worker 在正常模型调用和受控文件读取之前记录声明及带来源、审核引用和适用条件的 `memory_recall_v1` 输入，提示 Explorer 将它视为未核实经验并从当前仓库核查。没有请求时保持原流程，默认调度器尚不自动选择记忆。已知 benchmark 目标拒绝该接线；通过此接线标记记忆输入的 Task（包括后续 Run）被独立评测和正式通过入口拒绝，避免把记忆辅助结果称为独立评测；模型请求失败也不自动撤销标记。标记只覆盖本接线，不能识别外部手工注入或未标记的其他渠道；候选正文不会自动扫描隐藏材料，可信操作者必须只选择允许提供给模型的内容。记忆审核不证明事实真实或审核人身份，仓库标签不是认证；该流程不改变工具、Policy 或预算边界。

2026-11-18 阶段驱动显式接线：可信调用方可向 `drive_run_stages(..., memory_recall=...)` 提交一条选择，驱动将其用于下一个获 Supervisor 批准的 Explorer 交接，之后不自动重复注入；如果没有 Explorer 交接则不会生成使用声明。`worker_run` 支持可信操作者显式传入候选，但只允许无独立材料的 rehearsal，并在领取前检查目标与候选；benchmark 入口和调度清单不自动选择候选。前述独立评测限制不变。

2026-11-17 已知 benchmark 防护：`benchmark_task.create_benchmark_task` 在 Task 的准入证据写入 `pack:<pack_id>`；候选预览与使用声明（包括幂等重放）对带该标记的目标 Task 拒绝，避免经这两个入口向已知 benchmark 提供候选。此标记不是所有评测任务的权威分类：缺失标记不代表非评测，Task ID 不同或 issue URL 不同也不证明题目独立；该检查不能替代记忆快照、题目身份归属和独立评测隔离，记忆仍未接入 Agent/正式评测。

2026-11-18 已知 benchmark 来源：摄入和摄入重放在来源 Task 共享锁下检查上述标记，发现已知 benchmark 即拒绝；预览与使用声明也在来源 Task 锁下检查，并过滤或拒绝已标记来源，包括后来被标记的旧候选和声明重放。预览先获取有界候选快照，再按来源 Task、候选顺序加锁并复核来源，不能把快照后的新增记忆当成已覆盖；历史候选和使用记录不改写，按身份读取历史记录仍可用。该检查不扫描内容，未带标记的评测来源及其他题目关联无法识别；不得将此检查当成完整污染隔离或作为 Agent/独立评测接入的依据。

2026-11-17 候选预览并发一致性：按 UUID 顺序对本次候选取得共享锁后再读取冲突与版本关系；现有声明/解决入口按相同 UUID 顺序取得互斥锁，因此正在提交的相关声明完成后才形成预览，预览期间的新声明等待。该保证限于经过这些服务入口的写入，不覆盖直接数据库写入或任意未来路径；结果仍是有限的未核实候选，不证明不存在未声明的冲突。

2026-11-18 预览时效：候选行锁全部取得后重新读取数据库时间并过滤已过期候选，避免锁等待期间过期的候选通过预检；数量上限先行检查，以免误把截断结果当完整列表。返回之后仍可能过期，新使用声明会独立重核。

2026-11-17 候选预览补充候选级预检：在持有候选共享锁期间同时读取审核、未解决冲突和版本关系；结果显示审核缺失/接受/拒绝/摘要不匹配、已登记未解决冲突及声明的后继。`eligible_to_request_use` 只表示这些候选级条件具备，可以申请一次使用声明；它不核定目标 Run、调用者权限、适用环境、内容真伪或评测独立，也不意味着系统已使用。使用声明仍按当前状态重新核验，标记不能代替最终检查。

记忆区分项目事实与有效决策、事件经验、程序性经验。从正式完成的执行记录提取候选；完成但失败的 Run 也可提供有证据的失败经验。标注 repo、环境、版本、来源、事实有效时间和系统获知时间，以及事实、推测、摘要等证据等级。一次成功或 Agent 自述不能无条件变成通用事实。

摄入先查询相关旧记录，再判断重复、补充、替代、适用范围差异或未决冲突；保留原始来源和历史版本。相同事实可去重或增加来源，版本变化建立替代关系，不同环境分别保存，不能仅因记录较新就覆盖更可靠的事实。无法裁定的矛盾保留并在召回时说明。处理幂等与并发，索引同步可重试；摄入失败单独记录，不回滚已经提交的业务结果。

召回先按权限、项目、环境和允许共享范围过滤，再根据角色、阶段、症状和证据缺口搜索。起步使用 PostgreSQL 元数据与关键词；相关性之外还检查适用条件、失效、冲突和来源，在预算内注入经验及引用。记录使用了哪些版本；最终成功或失败只是观察证据，不能归因为每条被召回记忆的效果。

跨任务共享要求适用条件可迁移，未解决 Task 的计划和猜测不能自动污染其他任务；跨项目优先复用经验证的通用流程，专属事实保持隔离。长历史分为当前有效决策、阶段摘要、原始事件引用，按摘要定位后回查原记录，保留撤销、修正和条件变化。

验收场景包括配置更新、项目/权限隔离、时间变化、跨三次会话组合事实、缺失信息拒答、未决冲突、并发重复摄入、索引重建，以及摄入失败不影响已提交任务结果。

### 10.7 记忆对照评测与 Skills

固定模型、工具、任务、预算和评测开始时的记忆快照，对照“无长期记忆”“普通检索记忆”“带范围、来源、时间和冲突治理的记忆”。区分写入期与测试期，遵守 §10.5 的隔离。观察错误经验、过期事实、证据支持、重复探索、修复结果、耗时和成本，分别诊断摄入、候选召回、上下文选择和最终决策；不只报告 Recall@K。

Skills 是 DevFlow 的版本化程序性经验，不等于当前开发工具的插件，也不新增 LLM 角色。只封装重复且已验证的流程，例如识别测试命令、收集诊断和检查修改范围。Skill 包含版本、输入、适用条件、允许工具、步骤及完成证据，按需加载，所有操作仍经 Harness 并受 Policy 约束。

2026-11-17 首个受控 Skill：`run_registered_check` v1 由可信控制端显式调用，输入为 Run、该 Run 的 Verifier 执行及已注册命令名；元数据记录版本、适用角色、输入、允许工具、步骤和完成证据。实现复用 Worker 的 Verifier 命令入口，由现有 Coordinator/Harness 处理 Policy、执行权、预算、工作区锁与 ToolCall；Skill 不接受模型提供的命令或路径，不新增工具权限。返回的是 ToolCall 结果引用和状态，不是测试通过、正式 Verification 或独立评测结论；该入口尚未接 Agent 调度，不绑定可信 pytest 报告采集，也尚无不可变 Skill 代码版本引用或真实多任务复用证据。

2026-11-17 `run_registered_check` v2：在保留 v1 调用行为的同时，可信控制端可显式要求现有 Windows pytest 报告采集并指定受控源码相对路径。返回 ToolCall、命令摘要和 Artifact 引用，报告内容仍须由现有证据读取器验哈希、归属与格式；未采集到报告时如实返回原因。ToolCall 已持久化声明的 Skill 版本，但未固定代码哈希；未接入 Agent 调度或独立验收，报告和退出码不等于正式通过。

2026-11-17 Skill 调用记录：`run_registered_check` 的 ToolCall 请求在执行前记录可信调用方声明的名称、版本与报告采集选项，命令摘要仍按现有规则记录；被拒绝或执行结果不确定时也保留请求信息，普通非 Skill 调用不增加字段。版本标签是控制端声明，尚不构成不可变执行代码、配置或 Task 级版本快照，也不是成功证据。

2026-11-18 构造任务复用：三个隔离 Task/Run 显式调用同一 v2，分别验证不同注册命令、按 Task Policy 拒绝及跨 Run 身份拒绝；实际 ToolCall 归属各自 Run/Verifier。此结果仅证明构造测试中的复用和边界，未证明真实任务泛化或接入 Agent 调度。

2026-11-18 基线流程接线：Worker 收到已批准的 Verifier 基线交接后，通过 `run_registered_check` v2 执行已注册测试命令，复用原有补丁前检查、可选基线工作区校验、公开测试摘要、报告采集及读取校验；ToolCall 留下 Skill 名称和版本。该接线由控制端决定，不让模型选任意命令或增加工具权限；报告仍不是独立评测或正式通过，未改变正式验收门槛。Skill 版本标签仍不是不可变代码快照。

构造任务的 Windows + Docker 调度贯通测试覆盖上述基线 Skill 调用，以及独立评测通过和失败分支；模型为桩，可信侧材料由测试构造，不是新的真实任务成功或多任务泛化证据。

2026-11-18 补丁后公开测试也复用 `run_registered_check` v2，命令与报告仍由可信配置和既有验证链决定；调度贯通测试分别核对基线与补丁后 Skill ToolCall。

2026-11-18 范围 Skill 流程接线：任务包生成 Verifier 配置时携带公开 ScopeSpec；补丁后验证在范围规则受支持且与 Task 准入范围摘要一致时，使用 `check_diff_scope` v2 读取 diff 并保存诊断，正式完成后保留该诊断。旧 Task 无绑定、无范围配置或规则不受支持时沿用普通 diff；v2 将自身无法比较的变更路径标为 `unsupported_path`，范围检查仅作为诊断，不改变正式验收条件。摘要绑定不证明任务包本身可信，范围结果也不是工作区权限隔离。

2026-11-17 `check_diff_scope` v1：可信控制端显式提供 ScopeSpec，Skill 通过既有 Verifier git_diff、Policy、租约和工作区锁读取经哈希校验的 diff/numstat，再按大小写敏感的字面文件或以 `/` 结尾的目录规则检查所有记录的路径，禁止规则优先；通配符、绝对路径、反斜杠及路径穿越等不支持并拒绝执行。ToolCall 在执行前记录 Skill 声明与规范化范围摘要；既有证据读取器只接受旧参数格式或严格的 v1 新格式。结果为相对所声明范围的诊断，不是工作区隔离或正式验收；范围由可信调用方提供、摘要不证明该范围来自任务包，子模块、外部并发修改等沿用既有 diff 限制。未接 Agent 自动调度或不可变执行版本。

2026-11-17 `check_diff_scope` v2：新建 benchmark Task 准入证据记录任务包 ScopeSpec 的规范化 SHA-256；v2 在执行前核对目标 Run 所属 Task 的唯一 pack 标记与范围摘要，并在 Coordinator 执行前及返回后重新核对，缺失或不匹配拒绝。历史 Task 不自动回填，v1 仍仅依赖可信调用方提供范围。此绑定只证明调用时读取的准入声明与范围摘要一致，不能证明任务包可信、执行版本固定，或排除外部直接修改/短暂篡改；Task 的准入声明和独立验收仍依赖既有可信控制边界。

2026-11-17 范围绑定准备与领取：Worker 和 benchmark 入口在已有范围摘要时核对任务包 pack ID 和 ScopeSpec 摘要，准备后在 Task 行锁事务中复核完整准入记录及基线提交；不匹配则拒绝新建 Run。旧 Task 无摘要仍按旧流程处理，既不自动回填也不获得 v2 范围绑定证明；由于旧格式兼容，摘要被删除与原本没有摘要无法区分。本检查只绑定范围和基线，不固定整个任务包文件，也不能证明可信来源或抵御持久层外部篡改。

验收要求在多个适用任务中可复用，条件不满足时拒绝套用；单个案例成功不证明泛化。Skill 内容不能夹带隐藏答案或借流程复用扩大权限。

### 10.8 评测驱动优化与受控 RSI

设置内外两条循环：内层完成获准仓库的 Issue 修复；外层由 EvolutionRunner 管理 DevFlow AI 自身的候选改进实验。首个对象限于 ContextBuilder 的日志片段与证据选择策略；五个 Agent 可复用其既有诊断、计划、实现和验收职责。

启动门槛是工作包 E 的真实执行、独立验收、恢复与可重复评测已有证据，并具备版本追踪、实验隔离、资源上限和回退能力。无需等待完整 MemoryService/Skills，也不要求先训练模型。阶段内确定实验候选数、总模型用量、总时长、停止条件和晋升阈值；没有这些上限不得运行自动循环。

在候选生成前固定 V0、允许修改的组件、任务划分、正确性/成本/回归门槛及总实验预算。一次实验只改变一个因素，保持模型、工具、任务与预算口径稳定。首轮步骤为：基线执行 → 从允许查看的轨迹提出改进任务 → 独立工作区生成 V1 候选 → 单元/集成与开发任务检查 → 独立回归和未参与优化的任务评测 → 登记接受或拒绝理由与可回退版本 → 让接受的 V1 参与下一轮改进，记录 V2 及谱系。

候选代码在单独工作区和进程运行，实验数据库/正式记录与主开发业务隔离或显式分区，实验指标单独统计。候选无权修改隐藏测试、评分与晋升控制、权限边界、生命周期不变式或放宽硬预算。独立工作区本身不构成安全沙箱，继续限制为可信代码与受控环境。评测控制端按预先固定条件决定候选资格；改进 Task 仍遵守 §1 的验证后等待人工审核流程，不能用候选晋升绕过 HumanReview 或由候选自述宣布成功。

通过评测并按既有审核规则接受的新版本，只供后续新 Task 选择。`policy_snapshot` 只固定预算与权限，不能单独固定可执行代码；实验上线前须补充不可变执行版本引用，能够定位代码、配置、模型、提示词、上下文策略和已启用的 Skill/记忆快照等依赖。同一 Task 的后续 Run 使用其固定版本，不热切换候选。具体存储字段、版本加载与不可用时的处理在对应实现步骤推导；当前 Task 模型尚不具备这些机制。

按需引入 CandidateVersion、EvolutionExperiment、EvaluationResult 等概念，记录 parent_version、目标组件、补丁、代码与配置哈希、模型、任务包、记忆快照、预算、完整结果及接受/淘汰理由。工程组件产生的报告/摘要若复用 Artifact，要先解决其归属，不能伪造 AgentExecution 来满足现有外键。

分别报告候选完成修复任务的能力、产生有效自身改进的能力、单任务成本，以及整个候选搜索、评测和递归过程的累计成本。保留失败候选与无收益结果。验收包括 V0、候选差异、独立测试、接受/拒绝理由、V1 参与后续改进的真实轨迹及可回退版本；增加重试或追加记忆不能代替递归改进证据。

允许经过验证的新版本参与下一轮改进，构成本项目受控 RSI 实验；使改进机制自身可编辑属于更后续研究，需要另行方案。不得预设持续变强或把研究论文指标当作项目结果。

### 10.9 条件性增强与待细化项

| 能力 | 启动条件 |
|---|---|
| 动态 Tool Search | 工具规模增长后出现可测的选择错误或定义占用问题。 |
| Code Mode / 程序化工具调用 | 多工具批处理、中间结果或调用延迟形成实际瓶颈。 |
| 并行探索 | 串行闭环稳定，存在可拆分的只读任务；验证合并、权限与累计成本。 |
| 图关系检索 | 普通检索难以处理实际关联问题，先进行对照实验。 |
| MCP / A2A | 分别出现外部工具集成或独立部署 Agent 服务的互操作需求。 |
| 多模态 RAG | 代码任务确需截图、图或扫描文档；优先在独立 RAG 项目实验。 |
| 本地推理优化、微调、Agentic RL | 已有成本/延迟瓶颈或足够轨迹和可靠训练信号，提出可验证收益假设。 |
| 实时语音、通用 Computer Use | 当前代码修复主线无明确需求，继续后置。 |

V1 暂缓范围沿用 §9。待对应阶段推导的内容包括：输出修复限额、执行版本存储、记忆/实验实体与产物归属、索引选型、实验总预算和晋升门槛。列入路线不表示这些细节已锁定或需要本轮创建数据库表。

### 10.10 方法参考与项目采用边界

升级路线来源为用户于 2026-10-25 提供的《DevFlowAI_Frontier_Integration_Roadmap_20261025.md》。该文件作为设计输入按差异合并；SPEC 是正式设计来源，完整路线及隐藏评测材料不整体传给运行中的修复 Agent。

- [Anthropic：Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)：参考按需获取上下文、压缩和持久笔记等方法；本项目仍按角色和证据职责自行实现。
- [Sakana AI：Darwin Gödel Machine](https://sakana.ai/dgm/)：参考自身代码修改与下游任务评测交替、保留候选谱系的实验思路；本项目首轮是范围更小的上下文选择实验，未复现论文系统或性能。

附件中的 Graphiti、LongMemEval 等可在记忆阶段进一步核验作为实现或评测参考；当前未选定其依赖、存储后端或数据集。Hyperagents 等改进机制演化研究留作后续参考，不扩大本轮 RSI 的修改权限。
