# Task 审核状态转换：设计（待 Codex 审查，未实现、未迁移）

当前事实（只读核对）：`TaskStatus.AWAITING_REVIEW` 只在 `finalize_passing_verification` 写入；`TaskStatus.COMPLETED`
在枚举里存在但全仓库没有任何赋值；`services.py` / `api.py` / `repositories.py` 没有审核入口。人工审核结论目前只写在
运行记录的 `human_review` 字段里，与 Task 行没有关联——所以"审核通过"在数据库里不可见，也无法阻止对同一 Task 反复提交不同结论。

本文原文只给设计；**V1 已按下面第 2–4 节实现（迁移文件已写、未执行）**，实现差异见文末「实现记录」。本文不改任何现有数据库。

## 1. 目标

审核必须成为一条可审计的事实，而不是记录文件里的自由文本：谁、在有 UTC 时间戳的时刻、对哪一个 Task 的哪一条通过做了结论，
依据是哪一份记录（及其摘要）与哪一个正式 Verification；并且 Task 状态随之一致地前进一次，重复提交不能产生第二种结论。

## 2. 数据模型（建议：新表 + 少量列）

新增 `task_reviews`（追加式，不覆盖历史）：

| 列 | 说明 |
| --- | --- |
| `id` | UUID 主键 |
| `task_id` | 外键 → `tasks.id`，索引 |
| `run_id` | 被审核的 Run，必须属于该 Task 且状态 `PASSED` |
| `verification_id` | 该 Run 的正式 Verification，必须 `trusted_v1 / passed` |
| `record_sha256` | 被审核记录文件的 sha256（允许是修正副本；修正副本自身带 `correction` 链） |
| `conclusion` | `accepted` / `rejected` |
| `reviewer` | 审核人标识（字符串，来源见 §6 待定问题） |
| `reviewed_at` | 数据库时间或调用方提供的 UTC 时间，二选一需明确 |
| `supersedes_id` | 可空；指向被本条取代的旧结论（用于撤回/改判），不使用则留空 |

`tasks` 只加一个"当前结论指针"列（如 `current_review_id`）或在查询时取最新一行；**不**把 reviewer/时间复制到 `tasks`，
避免两处真相。`Task.status` 仍只有 `AWAITING_REVIEW → COMPLETED | REJECTED` 两条合法边。

## 3. 事务与不变量

单事务内完成，持 `tasks` 行锁（`SELECT ... FOR UPDATE`）：

1. Task 必须存在且 `status = AWAITING_REVIEW`，否则 `TaskStateConflictError`（重复审核、或任务根本没在等审核）；
2. Run 必须属于该 Task 且 `status = PASSED`；Verification 必须属于该 Run 且 `evidence.scope = trusted_v1`、`result = passed`；
3. 记录文件必须可读，其 `task_id`/`run_id`（若存在）与目标一致；`record_sha256` 与提交时给出的一致；
4. 若该 Task 已有同一 `record_sha256` 的结论 → **幂等成功**，不写第二行、不改状态；
5. 若已有不同记录的结论 → 拒绝（`TaskAlreadyReviewedError`），除非调用方显式声明 `supersedes_id` 并走"改判"路径；
6. 插入 `task_reviews` 行并更新 `Task.status`（accepted → `COMPLETED`，rejected → `REJECTED`），同一事务提交。

跨表不变量：`tasks.status = COMPLETED` 当且仅当存在一条 `accepted` 且未被取代的 review；`REJECTED` 同理。

## 4. 拒绝与改判语义

- 拒绝：`AWAITING_REVIEW → REJECTED`，Task 终止；不会因此启动新的 Run，也不改变记录的 `outcome`。
- 改判（accepted ↔ rejected）：只允许通过"新 review 行 + `supersedes_id`"完成，旧行保留；是否允许由 §6 决定。
- 任何情况下都不删除历史 review 行，也不改写记录文件；记录侧仍是 `review`/`correct` 生成的新文件。

## 5. 幂等与并发

- 幂等键：`(task_id, record_sha256)`。重复提交同一份记录返回既有结果，不产生第二行。
- 并发：两个审核者同时提交 → 行锁串行化，第二个要么幂等命中，要么因已有不同结论被拒。
- 崩溃：单事务保证"插入行 + 改状态"同时生效或同时不生效。

## 6. 需要 Codex 决定的问题

1. **审核人身份来源**：字符串自由填写，还是必须来自受控身份（例如环境变量/帐号表）？V1 记录里 `human_review.reviewer`
   已经是自由字符串；是否要求两者一致（记录里的 reviewer == review 行的 reviewer）？
2. **时间来源**：用数据库时间（与其它状态转换一致）还是调用方提供的 UTC 字符串（与记录字段一致）？两者并存时以谁为准？
3. **是否允许改判**：`COMPLETED → REJECTED` 这类反转要不要支持？如支持，是否需要额外的"撤销/重开"动作而不是 `supersedes`。
4. **记录与 Task 的绑定强度**：是否要求 `record_sha256` 必须是"当前最新"的那份（例如存在修正副本时必须交修正副本）？
5. **迁移时机**：新增表 + 列属于 Alembic 迁移，按约定需要你批准后由你执行；在此之前 Task 保持 `AWAITING_REVIEW`。
6. **入口形态**：独立 CLI（`benchmark_record review --task … --transition`）还是控制端命令（`control --review-task …`）？
   我倾向独立命令，避免把审核与恢复调度混在一起。

