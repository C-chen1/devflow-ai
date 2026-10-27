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
| `title` | TEXT | issue 标题；不允许 `NULL`，由数据库 `NOT NULL` 约束保证所有写入入口均遵守；该约束不拒绝空字符串 |
| `description` | TEXT | issue 内容；不允许 `NULL`，由数据库 `NOT NULL` 约束保证；该约束不拒绝空字符串，也不保证内容充分或任务可修复，准入仍由 Supervisor 判断 |
| `repo` | TEXT | 目标仓库；不允许 `NULL`，由数据库 `NOT NULL` 约束保证；该约束不拒绝空字符串，也不保证仓库地址有效、可访问或已获准处理 |
| `status` | 枚举（9 值） | 当前状态，不允许 `NULL`；枚举约束合法取值，状态转换合法性由应用服务中的生命周期控制检查，见 §1、§7 |
| `policy_version` | TEXT | 创建 Task 时固定的 Policy 版本；不允许 `NULL`，由数据库 `NOT NULL` 约束保证；该约束不拒绝空字符串，也不保证版本号与快照内容一致 |
| `policy_snapshot` | JSONB | 创建 Task 时固定的完整预算与权限配置；不允许 SQL `NULL`；SQLAlchemy 使用 `JSONB(none_as_null=True)` 将 Python `None` 按 SQL `NULL` 处理。类型转换与非空约束不保证快照结构和配置项完整 |
| `created_at` | TIMESTAMPTZ | 创建时间；不允许 `NULL`；配置 `server_default=func.now()`，插入时省略该列，由 PostgreSQL 提供当前事务时间 |
| `updated_at` | TIMESTAMPTZ | 最后更新时间；不允许 `NULL`；插入时以 `server_default=func.now()` 初始化，SQLAlchemy 构造更新语句且未显式赋该列时，以 `onupdate=func.now()` 刷新；原生 SQL 更新须显式维护该列 |

Task 创建后的初始状态为 `PENDING`。`status` 列配置数据库默认值 `PENDING`（SQLAlchemy `server_default`），在插入时省略该列的情况下由 PostgreSQL 补值；默认值不覆盖显式提供的状态。Task 创建应用服务仍须固定初始状态为 `PENDING`，不接受客户端指定初始状态。

Task 的两个时间字段使用 PostgreSQL 带时区时间类型 `TIMESTAMPTZ`（SQLAlchemy `DateTime(timezone=True)`）。Python 侧使用带时区的 `datetime`，应用内部统一按 UTC 表示和比较，展示时按需转换为本地时间。

#### Run（实体表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `task_id` | UUID（FK） | 挂在哪个 Task |
| `attempt_number` | INTEGER | 第几次尝试 |
| `status` | 枚举（3 值） | RUNNING / PASSED / FAILED |
| `creation_idempotency_key` | TEXT | 同一 Task 内创建请求重发时定位原 Run，受唯一约束保护 |
| `worker_id` | TEXT | 当前执行归属；租约阶段使用 |
| `last_heartbeat_at` | TIMESTAMP | 最近一次有效心跳；租约阶段使用 |
| `lease_expires_at` | TIMESTAMP | 执行权到期时间；租约阶段使用 |
| `started_at` | TIMESTAMP | Run 实际开始时间 |
| `finished_at` | TIMESTAMP | Run 进入终态的时间 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 最后更新时间 |

#### AgentExecution（实体表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `run_id` | UUID（FK） | 挂在哪个 Run |
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
| `agent_execution_id` | UUID（FK） | 挂在哪个 AgentExecution |
| `tool_name` | TEXT | 哪个工具（git_clone / read_file / run_command） |
| `args` | JSONB | 传的参数 |
| `status` | 枚举 | requested / running / succeeded / failed / denied / timed_out / unknown_result |
| `stdout_excerpt` / `stderr_excerpt` | TEXT | 输出片段；较大原始内容保存为 Artifact |
| `exit_code` | INTEGER（可空） | 命令明确结束时的退出码；未知结果不可伪造 |
| `duration_ms` | INTEGER（可空） | 可确认的执行耗时 |
| `reason` | TEXT（可空） | 拒绝、超时、未知结果等原因 |
| `created_at` | TIMESTAMP | 创建时间 |
| `updated_at` | TIMESTAMP | 最后更新时间 |

