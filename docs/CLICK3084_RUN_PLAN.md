# Click #3084：第二个真实任务的运行准备清单

状态：**公开基线、独立材料校准、Policy 与本机 preflight 已完成**。可信材料保存在仓库外；
浅仓库经第三方代理取得的来源限制仍需在结果中保留。本次准备未创建正式 Task、未启动模型或改数据库。

## 1. 已核验（可作为事实引用）

| 项 | 结论 | 来源 |
| --- | --- | --- |
| Issue / PR / 合并关系 | #3084 由 PR #3152 修复，合并提交 `ebcd548d…` 的父提交是 `7f7bbe4…` 与 `955ca49…` | GitHub API + Codex 复核 |
| 基准 commit | `7f7bbe4569ea68e8dabee232eade069ef3310aea`（完整 40 位，`stable` 上修复前的 tip） | 同上 |
| 参考修复 | 分支 tip `955ca492a6b9e8de0660dae1c415568ca6f9c4b3` | 同上 |
| 公开基线（Windows 正式候选） | 基准 1 failed / 2 passed、退出 1；参考修复 3 passed；Python 3.11.15 / pytest 9.1.1 / colorama 0.4.6 | Codex 复核 |
| 公开基线（Linux 编码侧） | 同样结论，Python 3.11.17 / pytest 9.2.0 | 编码侧 |
| 浅仓库 | `benchmarks/click_3084_public/click3084-public-checkouts.tar.gz`（sha256 `47a1d209…87fd`），HEAD/tree/144 文件/工作区干净已核对 | 编码侧打包、Codex 解包核对 |
| 任务包 | `benchmarks/click_3084_public/`（`task.json`、`scenarios.py`、`stdio_adapter.py`、`policy.draft.json`、`README.md`），`python -m devflow_ai.task_pack` 通过 | 本仓库 |

**未消除的来源限制**：浅仓库经第三方代理取得。对象按 SHA-1 寻址、tree 与上游 API 一致、Codex 已解包核对，
但正式运行仍以可信侧自己的 checkout 为准；这一点写在清单 `unverified.base_commit` 与包内 README 里。

## 1.1 首次真实尝试的结果（不是通过，也不是失败）

首次正式尝试已结束，结果为 `incomplete`：没有进入 Planner、Coder 与正式验收，**没有产生补丁**，因此既不能说任务通过，
也不能说补丁验证失败。用量 14 次模型请求 / 72,527 token（Explorer 64,548、Supervisor 7,979），无自动重试。
该 Run 与记录保持原状（数据库中仍为 `RUNNING`，未关闭、未重跑）。

直接原因：`src/click/core.py` 较大，Explorer 多次读取都只拿到文件头的截断预览，关键的 `Option` 定义在更后面；
Supervisor 反复要求补充证据，最终 Explorer 无法再取得足够的模型预算。

已就此实现的能力（详见 `docs/PROGRESS.md`）：`read_file` 支持按行范围读取（`offset`/`limit`），
窗口写进工具调用参数与证据；Explorer 的输入与路由视图都会带上“已读窗口”，提示词要求在被读文件更深处缺证据时指明行范围，
而不是重复索要同一段开头。**下一次尝试应当受益于此，但这不保证任务能通过**——需要真实运行才知道。

## 2. 环境（正式候选）

- Windows，Python **3.11.15**（绝对路径，`.exe`），pytest **9.1.1**，colorama **0.4.6**；
  该组合已用基准与参考修复两个 checkout 复核过公开基线。
- Linux（3.11.17 / 9.2.0）的结果只作编码侧记录，不与 Windows 混用。
- 独立评测镜像需自备 Python 运行时；适配器必须打进镜像（见 §4）。

## 3. Policy：`benchmark-click3084-v1`

`benchmarks/click_3084_public/policy.draft.json`，与 #3019 的快照版本相互独立：

- 预算：`max_run_duration_seconds=1200`、`max_llm_tokens_per_run=80000`、`max_retries_per_task=0`、
  `max_rediagnose_attempts=0`、`max_graph_steps_per_run=30`、`max_tool_calls_per_run=60`、
  `max_output_repairs_per_agent_execution=1`、`tool_call_timeout_seconds=300`；
- 权限：SUPERVISOR=[]、PLANNER=[]、EXPLORER=[read_file]、CODER=[read_file, apply_patch]、
  VERIFIER=[read_file, run_command, git_diff]；
- `python -m devflow_ai.benchmark_task` 的结构检查（限额 + 三角色必需工具）在该草案上通过；
- 运行前 `benchmark_run --policy` 会把它与 Task 里实际保存的快照逐节比对。

## 4. 公开适配器与独立材料

- 适配器：`benchmarks/click_3084_public/stdio_adapter.py`（公开，一次 JSON 进一次 JSON 出，源码根由
  `DEVFLOW_CANDIDATE_ROOT` 指定，默认 `/candidate`）；
- 容器契约（既有代码）：`--network none --read-only`，只把候选工作区只读挂到 `/candidate`，
  因此适配器要 `COPY` 进镜像，材料 `command` 用镜像内路径；
- 材料格式与校准方式见包内 README「独立评测」一节：`suite_version`、`image`、`command`、隐藏 `cases`、
  两个校准工作区（基准 / 参考修复）与 `expected_commit`；
- 校准由 `calibrate_stdio_suite` 在正式评测时执行：**基准必须 failed、参考必须 passed** 才算 `verified`；
- **不能复用 #3019 的私有套件**；本仓库不生成、不读取、不要求提供任何隐藏用例内容。

## 5. 运行步骤（每步以退出码判定）

1. `preflight`（`--pack benchmarks/click_3084_public --checkout <可信 base checkout> --python <3.11.15 绝对路径>
   --policy benchmarks/click_3084_public/policy.draft.json --import-name click --material <材料 JSON>
   --artifacts-root <PREFLIGHT_DIR> --check-db`）——要求最后一行 `{"preflight":"ok"}`、
   `formal_acceptance_blockers` 为空、`public_baseline` 目标 failed + 两对照 passed；
2. `benchmark_task` 建新 Task（`--policy policy.draft.json --repo <checkout> --operator <姓名>`）；
3. `benchmark_run`（`--policy … --material … --attempt-kind formal --confirm-run`，记录写到新路径）；
4. `usage_report --run <NEW> --compare <上一次 Run>`；
5. `benchmark_record summarize` → 真人审核 → `review` 写审核副本 → 再 `summarize`。

## 6. 可信侧待办（缺一不可）

1. 基准/参考浅仓库 HEAD、tree、工作区、公开基线及参考检查已在 Windows 验证；来源经第三方代理，限制保留；
2. 可信侧已独立准备本任务材料并用离线容器校准：基准 failed、参考 passed；材料不入仓库；
3. 已按本任务公开检查与离线独立评测的依赖声明 `external_connections=none`；宿主网络并未因此隔离，模型调用仍需网络；
4. 已固定 Policy `benchmark-click3084-v1` 与解释器；运行前重新核对镜像、材料摘要、数据库、目录与执行人，审核人由实际审核的真人记录。

## 7. 本批不做什么

不创建正式 Task、不启动模型、不跑 Docker 独立评测、不提交、不清理工作区、不迁移数据库；
隐藏用例只由可信侧保管与注入。
