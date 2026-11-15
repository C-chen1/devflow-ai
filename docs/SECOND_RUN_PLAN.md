# 第二次真实运行：准备与执行方案

首轮结果是 `incomplete`，不是通过也不是失败：5 个阶段、19 秒、79,511 token（Supervisor 34,690 /
Explorer 21,155 / Planner 23,666，Codex 从隔离库的实际 usage 行核对），停在 `BLOCKED` /
`Model budget unavailable`，未进入 Coder 与 Verifier。本轮只做只读排查与准备，不启动第二次运行。

## 1. 首轮遗留状态（只读事实）

| 项 | 值 |
| --- | --- |
| Task | `e777f9ee-7adb-43eb-a50c-08cfdadae9ff` |
| Run | `87dfb823-999a-42af-892d-31420535c486` |
| 记录文件 | `D:/python/.devflow-test-temp/benchmark-formal-7b5143ecf3d64c7281f20878597df68e/record.json` |
| 记录结论 | `incomplete`（不是 `failed`，也不是正式通过） |
| Run 状态 | 仍在隔离库里，不能假定已结束；租约由 worker 续约，进程退出后到期 |
| 预算 | 80,000 token 已耗尽到 79,511（未被提高，也不应提高） |

**不能做的事**：删除或改写 Run/Verification/ToolCall/Artifact 行；改写首轮记录文件；把首轮记录当作失败样本；
自动开启恢复 Run（Policy 的 `max_rediagnose_attempts=0`、`max_retries_per_task=0`，本就不允许额外 Run）。

## 2. 收尾方案（先看清再决定，全部只读或单次动作）

第一步只读，一个字都不改：

```
python -m devflow_ai.control --inspect-expired-run 87dfb823-999a-42af-892d-31420535c486
```

按它报告的 `expired` / `takeover_blockers` 决定：

- **未过期**：什么都不做。租约还在说明仍有人在续约，此时任何关闭或接管都会被拒绝，也不必干预。
- **已过期**：只关闭，不恢复、不接管：
  ```
  python -m devflow_ai.control --close-expired-runs --max-runs 1
  ```
  它会写一条 `result=failed` 但 `evidence.scope=no_verification`、`reason=worker_lease_expired` 的
  Verification，加一条 `lease-expired:v1` 的 FailureRecord，并把 Task 置为 `ESCALATED`（两个恢复额度都是 0）。
  **注意**：这条 Verification 记录的是“没有验收证据”，不是“任务失败”；读取方必须先看 `scope`。
  首轮的运行记录文件不受影响，仍是 `incomplete`。
- **不要用** `--reclaim-expired-runs`：它会尝试建立恢复 Run，而本次快照明确禁止额外 Run；
  也不要用 `--take-over-expired-run`：预算已耗尽、节点无重放依据，接管只会再花一次预算。

如果不确定要不要关闭，就保持原状——留着一个 RUNNING 的旧 Run 不影响新建 Task 的第二次尝试。

## 3. 第二次运行的前置条件（逐项，标明谁核验）

| 项 | 要求 | 谁核验 | 怎么核验 |
| --- | --- | --- | --- |
| 新 Task | 必须新建；旧 Task 已有一次尝试 | 我 | `benchmark_run` 会拒绝已有 Run 的 Task |
| 独立记录 | 记录路径与首轮不同，排他创建 | 我 | 路径已存在会被拒绝（不覆盖） |
| Policy 快照 | 文件与 Task 保存的快照逐节一致 | 我 | `benchmark_run --policy ...` |
| 预算上限 | 80,000 token / 1,200 秒 / 1 次尝试 / 0 重试 | 我 | `BenchmarkLimits` 固定且入口执行 |
| 可信材料 | 文件字节的 SHA-256 等于清单 `materials_sha256` | 我 | `preflight --material` |
| 隔离库 | 连接指向 `devflow_benchmark_8dea52ee2d1d/public`，revision 等于项目 head | 我跑、Codex 确认指向 | `preflight --check-db` |
| 公开基线 | 两目标 failed、两对照 passed | 我 | `preflight` 的 `public_baseline` |
| 模型配置 | `DEEPSEEK_API_KEY` / `DEVFLOW_DEEPSEEK_MODEL` 在运行进程里 | 可信侧 | 本机/用户级配置存在但不会被自动继承 |
| 执行人/审核人 | 运行时再记录实际人员 | 可信侧 | `--operator`、review |

