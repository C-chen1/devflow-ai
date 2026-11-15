# Benchmark 运行说明（V1）

本说明描述如何准备、驱动并记录一次 benchmark 任务，以及可以从结果中得出的结论边界。它不描述如何判断一个补丁"应该是"什么——那属于可信调用方的隐藏验收。

## 1. 适用范围

适用：在本机或受控环境中，对一个已登记的任务包跑一次真实运行，并留下可定位证据。

不适用：多任务统计服务、常驻守护进程、自动重试与调度。V1 的运行由人显式发起，一次一条 Run。

## 2. 前置清单

逐项确认后才开始，任何一项不确定就停下来，不要"先跑起来再补"。

- [ ] 测试库凭据就位：`DEVFLOW_DB_HOST` / `DEVFLOW_DB_PORT` / `DEVFLOW_DB_USER` / `DEVFLOW_DB_PASSWORD` / `DEVFLOW_DB_NAME` 指向测试库，集成测试用 UUID 隔离 schema；**不使用生产库**。
- [ ] 模型凭据就位：`DEEPSEEK_API_KEY` 非空、`DEVFLOW_DEEPSEEK_MODEL` 已固定（首轮为 `deepseek-flash`）；两者只经环境变量传入，不写入仓库、不打印。
- [ ] Docker 可用：独立评测需要；不可用时相关用例按标记跳过，**跳过不等于通过**。
- [ ] 目标仓库已 checkout 到清单的 `base_commit`（40 位完整哈希），并已核对实际导入路径指向该 checkout。
- [ ] `workspaces_root` 与 `artifacts_root` 是两个已存在且分离的目录。
- [ ] Policy 快照文件已由可信方定稿并审查：包含版本名、各项预算、各角色工具权限与超时；未定稿前清单 `policy` 保持未核验，不得只填版本名。`benchmarks/click_3019_public/policy.draft.json` 是一份待审草案，不是已批准配置。
- [ ] 执行人（operator）与独立审核人（reviewer）已确定；审核人不预填，由实际完成审核的人签收。
- [ ] 隐藏材料（独立套件版本、用例、参考版本）已由可信控制端准备并保管；确认它们不在本仓库、不在 Agent 工作区或上下文里。
- [ ] 本轮的时长与 token 上限已确认（首轮 1,200 秒 / 80,000 token / 1 次尝试 / 0 自动重试）。

## 3. 校验任务包

```bash
python -m devflow_ai.task_pack benchmarks/<pack_id>
```

输出 JSON 报告。`valid: true` 只说明格式与可核验字段符合要求；`unverified` 列出尚未确认的项，必须在运行前逐条处理或明确接受。清单的 `extra="forbid"` 只能拒绝未定义的字段，**无法阻止有人把隐藏内容写进 `notes` 之类自由文本**，所以它不是隔离措施——隐藏材料由可信控制端保管并在运行时注入，不进入本仓库、Agent 工作区或上下文。

## 4. 准备可信输入

- 把目标仓库 checkout 到清单里的 `base_commit`（40 位完整哈希），不要用浮动分支。
- Task 本身用 `python -m devflow_ai.benchmark_task --pack <包目录> --policy <快照.json> --repo <克隆源> --operator <姓名>` 建立并准入：它按清单的 `base_commit` 创建 Task 并置为 `ELIGIBLE`，创建前先校验 Policy 快照能否承载本次限额（`max_run_duration_seconds`、`max_llm_tokens_per_run`、最小图步数，以及 coder/verifier/explorer 必需的工具权限），不满足按配置错误退出 2。该命令**不内置任何权限**：快照内容由可信方提供并审查。
- 组装 `VerificationConfig`：`task_id`、`base_commit`、`command_name`、`command`（服务端登记的命令元组）、`target_tests`、`external_connections="none"`；需要独立评测时补 `independent_suite_version` 与 `pythonpath_relative`。公开测试不在 checkout 内时还必须给 `public_tests_root=<任务包目录>`：Worker 会把其中的 `*.py` 复制到本 Run 的 artifact 目录并让命令指向副本，checkout 保持干净。可由 `devflow_ai.task_pack.to_verification_config(manifest, task_id=..., public_tests_root=...)` 从清单派生，使 commit、命令与目标测试同源，避免手写配置与清单不一致。
- 隐藏用例、参考版本由可信控制端提供并保管，**不进入本仓库、不进入 Agent 工作区或上下文、不进入日志**。清单中只允许出现套件版本、口径明确的 `sha256` 与"由可信控制端提供"的声明。

## 5. 驱动一次运行

恢复与过期处置已有控制端命令（均为只读或单次动作，可重复执行）：