## 7. 迁移草案（仅说明，不执行）

一个 revision：`create table task_reviews (...)` + 索引 `(task_id, reviewed_at)` + 可空列 `tasks.current_review_id`；
`downgrade` 删除同名列/表。历史 Task 保持原状态，不回填、不推断。


---

## 实现记录（V1，2026-11-13）

**已实现**：`task_reviews` 表（`migrations/versions/b7f2c31d8a90_create_task_reviews.py`，未对任何数据库执行）、
`models.TaskReview`、`services.TaskReviewService.record(...)`、CLI `python -m devflow_ai.task_review`。

落地时的取舍与原文不同的地方，如实记在这里：

1. **不加 `tasks.current_review_id`**。Task 的当前状态由 `tasks.status` 表达，评审行本身是追加式证据；
   再加一列指针就是第二个真相来源，一旦不一致反而无法判定。查询"这个 Task 的评审"按 `task_id` 取即可。
2. **不加 `supersedes_id`**。V1 不支持改判（原文 §4），留一个永不使用的列只会让人以为支持。
3. **幂等键落在数据库上**：`UniqueConstraint(task_id, record_sha256)`。服务层在行锁内先查这条，
   相同摘要 + 相同结论 → 幂等返回（不写第二行、不改状态）；相同摘要 + 不同结论，或该 Task 已有别的记录的评审 → `TaskReviewConflictError`。
4. **状态**：`AWAITING_REVIEW` → `COMPLETED`（accepted）/ `REJECTED`（rejected），两者都是既有枚举值；
   `tasks.status` 与评审行在同一事务内提交，失败则一起回滚。
5. **证据绑定**：Run 必须属于该 Task 且 `PASSED`；Verification 必须属于该 Run、`result=passed` 且 `evidence.scope=trusted_v1`；
   记录摘要由 CLI 按记录文件字节计算。任一不满足 → `TaskReviewEvidenceError`，不写任何行。
6. **时间来源**：`reviewed_at` 是记录里审核人自己写的时间（调用方提交，非空），`created_at` 是入库时数据库的 `now()`；
   两者都保留，不互相覆盖。
7. **审核人**：CLI 用 `--reviewer` 与记录里的 `human_review.reviewer` 核对，二者必须相等且非空；两边都比较去掉首尾空白后的值，并且提交给服务的就是这个形式，否则重复提交会被自己的空白差异判成冲突；
   服务层再要求非空，避免空标识写进证据。

**仍未做（保留为设计问题）**：改判 / 历史回填 / 自动审核（原文 §6 的第 1–3 问仍待定：审核人身份来源、时间以谁为准、
是否允许改判）；`COMPLETED` 之后没有任何后续状态转换。首次真实接入前需由 Codex 决定是否补这些。

**测试**（`tests/test_task_review.py`，隔离 schema 内自建表，未触碰真实数据库）：正常接受/拒绝、幂等、结论冲突、
记录摘要冲突、Run 未通过、Verification 非 trusted_v1、Verification 属于别的 Run、Task 不在 AWAITING_REVIEW、
不可用的提交（空审核人/空时间/坏摘要/未知结论）、写入后异常回滚、两个审核者并发提交只产生一行；以及 CLI 侧
记录文件不匹配、缺 id、id 与记录矛盾等用例。

**实现后的三处收紧（2026-11-13，Codex 复验意见）**：

- CLI 的 `--task-id`/`--run-id`/`--verification-id` 只能补缺，不能覆盖：与记录里已有的值不一致即退出 2，
  （包括 `formal.verification_id`——此前它可以被覆盖，等于让调用方换掉记录绑定的正式验收证据）。
- 记录**只读一次**：`parse_run_record(bytes)` 与 `sha256(bytes)` 用同一份字节，`load_run_record(path)` 变成
  “读字节 + 解析”，避免两次读取之间文件被替换。
- 幂等分支比较整份提交：同摘要下 Run、Verification、结论、审核人、审核时间任一处不同都报冲突并列出字段名，
  不是只看 `conclusion`。

**最后一项证据一致性（2026-11-13）**：服务在同一事务里比较记录声明的 `formal.independent_suite_version` 与
所引用 `Verification.evidence.suite_version`——两者都必须存在且相等，否则 `TaskReviewEvidenceError`，不写评审行、不动 Task 状态。
该检查排在幂等分支**之前**，所以重复提交不会因为“上次成功过”而绕过它；版本值由 CLI 从记录里显式传入服务。

**注意边界**：版本一致只说明“审核人看的那份记录与被引用的正式验收声明同一个套件版本”，
**不等于**重新核验了独立回执（回执归属、哈希与校准仍由正式评测与之前的验收负责）。