#### Artifact（记录表）

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID | 主键 |
| `agent_execution_id` | UUID（FK） | 谁产的 |
| `kind` | TEXT | analysis / plan / patch |
| `content_ref` | TEXT | 文件路径（内容存文件，不存 DB） |
| `content_hash` | TEXT | 文件内容哈希，用于完整性核对和跨 Run 引用 |
| `created_at` | TIMESTAMP | 创建时间 |

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

保留每个失败 Run 可以完整追踪每次尝试的 Agent 执行、工具调用、产物、验证证据和失败指纹，也能可靠计算重试次数并识别没有信息增量的重复失败。

---

## 5. Agent 契约与决策权

Agent 契约位于图编排与具体 Agent 执行之间，规定每个角色可以自主决定什么、不得决定什么，以及越界或证据不足时如何交接。它的目标不是限制 Agent 的专业判断，而是防止职责漂移、修改范围失控和无法追踪的隐式决策。

### 5.1 通用控制原则

1. **专业决策权与流程控制权分离**：Explorer、Planner、Coder、Verifier 分别对探索、计划、实现和验收作专业判断；Supervisor 负责准入、分类、恢复动作、路由和人工升级，不取代其他角色生成专业产物。
2. **最小授权**：Agent 只在当前 Issue、上游证据、已批准计划和策略允许的范围内自主决策。改动大小不是是否越权的判断标准；未授权的一行修改也可能越权，得到批准的多文件修改则可以执行。
3. **越界前停止**：Agent 一旦发现继续工作需要改变目标、计划、授权范围或取代其他角色的专业判断，必须在执行越权动作前停止并提交证据。
4. **证据先于交接**：Agent 不能只声称“计划错误”或“无法完成”，必须提供文件位置、调用关系、工具输出、产物引用或其他可复查事实。证据不足或上游产物互相矛盾时，不允许靠猜测继续执行。
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
| `max_retries_per_task` | 3 | 最多 3 个额外 Run，总尝试最多 4 次 |
| `max_same_signature_occurrences` | 3 | 同一 Task 内相同失败指纹的累计次数 |
| `max_rediagnose_attempts` | 2 | `UNKNOWN_FAILURE` 的重新诊断次数 |
| `max_graph_steps_per_run` | 30 | 已记录的 AgentExecution 次数，包括 Supervisor |
| `max_tool_calls_per_run` | 60 | 已持久化的 ToolCall 请求数，包括拒绝、超时和失败请求 |
| `max_llm_tokens_per_run` | 80000 | Run 内所有模型调用的输入与输出 token 总和 |
| `tool_call_timeout_seconds` | 300 | 单次工具调用最长时间，仍受 Run 剩余时长限制 |
| `max_run_duration_seconds` | 1800 | 单个 Run 的最长执行时间 |

这里的 graph step 是项目定义的 AgentExecution 计数，不直接等于 LangGraph 内部 super step。一个 AgentExecution 可以包含多个模型请求与 ToolCall。ToolCall 的计数和是否真正执行分别记录。模型服务未返回 usage 时标明估算或未知，不当作零。

### 6.3 检查、审计与版本

- 创建新 Run 前核对 Task 的预算、重复指纹、重新诊断和合规方案。Agent 节点、模型请求和工具调用前检查各自额度。
- 工具执行期间监控超时，Run 执行期间监控总时长；Run 到期要停止继续执行，不能只在结果返回后标记超额。
- 模型请求限制可用输出额度并设置超时。每次判断保存所用配置、计数、证据引用和理由。
- Run 数量用于计算额外尝试数；AgentExecution 提供节点和 token 用量；ToolCall 提供请求数与耗时；Run 起止时间提供时长；FailureRecord 提供指纹及重新诊断历史。
- Task 创建时保存 `policy_version` 和完整 `policy_snapshot`。同一 Task 的所有 Run 使用该快照；修改系统默认配置只影响后续 Task。

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

### 7.2 原子领取与并发约束

Task 首次创建可以独立提交，此时没有 Run 是合法状态。worker 领取已有 `ELIGIBLE` Task，或符合恢复条件的 `RETRYING` Task 时，在短事务中以 `SELECT ... FOR UPDATE SKIP LOCKED` 取得候选，重新核对状态和 Policy，创建 Run、记录执行归属与租约，并把 Task 转为 `RUNNING` 后一次提交。任一步失败则整体回滚。

