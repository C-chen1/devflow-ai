# 首轮真实任务：可执行运行方案

目标：用 Click #3019 任务包验证系统能否从“一个 Issue”走到“正式验收”，并以一次真实
`deepseek-flash` 运行留下可审查的证据。本文只写**能照着执行**的步骤；每项都标证据状态：
`已实测`（有可复现位置）、`仅构造验证`（测试替身/合成夹具）、`待核验`（需 Codex 或可信方确认）、
`缺证据`（没做或没人核验）。

**当前决策（2026-11-12，已定）**：

- 按“先核验并固定独立评测材料与外部连接声明，再建正式 Task”推进。在可信方把清单的 `independent_suite` 与
  `external_connections` 定版之前，**不创建正式 Task**（不占掉唯一的一次尝试），也不凭静态扫描或格式校验替这两项填值。
- 正式尝试中若独立评测返回 `unavailable`，保留全部证据、记录为 `incomplete`，**不自动新建 Task 重跑**；
  之后若要再试，必须新建 Task 作为新的一次尝试单独记录，不能把首轮改写成成功。
- 若在此期间确实需要跑一次环境演练：用 `--attempt-kind rehearsal`（记录的正式字段，不是备注），并且**另建一个演练 Task**——
  一次 Task 只有一次尝试，拿正式 Task 去演练等于把它用掉。演练记录被汇总默认排除，不混入正式样本；演练结束后再建正式 Task。

先说结论性的上限，避免把首轮读成“已解决”：

- `independent_suite` 已由可信方定版（`click3019-private-v1` + 材料摘要），但 `external_connections`
  仍为 `unknown`，而 `finalize_passing_verification` 要求 `external_connections == "none"`。
  因此在外部连接被可信方核验为 `none` 之前，首轮**仍不可能**产生 `formal_pass`；本方案不会替它填值。
- 因此首轮的合格结果上限是 `public_only_pass`（公开检查通过、正式评测未获准入），
  或如实记为 `incomplete`/`failed`/`timeout`。`counts_as_solved` 必然为 false。
- 一次 `benchmark:<task_id>:1` 创建键只对应一次尝试；要跑第二次必须新建 Task（新样本），
  不能用同一 Task 补跑。

## 1. 固定输入

| 项 | 值 | 状态 |
| --- | --- | --- |
| 任务包 | `benchmarks/click_3019_public`（`pack_id=click-3019`） | 已实测 |
| base commit | `00fadb8904387158ce6e9aa1573be770446895c1` | 已实测 |
| 公开检查 | `python -B -m pytest -q -p no:cacheprovider scenarios.py`，目标 `scenarios::test_prompt_suffix[empty-suffix]`、`scenarios::test_confirm_suffix[empty-suffix]` | 已实测（Windows 自动公开基线：两目标 failed、两对照 passed、退出 1） |
| Policy 快照 | `benchmarks/click_3019_public/policy.draft.json`（`benchmark-click3019-v1`） | 结构已实测；**定稿待核验** |
| 解释器 | `D:\python\PyCharm\Project\05_devflow-ai.venv\Scripts\python.exe`（Python 3.11.15、pytest 9.1.1、colorama 0.4.6） | 已核验（Codex 实测） |
| 目标 checkout（缺陷基准） | 干净 Click 仓库，HEAD = `00fadb8904387158ce6e9aa1573be770446895c1` | 已核验（Codex 实测） |
| 参考工作区（校准用 corrected） | 本地 HEAD `215ccf62340c81496f07995bf22dbcff5317cedd` | 已核验（**本地参考提交，不是上游官方提交**） |
| 隔离数据库 | `devflow_benchmark_8dea52ee2d1d/public`，已迁移到项目 head `f3b8d2a6c1e4` | 已核验（Codex 新建并迁移）；**使用前仍由 Codex 核对连接指向**，旧的 `devflow_test/public`（停在 `4b79c6e21a50`）不迁移、不写入 |
| Docker | 守护进程可应答；`python:3.11-slim` 已在本机 | 已核验（镜像存在**不等于**独立评测或校准通过） |
| 可信独立材料 | `suite_version=click3019-private-v1`，`materials_sha256=c108f73a…62f40`（口径：material JSON 原始 UTF-8 字节含末尾换行）；文件本体在仓库外 | 已核验（Codex 实测校准 `verified`：缺陷基准 failed、参考版本 passed，8 条用例，`python:3.11-slim`）；文件不离可信侧 |
| 限额 | 1,200 秒 / 80,000 token / 1 次尝试 / 0 自动重试 | 已实测（`BenchmarkLimits` 与入口强制） |
| 模型 | `deepseek-flash`；`DEEPSEEK_API_KEY` 只在进程环境里，不写文件、不进日志 | 待核验 |