```bash
python -m devflow_ai.control --inspect-expired-run <RUN_ID>
python -m devflow_ai.control --close-expired-runs --max-runs 1
python -m devflow_ai.control --reclaim-expired-runs --max-scans 1 \
  --workspaces-root <dir> --artifacts-root <dir>
python -m devflow_ai.control --take-over-expired-run <RUN_ID> \
  --workspaces-root <dir> --artifacts-root <dir>
```

单任务入口现在有命令行形式（推荐；首轮执行方案见 `docs/FIRST_RUN_PLAN.md`）：

```bash
python -m devflow_ai.benchmark_run --pack benchmarks/click_3019_public \
  --task-id <TASK_ID> --operator <姓名> --python-executable <绝对路径解释器> \
  --workspaces-root <dir> --artifacts-root <dir> --record <record.json> \
  [--policy <快照 JSON>] [--material <可信材料 JSON>] --confirm-run
```

`--confirm-run` 是必须的显式确认（会花掉本次预算）；`--policy` 会比对 Task 实际保存的快照与给定文件，任一节（`budget`/`permissions`/`policy_version`）不同即退出 2；入口没有重试参数，一次调用就是一次尝试。未提供材料时告警且正式门禁不会转交，`external_connections=unknown` 时同样告警。退出码 `0` 表示记录已写出（结果看记录 `outcome`），`2` 表示配置不可用、没有开始跑，`1` 表示运行期失败（记录通常已写出）。

等价的程序化入口 `devflow_ai.benchmark_run.run_one_task` 在一次显式调用里完成装配、领取、执行阶段推进并写运行记录：

```
limits  = BenchmarkLimits(max_duration_seconds=1200, max_model_tokens=80_000)   # attempts=1, auto_retries=0
request = BenchmarkRunRequest(pack_dir=..., task_id=..., operator=...,
                              python_executable=sys.executable,   # trusted absolute interpreter
                              workspaces_root=..., artifacts_root=..., record_path=..., limits=limits)
result  = run_one_task(request, session_factory=..., worker=...,
                       gateways=model_gateways_from_env(...), heartbeat_config=...,
                       max_patch_bytes=..., verifier_max_diff_bytes=...,
                       independent_evaluation=<可信材料或 None>)
```

阶段驱动停止后，入口把 Verifier 草稿交给 orchestration 的 `finalize_benchmark_run` 走既有正式评测与完成事务；不提供材料时停在草稿并返回具体原因，不会伪造通过。

入口会先校验 Task 的 base commit 与清单一致、Task 的 Policy `max_llm_tokens_per_run` 不超过本次 token 上限，任一不符即拒绝启动且不写记录。执行阶段由 orchestration 层的 `run_task_stages` 推进：每轮依据"最后一条已成功的非 Supervisor 执行"选择下一次决策方，并在 Run 离开 RUNNING、执行权被拒、图步数预算耗尽、Supervisor 未派发、该角色之后无对应路由、达到 `max_stages` 或收到停止信号时停止，返回结果里带 `stages` 与 `stopped` 原因。停止不等于失败，也不等于通过——最终结论仍由结果分类与人工审核给出；本入口不做自动重试。

## 6. 记录一次运行

每次运行写一份运行记录 JSON（模型见 `devflow_ai.benchmark_record.BenchmarkRunRecord`），放在仓库之外的可定位路径，并在记录里保存 artifact 目录与验证 id。PROGRESS 只写摘要并引用记录路径，**不把运行证据只留在文档文字里**。

独立材料在运行前必须先通过摘要校验：入口用文件**原始字节**算 SHA-256 与清单 `materials_sha256` 比对，清单未固定摘要或摘要不一致都会被拒绝（不存在“跳过校验”的开关）。

记录必须包含：`pack_id`、`base_commit`、尝试序号、起止时间、模型与 Policy 版本、`outcome`、公开检查计数、正式验证引用（若存在）、人工审核结论、artifact 位置、操作者。

人工审核写到**新文件**，原始记录不改（记录是证据）：

```bash
python -m devflow_ai.benchmark_record review --record <record.json> \
  --conclusion accepted|rejected|pending --reviewer <姓名> --at <UTC 时间>
python -m devflow_ai.benchmark_record summarize <record>.reviewed.json [...]
```

`accepted` 必须同时给审核人与时间；汇总要对审核副本求和，否则 accepted 永远不会计入 `counts_as_solved`。

若记录里的派生摘要与证据不一致（例如正式事务替换了 Verifier 检查点输出），生成**修正副本**而不是改原文件：

```bash
python -m devflow_ai.benchmark_record correct --record <record.json> --reviewed <record.user-reviewed.json> \
  --out <record.corrected.json> --run <RUN_ID> --artifacts-root <dir> --basis "<依据>" \
  --corrected-by <姓名> --at <UTC 时间>
```