Run 保留 `UNIQUE(task_id, attempt_number)`；以 `status = 'RUNNING'` 为条件，对 `task_id` 建部分唯一索引，保证一个 Task 最多一个活跃 Run。Run 创建请求保存稳定的 `creation_idempotency_key`，在同一 Task 范围内用唯一约束防止重发造成重复 Run。Task 创建 API 的重复提交需要单独设计幂等键，不能借用 Run 的键。

### 7.3 Agent 与工具的短事务

Agent 产物先写到唯一文件位置并计算哈希。Artifact、Handoff、Evidence 引用和 AgentExecution 完成状态在同一 PostgreSQL 事务提交；提交成功后才执行正式路由。数据库回滚无法撤销文件，未获正式记录确认的孤立文件不自动成为有效产物。

ToolCall 使用两个短事务：先持久化稳定 ID、请求及初始状态；在事务外执行受控工具；再检查执行权，保存最终状态、stdout、stderr、退出码和产物引用。被拒绝的请求也记录，且按第 6 节口径计入工具预算。不能把数据库事务一直保持到 Shell 或模型调用结束。

执行中进程崩溃时，记录无法证明命令是否启动或完成。租约失效后，相关 ToolCall 标记 `UNKNOWN_RESULT`，原因记为 `WORKER_LEASE_EXPIRED`；这两者不是新的 Run 失败类别。不能伪造退出码，也不能默认重放有副作用的命令。

### 7.4 worker 租约与恢复

Run 保存 `worker_id`、`last_heartbeat_at` 和 `lease_expires_at`。worker 在执行工具前检查执行权，在提交结果的事务中再次确认 Run 仍为 `RUNNING`、worker 身份匹配且租约有效。旧 worker 的迟到写入不能覆盖终态 Run 或新 Run。

租约到期后，恢复流程结束旧 Run 并保存 FailureRecord，再由 Policy 判断创建新 Run 或升级人工。数据库租约本身不能停止操作系统进程；Harness 还需监控和清理已启动的子进程。先以单 worker 打通链路，随后通过故障注入验证租约、心跳、多 worker 竞争和旧 worker 拒写；未经测试不得声称具备崩溃恢复能力。

### 7.5 Run 工作区、ContextBuilder 与循环

每个 Run 使用独立工作区、临时目录、缓存和测试输出，从 Task 固定的基准 commit 建立干净副本。上一 Run 的正式历史可以作为证据引用；残留工作区不自动继承。复用旧补丁须有明确恢复计划，校验 Artifact 哈希，通过 Harness 在新工作区应用并留下新审计记录。

ContextBuilder 根据 task_id、run_id 和目标角色，从正式记录中筛选、排序、裁剪上下文，并为事实附来源。公共信息包括 Issue、验收标准、基准 commit、授权范围、Policy 快照和剩余额度；专业 Agent 只获得各自工作所需证据，Supervisor 获得正式完成情况、失败历史及 Policy 判断。完整日志留在 Artifact，模型上下文使用必要摘要和引用；隐藏验收源码及参考修复不进入修复 Agent 上下文。

区分三种循环：单个 AgentExecution 内的工具循环、当前 Run 内 Supervisor 通过 LangGraph 的角色调度、旧 Run 失败后的跨 Run 恢复。正式角色交接须先持久化，再继续路由。

Git 工作区和命令白名单提供受控操作边界，但不能隔离任意 Python 程序的全部能力。V1 只处理经人工准入的可信仓库和受控测试环境；数据库或应用容器化不等于工具执行沙箱。

## 8. Benchmark 与任务包

Click 为首选目标仓库，Flask 为备选。首个候选是 Click #3019，交接材料描述为 `click.prompt` 与 `click.confirm` 的空 `prompt_suffix` 行为；具体问题、基准和可执行验收均待实际核验。先完成一项真实任务闭环，再扩展到目标六项。测试替身只用于验证系统链路，不计真实修复成功。

每个任务包保存版本、许可证与来源、修复前 base commit 完整哈希、运行环境和依赖、净化后的问题描述、可观察错误与正确行为、允许和禁止的修改范围、公开测试命令、重复执行的基准结果、Policy 与模型配置，以及评测器掌握的隐藏验收版本或哈希。未核验的值标为待核验，不补造。