Policy 关键取值（来自草案，`python -m devflow_ai.preflight --policy ...` 会逐项回报）：
`max_run_duration_seconds=1200`、`max_llm_tokens_per_run=80000`、`max_retries_per_task=0`、
`max_rediagnose_attempts=0`、`max_graph_steps_per_run=30`、`max_tool_calls_per_run=60`、
`max_output_repairs_per_agent_execution=1`、`tool_call_timeout_seconds=300`；权限 SUPERVISOR=[]、
PLANNER=[]、EXPLORER=[read_file]、CODER=[read_file, apply_patch]、VERIFIER=[read_file, run_command, git_diff]。

### 1.1 隔离数据库现状

可信方已新建 `devflow_benchmark_8dea52ee2d1d/public` 并迁移到项目 head `f3b8d2a6c1e4`。
旧的 `devflow_test/public`（停在 `4b79c6e21a50`）**不迁移、不写入、不用于任何首轮动作**；
使用新库之前仍由 Codex 核对连接实际指向该库（`DEVFLOW_DB_NAME` 等环境变量）。

## 2. 一次性目录准备

四个根目录互不重叠，全部在仓库之外（仓库里不产生运行数据）：

```
<CLICK_CHECKOUT>       干净 checkout（只读使用，运行中不会被写入）
<PREFLIGHT_DIR>        preflight 的副本与 JUnit 报告
<WORKSPACES_ROOT>      Run 工作区
<ARTIFACTS_ROOT>       Run artifact
<RECORD_DIR>           运行记录与审核副本
```

## 3. 步骤（每步都以退出码判定，0 才算通过）

以下命令都写成**单行**（Windows PowerShell 直接粘贴即可，不用续行符）；把 `<...>` 换成实际路径。

### 步骤 0：前置核验（不调用模型，不写任何证据）

```
python -m devflow_ai.preflight --pack benchmarks/click_3019_public --checkout <CLICK_CHECKOUT> --python <PYTHON_3_11_15> --policy benchmarks/click_3019_public/policy.draft.json --import-name click --material <TRUSTED_MATERIAL_JSON> --artifacts-root <PREFLIGHT_DIR> --check-db
```

读取要点：`policy.ok`、`interpreter.version==3.11.15`、`checkout.head` 等于清单 commit 且
`dirty=false`、`import_inside=true`、`environment.present` 全为 true、`docker.ok`、
`database.ok`（现在会报 `database` 库名、`alembic_revision`、`project_head`、`at_head`：revision 与项目 head 不一致会直接判 `ok=false`；但这只说明“指针在哪个库、记录的是哪个 revision”，**不**证明 schema 完整或与其他数据隔离）、
`public_baseline.target_statuses` 两个 `failed`、`controls` 两个 `passed`、`files_in_checkout=[]`。
给了 `--material` 时还会多一行 `independent_material`：先核对文件原始字节的 `file_sha256` 与清单 `materials_sha256`（`digest_matches` 必须为 true），再核对套件版本与清单一致、两个校准工作区存在且不同、corrected 工作区的 HEAD 等于声明 commit、候选镜像是否已在本机（只 `docker image inspect`，从不拉取）、用例条数；**用例内容不会出现在输出里**。清单的 `independent_suite` 仍为 `null` 时这一项会明确报“尚未固定套件版本”。
最后一行 `{"preflight": ...}` 且退出码 0。

这一步**不是**基线证据：它不写 Run/Verification，真实 Run 仍要自己记录一次补丁前失败。

### 步骤 1：建 Task（不启动 Run）——**门禁：仅在可信方定版 `independent_suite` 与 `external_connections` 之后执行**

否则这一步会占掉该 Task 唯一的一次尝试（`benchmark:<task_id>:1`），而首轮只可能得到 `public_only_pass`。

```
python -m devflow_ai.benchmark_task --pack benchmarks/click_3019_public --policy benchmarks/click_3019_public/policy.draft.json --repo <CLICK_CHECKOUT> --operator <姓名>
```

输出 `{"event": "benchmark_task_created", "task_id": "..."}`，记下 `task_id`。
创建后核对该 Task 保存的快照：读 `Task.policy_snapshot` 与 `Task.base_commit` 确认与草案、清单一致；
步骤 2 的 `--policy` 会在每次运行前自动做同一比对，人工核对只是留痕。

### 步骤 2：一次真实运行（调用模型，限额由入口执行）