## 4. 命令（单行，PowerShell 可直接粘贴）

```
python -m devflow_ai.preflight --pack benchmarks/click_3019_public --checkout <CLICK_CHECKOUT> --python D:\python\PyCharm\Project\05_devflow-ai.venv\Scripts\python.exe --policy benchmarks/click_3019_public/policy.draft.json --import-name click --material <TRUSTED_MATERIAL_JSON> --artifacts-root <PREFLIGHT_DIR> --check-db
```
最后一行必须是 `{"preflight": "ok", ...}` 且 `formal_acceptance_blockers` 为空。

```
python -m devflow_ai.benchmark_task --pack benchmarks/click_3019_public --policy benchmarks/click_3019_public/policy.draft.json --repo <CLICK_CHECKOUT> --operator <姓名>
```
记下新的 `task_id`。

```
python -m devflow_ai.benchmark_run --pack benchmarks/click_3019_public --task-id <NEW_TASK_ID> --operator <姓名> --python-executable D:\python\PyCharm\Project\05_devflow-ai.venv\Scripts\python.exe --workspaces-root <WORKSPACES_ROOT> --artifacts-root <ARTIFACTS_ROOT> --record <RECORD_DIR>\click-3019-attempt-2.json --policy benchmarks/click_3019_public/policy.draft.json --material <TRUSTED_MATERIAL_JSON> --attempt-kind formal --confirm-run
```

跑完先看用量，再看记录：

```
python -m devflow_ai.usage_report --run <NEW_RUN_ID> --compare 87dfb823-999a-42af-892d-31420535c486
python -m devflow_ai.benchmark_record summarize <RECORD_DIR>\click-3019-attempt-2.json
```

## 5. 两次实际用量的比较方法

`python -m devflow_ai.usage_report` 只读取已经提交的 `llm_usage`，逐角色汇总输入/输出 token、请求数与**未结算**
请求（`actual_usage` 为空）的预留量；不读提示词、不回显内容、不改任何行。加 `--compare` 给两次 Run 的逐角色差值
与总差值。看三个数字：

1. **按角色的输入 token**：Supervisor 是否显著下降（首轮 34,690 里绝大部分是重复发送的预览正文）；
2. **请求数**：Explorer 每次执行有两次模型调用；若第二次尝试仍然反复 RE_EXPLORE，请求数会上升，
   说明问题不在提示大小而在证据判断；
3. **阶段数与停止原因**（记录里的 `stages` / `stopped`）：token 变少但阶段也变少，不能读成“优化有效”，
   要看它是否走到了 Coder/Verifier。

未结算请求会被单列：请求发出但没回用量时可能仍计费，所以它们不并入总量，也不假装是 0。

## 6. 结论口径（提前写死，避免事后解释）

- 第二次仍然是 `formal_pass` 才算形式上通过；`public_only_pass` 只说明公开检查过；`incomplete` 既不是成功也不是失败。
- 只有“正式通过 + 真人审核通过”才算任务解决；两次尝试是**两个独立样本**，汇总必须报样本数。
- token 更少不等于更优：只有“走到更后阶段或正式通过”才说明优化有意义。
- 首轮记录保持原样，永远不回填成第二次的结果。

## 7. 交给可信侧的动作

1. 确认 `DEVFLOW_DB_*` 指向 `devflow_benchmark_8dea52ee2d1d`（`preflight --check-db` 会打印库名与 revision）。
2. 确认运行进程能读到模型凭据（不输出、不落盘）。
3. 提供材料文件路径（内容不离可信侧），并确认清单里的摘要仍是 `c108f73a…62f40`。
4. 记录 `--operator` 与实际审核人，审核结论由真人作出。
5. 决定首轮 Run 是否按 §2 关闭（关闭只是收尾，不改写证据，也不产生新的尝试）。