修复 Agent 只看到修复前源码、净化问题、公开成功标准、授权范围、安装方法、公开测试及基准证据。参考补丁、后续修复 commit、解法记录和隐藏验收源码由独立评测端保管；工具可访问范围也落实隔离。隐藏测试应在缺陷基准失败、在已知正确版本通过，并检查相关正常行为。验收针对行为，不要求补丁与参考实现一致。

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

环境阶段先核定项目运行 Python 版本和依赖版本，benchmark 的目标仓库 Python 版本单独管理；不静默升级到最新。基础结构化日志在可用时携带 `request_id`、`task_id`、`run_id`，后续补 AgentExecution 和 ToolCall 标识；日志不得输出数据库口令或模型密钥。

API 与 Alembic 复用项目的数据库连接配置读取逻辑，由 `builder_database_url()` 读取当前环境的五项 `DEVFLOW_DB_*` 配置；迁移不在 `alembic.ini` 中另行硬编码真实连接地址或凭据。不同环境可提供不同配置值，复用配置读取逻辑不要求各环境的数据或配置值相同。

最小 Harness 只接受结构化工具名与受约束参数。读、搜、diff、补丁和测试命令按角色和计划范围授权；命令使用注册模板与参数约束，不接收任意 Shell 字符串。执行前解析实际路径并校验允许根目录和符号链接，输出过大时保存 Artifact 及截取片段，超时或 Run 停止后清理相关子进程。

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

解析或契约失败允许有限次重新生成，记录原始输出引用、校验错误、修复次数及用量。具体修复次数上限和配置键在实现该步时推导并纳入 Policy 快照；所有模型请求、AgentExecution 与工具请求仍按 §6 计量，不允许隐藏的无限修复循环。恢复路线沿用 `AGENT_CONTRACT_FAILURE`；只有 Run 已失败时才写 FailureRecord，运行中的校验事件不能伪装成已失败 Run 的记录。未经有效校验的 Supervisor 输出不得直接触发路由、创建 Run 或修改数据库。

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

V2 保留现有九张业务表，不创建重复的 Evidence 真相。按实际查询和事务需要考虑 MemoryItem、MemoryRevision、MemoryUse：分别表达经验身份/范围/内容/来源/条件/时间/状态、修订与冲突历史、具体使用版本和观察结果。MemoryRevision 可以先并入版本化 MemoryItem；这些是候选概念，尚未锁定表结构或创建迁移。

记忆区分项目事实与有效决策、事件经验、程序性经验。从正式完成的执行记录提取候选；完成但失败的 Run 也可提供有证据的失败经验。标注 repo、环境、版本、来源、事实有效时间和系统获知时间，以及事实、推测、摘要等证据等级。一次成功或 Agent 自述不能无条件变成通用事实。

摄入先查询相关旧记录，再判断重复、补充、替代、适用范围差异或未决冲突；保留原始来源和历史版本。相同事实可去重或增加来源，版本变化建立替代关系，不同环境分别保存，不能仅因记录较新就覆盖更可靠的事实。无法裁定的矛盾保留并在召回时说明。处理幂等与并发，索引同步可重试；摄入失败单独记录，不回滚已经提交的业务结果。

召回先按权限、项目、环境和允许共享范围过滤，再根据角色、阶段、症状和证据缺口搜索。起步使用 PostgreSQL 元数据与关键词；相关性之外还检查适用条件、失效、冲突和来源，在预算内注入经验及引用。记录使用了哪些版本；最终成功或失败只是观察证据，不能归因为每条被召回记忆的效果。

跨任务共享要求适用条件可迁移，未解决 Task 的计划和猜测不能自动污染其他任务；跨项目优先复用经验证的通用流程，专属事实保持隔离。长历史分为当前有效决策、阶段摘要、原始事件引用，按摘要定位后回查原记录，保留撤销、修正和条件变化。

验收场景包括配置更新、项目/权限隔离、时间变化、跨三次会话组合事实、缺失信息拒答、未决冲突、并发重复摄入、索引重建，以及摄入失败不影响已提交任务结果。

### 10.7 记忆对照评测与 Skills