```
python -m devflow_ai.benchmark_run --pack benchmarks/click_3019_public --task-id <TASK_ID> --operator <姓名> --python-executable <PYTHON_3_11_15> --workspaces-root <WORKSPACES_ROOT> --artifacts-root <ARTIFACTS_ROOT> --record <RECORD_DIR>\click-3019-attempt-1.json --policy benchmarks/click_3019_public/policy.draft.json --material <TRUSTED_MATERIAL_JSON> --attempt-kind formal --confirm-run
```

- `--confirm-run` 是必须的显式确认：这条命令会花掉 1,200 秒 / 80,000 token 的预算。
- `--policy` 让入口在开始前比对**Task 里实际保存的** Policy 快照与这份文件；只要有一节不同（如 `budget`/`permissions`/
  `policy_version`）就以退出码 2 拒绝并列出差异节名。不给 `--policy` 时只告警“没有比对”。
- 入口不接受“重试”参数：一次调用就是一次尝试，`BenchmarkLimits` 固定 `attempts=1`、`auto_retries=0`。
- 进程内固定限额：读 64 KiB / 补丁 64 KiB / diff 256 KiB，心跳语句超时 5 秒、关闭超时 5 秒（都可用同名参数收紧，
  不能放宽到超过 Policy）。
- 未提供 `--material` 时入口会告警：正式门禁不会转交，结果最多 `public_only_pass`。
- `--attempt-kind` 写入记录的**正式字段** `attempt_kind`（`formal`/`rehearsal`/`unknown`）：只有 `formal` 进正式样本，`rehearsal` 与 `unknown` 默认被 `summarize` 排除并分别报出数量（`--include-non-formal` 才计入）；旧记录缺该字段时读作 `unknown`，不假定为 formal。
- `external_connections=unknown` 时同样会告警。
- 退出码：`0` 已写出运行记录（结果看记录里的 `outcome`）；`2` 配置不可用、没开始跑；`1` 运行期失败
  （记录通常已经写出，stderr 会给出 `record_written` 与路径）。

### 步骤 3：判读运行记录

```
python -m devflow_ai.benchmark_record summarize <RECORD_DIR>\click-3019-attempt-1.json
```

首轮预期：`outcomes.public_only_pass` 或 `incomplete` 为 1，`solved_over_sample == "0/1"`，
`formal_pass_pending_review == 0`。若为 `incomplete`，看记录 `notes` 里的
`stopped=`/`finalization=` 与 artifact 目录里的证据定位停止原因。

### 步骤 4：人工审核（只有“正式通过 + 真人审核通过”才算解决）

```
python -m devflow_ai.benchmark_record review --record <RECORD_DIR>\click-3019-attempt-1.json --conclusion accepted|rejected|pending --reviewer <姓名> --at 2026-11-12T10:00:00+00:00
```

审核结论写到**新文件** `<record>.reviewed.json`，原始记录一字节不改（记录是证据）。
`accepted` 必须同时有审核人与时间，否则拒绝。汇总时对**审核副本**求和，否则 accepted 永远不计入：

```
python -m devflow_ai.benchmark_record summarize <RECORD_DIR>\*.reviewed.json
```

（PowerShell 下通配符由 PowerShell 展开，可写 `Get-ChildItem <RECORD_DIR>\*.reviewed.json | ForEach-Object { python -m devflow_ai.benchmark_record summarize $_.FullName }`，或逐个文件传参。）

### 步骤 5：收尾（仅在 Run 仍 RUNNING 时）

Verifier 阻塞会让 Run 停在 RUNNING 等租约过期。过期后可以只关闭、不重试：

```
python -m devflow_ai.control --close-expired-runs --max-runs 1
```

Policy 的 `max_retries_per_task=0` 与 `max_rediagnose_attempts=0` 会让关闭后的决策落到升级人工，
不会自动再跑一次。不要删除工作区或 artifact 目录。

## 4. 可信独立评测材料的注入方式

材料放在仓库之外（例如 `<RECORD_DIR>\material.json`），只由你保管，由 `--material` 在运行时读入；
入口只使用、不回显：`PrivateCase` 的用例内容不进 `repr`、不进日志、不进记录。格式（严格字段，多一个字段都会被拒）：

```json
{
  "suite_version": "click-3019-hidden-v1",
  "image": "<候选容器镜像，需事先 docker pull>",
  "command": ["python", "-B", "adapter.py"],
  "cases": [{"stdin_base64": "...", "stdout_base64": "..."}],
  "calibration": {
    "baseline_workspace": "<未修复 checkout 的绝对路径>",
    "corrected_workspace": "<已知修复 checkout 的绝对路径>",
    "expected_commit": "<corrected 的 40 位 commit>"
  },
  "timeout_seconds": 30
}
```