修正副本保留原有人工审核结论，只替换公开/独立摘要，并用 sha256 指向原记录与审核副本；结论不允许被修正改变。

审核结论还要落到数据库状态上（记录文件本身不改数据库）：

```bash
python -m devflow_ai.task_review --record <record.json> --reviewer <姓名>
```

它在**同一事务**里写一条追加式 `task_reviews` 行（绑定 Task、Run、正式 Verification 与记录 sha256），并把 Task 从 `AWAITING_REVIEW` 改为 `COMPLETED`（accepted）或 `REJECTED`（rejected）。同一份记录重复提交是幂等的；换记录或换结论会被拒绝。需要先执行 `alembic upgrade`（迁移 `b7f2c31d8a90`），由可信侧确认后自行执行——本文档不代为迁移。汇总时同一批里可能同时出现原记录、审核副本与修正副本：它们按 `run_id`（缺省用 pack/base/attempt/started_at）归为同一次尝试，默认只计一份（优先未被取代的、再优先修正副本、再优先带人工结论者），其余计入 `superseded_excluded`；同一尝试存在多份修正或无法排序时报 `conflicts` 且不计入样本，不会靠猜。

## 7. 判读与统计

四种结果严格区分，不能互相替代：

- `formal_pass`：走完独立评测与正式通过事务，**且证据链可复核**——`scope=trusted_v1`、草稿与独立回执齐备、套件版本与任务包一致，并且这两份 artifact 能从磁盘读回且归属同一 Verifier 与 Run、回执指向同一草稿且结果为通过；
- `public_only_pass`：公开结果**用草稿重算并与 checkpoint 记录一致**（不是“文件能读回”就算）+ 尝试已结束 + 独立评测**实际尝试过**、回执可核验且套件版本与清单一致、同一草稿，结果为 `unavailable` **且写明原因**；
- `failed`：独立评测实际执行、通过校准、回执可读且显示失败；记录同时保留“公开检查通过”这一事实（`public_checks.verified_outcome`），不只留一个失败标签；未校准的独立失败不算裁决；
- `timeout`：超出预设时长上限而中止；
- `incomplete`：运行仍在进行、被阻塞、执行被中断、独立评测未尝试、回执不可核验或套件版本不符，或公开/独立任一侧缺少可核对证据；提示语里保留具体原因。**它不是失败**，不得计作失败，也不得计作通过。

缺少完整证据链的"通过"记录只能归为 `incomplete`。执行抛异常时，入口仍会写出记录（`incomplete` 且 notes 含 `execution_error:<类型>`），然后把原异常原样抛出。

阶段驱动停在 `verification_blocked` 时，那是检查点草稿（执行仍在 RUNNING），入口会在同一心跳内把它交给正式评测；若最终仍未产生正式结果，记录里的 `finalization` 会写出 `verifier_still_running`、`no_blocked_verifier_draft` 等具体原因。

**成功口径**：正式通过 **且** 人工审核通过，才计为任务解决。只 `formal_pass` 而未审核，记为"曾正式通过一次，待审核"（`counts_as_solved == False`）。

**统计口径**：报告必须同时给出样本数。记录的 `attempt_kind`（`formal`/`rehearsal`/`unknown`）是正式字段；只有 `formal` 进正式样本，`rehearsal` 与缺字段的旧记录（读作 `unknown`）默认排除，`summarize` 分别报出 `rehearsals_excluded` 与 `unknown_kind_excluded`（`--include-non-formal` 才计入）。被排除的记录仍是证据，只是不进正式样本。1 次尝试通过并审核通过，可以如实写 `1/1`，但必须注明样本只有 1 次，不能据此宣称稳定成功率。模拟模型、测试替身、夹具构造的链路不计入真实成功率。

## 8. 运行后自查

- 独立评测是否真的执行，而不是被标记跳过；
- 隐藏材料是否从未出现在工作区、日志或模型中；
- 运行记录能否从 artifact 与验证 id 定位回实际产物；
- 清单中 `unverified` 的项是否已减少，或仍被明确接受。

## 9. 当前已知未核验项

- Policy 快照只是首轮候选：Task 上实际保存的完整快照仍需在创建后核对。
- `external_connections` 仍为 `unknown`；静态扫描为空不能替代可信声明，正式门禁因此会拒绝。
- 清单 `independent_suite` 仍为 `null`，正式评测无套件版本可核对；可信材料（镜像、用例、校准工作区）尚未准备。
- 真实 Run 自己记录的补丁前基线、一次真实 `deepseek-flash` 运行、Docker 独立评测端到端，均无证据。
- Linux 沙箱全量测试中 17 项失败尚未逐项核实（与 pytest junit 报告捕获的平台限制有关），不计为通过。