固定模型、工具、任务、预算和评测开始时的记忆快照，对照“无长期记忆”“普通检索记忆”“带范围、来源、时间和冲突治理的记忆”。区分写入期与测试期，遵守 §10.5 的隔离。观察错误经验、过期事实、证据支持、重复探索、修复结果、耗时和成本，分别诊断摄入、候选召回、上下文选择和最终决策；不只报告 Recall@K。

Skills 是 DevFlow 的版本化程序性经验，不等于当前开发工具的插件，也不新增 LLM 角色。只封装重复且已验证的流程，例如识别测试命令、收集诊断和检查修改范围。Skill 包含版本、输入、适用条件、允许工具、步骤及完成证据，按需加载，所有操作仍经 Harness 并受 Policy 约束。

验收要求在多个适用任务中可复用，条件不满足时拒绝套用；单个案例成功不证明泛化。Skill 内容不能夹带隐藏答案或借流程复用扩大权限。

### 10.8 评测驱动优化与受控 RSI

设置内外两条循环：内层完成获准仓库的 Issue 修复；外层由 EvolutionRunner 管理 DevFlow AI 自身的候选改进实验。首个对象限于 ContextBuilder 的日志片段与证据选择策略；五个 Agent 可复用其既有诊断、计划、实现和验收职责。

启动门槛是工作包 E 的真实执行、独立验收、恢复与可重复评测已有证据，并具备版本追踪、实验隔离、资源上限和回退能力。无需等待完整 MemoryService/Skills，也不要求先训练模型。阶段内确定实验候选数、总模型用量、总时长、停止条件和晋升阈值；没有这些上限不得运行自动循环。

在候选生成前固定 V0、允许修改的组件、任务划分、正确性/成本/回归门槛及总实验预算。一次实验只改变一个因素，保持模型、工具、任务与预算口径稳定。首轮步骤为：基线执行 → 从允许查看的轨迹提出改进任务 → 独立工作区生成 V1 候选 → 单元/集成与开发任务检查 → 独立回归和未参与优化的任务评测 → 登记接受或拒绝理由与可回退版本 → 让接受的 V1 参与下一轮改进，记录 V2 及谱系。

候选代码在单独工作区和进程运行，实验数据库/正式记录与主开发业务隔离或显式分区，实验指标单独统计。候选无权修改隐藏测试、评分与晋升控制、权限边界、生命周期不变式或放宽硬预算。独立工作区本身不构成安全沙箱，继续限制为可信代码与受控环境。评测控制端按预先固定条件决定候选资格；改进 Task 仍遵守 §1 的验证后等待人工审核流程，不能用候选晋升绕过 HumanReview 或由候选自述宣布成功。

通过评测并按既有审核规则接受的新版本，只供后续新 Task 选择。`policy_snapshot` 只固定预算与权限，不能单独固定可执行代码；实验上线前须补充不可变执行版本引用，能够定位代码、配置、模型、提示词、上下文策略和已启用的 Skill/记忆快照等依赖。同一 Task 的后续 Run 使用其固定版本，不热切换候选。具体存储字段、版本加载与不可用时的处理在对应实现步骤推导；当前九字段 Task 模型尚不具备这些机制。

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

V1 暂缓范围沿用 §9。待对应阶段推导的内容包括：输出修复限额、Task 提交幂等契约、执行版本存储、记忆/实验实体与产物归属、索引选型、实验总预算和晋升门槛。列入路线不表示这些细节已锁定或需要本轮创建数据库表。

### 10.10 方法参考与项目采用边界

升级路线来源为用户于 2026-10-25 提供的《DevFlowAI_Frontier_Integration_Roadmap_20261025.md》。该文件作为设计输入按差异合并；SPEC 是正式设计来源，完整路线及隐藏评测材料不整体传给运行中的修复 Agent。

- [Anthropic：Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)：参考按需获取上下文、压缩和持久笔记等方法；本项目仍按角色和证据职责自行实现。
- [Sakana AI：Darwin Gödel Machine](https://sakana.ai/dgm/)：参考自身代码修改与下游任务评测交替、保留候选谱系的实验思路；本项目首轮是范围更小的上下文选择实验，未复现论文系统或性能。

附件中的 Graphiti、LongMemEval 等可在记忆阶段进一步核验作为实现或评测参考；当前未选定其依赖、存储后端或数据集。Hyperagents 等改进机制演化研究留作后续参考，不扩大本轮 RSI 的修改权限。