- **入口先校验摘要**：`--material`（以及 `preflight --material`）先用文件原始字节算 SHA-256 与清单 `materials_sha256` 比对，通过后才解析内容；清单未固定摘要或摘要不一致一律拒绝，不会降级成“任何材料都行”。
- **摘要只标识被固定的字节**：`materials_sha256` 证明“这么多字节是这个内容”，不证明来源、隔离，也不证明用例设计得好。判断“能否区分缺陷与修复”只能靠校准：正式评测会实际跑两个版本，缺陷版本必须**失败**、参考（corrected）版本必须**通过**，只有 `calibrated=true` 才允许正式通过；`preflight --material` 不跑容器、也不做任何校准结论。
- `suite_version` 必须与清单 `independent_suite.suite_version` 一致；清单现在为 `null`，
  所以真实材料要先由可信方在清单里固定版本，否则正式门禁直接拒绝。
- 两个 calibration 工作区必须互不相同、也不能是候选 Run 的工作区，否则评测会判为不可用；现有参考工作区的 HEAD 是**本地参考提交** `215ccf62340c81496f07995bf22dbcff5317cedd`，记录与文档都不得写成上游官方提交。
- adapter 必须用 stdin/stdout 表达目标行为；材料本体不进入候选挂载。

## 5. 结果保存位置

| 内容 | 位置 |
| --- | --- |
| 运行记录（一次尝试一份，排他创建） | `--record` 指定的路径 |
| 人工审核副本 | `<record>.reviewed.json`（由 `review` 写出） |
| 公开测试副本、JUnit 报告、语义草稿、独立回执 | `<ARTIFACTS_ROOT>\<run_id>\` |
| 目标仓库工作区（补丁、diff） | `<WORKSPACES_ROOT>\<run_id>\`（Run 从 `Task.repo` 即 `<CLICK_CHECKOUT>` clone 出来，原 checkout 只作为来源、不被写入） |
| Task / Run / AgentExecution / Handoff / ToolCall / Artifact / Verification 行 | 隔离数据库 |

## 6. 证据状态

**已实测**：清单结构与目标命名；Policy 草案通过结构检查；preflight 全流程与自动公开基线
（Windows、固定 Python 3.11.15、干净 checkout：两目标 failed、两对照 passed、退出 1、
checkout 无新增文件）；`BenchmarkLimits` 与入口限额；公开测试装配后 checkout 不被改动。

**仅构造验证**：`benchmark_run` 单命令入口（合成 Task/桩模型）、`benchmark_record review/summarize`
（合成记录）、正式评测转交与拒绝原因、过期 Run 的关闭与接管。

**待核验**：`independent_suite` 与 `external_connections` 的可信定版（当前为空/unknown，未创建正式 Task）；Policy 快照定稿与 Task 上保存的内容；解释器绝对路径与依赖版本；隔离库的迁移版本与隔离性；
Docker 镜像可用性；`external_connections` 的声明（静态扫描为空**不足以**声明 `none`）；
清单里的 `independent_suite.suite_version` 与真实隐藏套件是否一致。

**缺证据**：真实 Run 自己在 Run 内记录的补丁前失败基线；一次真实 `deepseek-flash` 运行；
Docker 独立评测端到端；正式通过 + 人工审核通过的完整样本。

## 7. 已知限制（如实保留）

- `preflight` 按**变量名**过滤凭据（`API_KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL`），
  不保证覆盖所有可能的命名；实现会临时修改**进程级**环境再恢复，因此不能宣称并发隔离。
  公开检查的子进程输出默认不返回，只有 `--show-output` 才带尾部。
- `command_sha256` 含本次副本的绝对路径，只在同一 artifact 根内可比；跨运行只能比
  `public_tests_sha256`（逐文件内容摘要）。
- 停止是协作式的：到期只表示已请求停止，不证明在进行中的外部调用已退出。
- 独立评测的容器以 `docker run --rm --pull never --network none --read-only` 执行，这是“该容器没有网络”的证据，但它**不能**单独证明整个任务评测没有外部依赖（公开检查在宿主进程里跑、材料来源与镜像内容仍由可信侧负责）。
- 数据库探测只说明“能连上、当前 schema 有多少张表”，不证明迁移完整、schema 与代码一致或库已隔离。

## 8. 第二次运行

首轮已实际执行并停为 `incomplete`；第二次的前置条件、收尾决定与用量比较方法见 `docs/SECOND_RUN_PLAN.md`。

## 9. 本轮不做什么

不启动真实模型任务、不跑 Docker 独立评测（在 Codex 核验可信材料与实际环境之前）；
不提交、不清理工作区、不迁移现有数据库；不把 `external_connections` 从 `unknown` 改成任何值。
