# 首轮真实任务：前置检查清单

> 逐步可执行的运行方案（入口、固定输入、限额、材料注入、结果位置）见 `docs/FIRST_RUN_PLAN.md`。

每条都标注**证据状态**。状态只有三种：`已实测`（附可复现位置）、`仅构造验证`（测试替身/构造证据，不等于真实运行）、`缺证据`（未做或无人核验）。任何人不得把后两种读成第一种。

## A. 代码与配置（本地可验证）

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 任务包清单结构合法 | 已实测 | `python -m devflow_ai.task_pack benchmarks/click_3019_public` → `valid: true` 并列出 6 项未核验 |
| 清单承载 `pythonpath_relative` / 公开测试目录 | 已实测 | `tests/test_task_pack.py`、`tests/test_public_checks.py` |
| Policy 草案结构与额度（两额度为 0、限额、权限） | 已实测 | `tests/test_benchmark_task.py::test_the_committed_policy_draft_parses_and_meets_the_structural_check` |
| 建 Task 命令（按清单建 Task 并准入） | 仅构造验证 | `tests/test_benchmark_task.py`（隔离库 + 夹具，未在真实 Task 上跑过） |
| 单任务入口限额（时长、token、阶段上限） | 仅构造验证 | `test_benchmark_run_*`；初始化阶段超时、异常留证已覆盖 |
| 阶段驱动路由与停止原因 | 仅构造验证 | `test_stage_driver_*`（11 条） |
| 结果分类与证据链读取 | 仅构造验证 | 构造 artifact 的正向/篡改/归属/版本用例；无非真实草稿 |
| 公开测试装配（副本 + rootdir/confcutdir） | 已实测（单元） | `tests/test_public_checks.py`；不含 checkout 改动 |
| 命令证据由同一推导产出（执行侧与验收侧重算一致） | 已实测 | 共用 `verification_config.public_check_command`；`test_the_recorded_command_evidence_is_the_evidence_acceptance_recomputes`（真实执行一次命令，比较记录值） |
| 公开测试副本内容被钉住（逐文件 sha256） | 已实测 | 同上用例 + `test_the_public_test_command_points_at_a_run_scoped_copy`；改写副本后摘要不再匹配 |
| 可信解释器绝对路径 | 已实测 | `VerificationConfig.python_executable` 校验 + 装配用例 |
| 未启动证据由进程边界给出（不按异常类型） | 已实测 | `test_only_the_process_boundary_can_prove_a_command_never_started`、`test_a_tool_the_boundary_proved_unstarted_is_recorded_as_failed`、`test_the_same_error_after_a_real_start_is_recorded_as_unknown` |
| 清单 → 真实基线执行（构造 Git 夹具） | 已实测（Windows） | Codex 2026-11-12 在 Windows + 隔离 PostgreSQL 复测：公开测试与基线专项 10 passed，含 `test_a_pack_public_test_runs_as_a_baseline_without_touching_the_checkout`；Linux 沙箱无 junit 捕获，本地跳过 |
| 基线 → 补丁后检查 → 验收命令核对（公开测试在 checkout 外） | **缺证据（Windows）** | `test_a_pack_located_public_test_keeps_one_command_from_baseline_to_acceptance` 需 Windows junit 捕获；本地曾以“仅放开 junit 捕获的平台判定替换”临时预跑整链通过（`preliminary_public_outcome=passed`，两次命令记录与验收侧重算一致），该临时用例未入库，不替代 Windows 结果 |
| 目标测试名与 JUnit 记录一致 | 已实测 | `test_the_click_pack_target_tests_use_the_recorded_report_naming`；解析器记 `classname::name`（模块名去 `.py`），清单已改为 `scenarios::test_x[param]` |
| 单命令运行入口（`python -m devflow_ai.benchmark_run`） | 仅构造验证 | `tests/test_benchmark_run.py` 的 CLI 用例（桩模型/合成 Task）；真实运行未开始 |
| 审核与统计入口（`python -m devflow_ai.benchmark_record`） | 仅构造验证 | `tests/test_benchmark_record.py`（review 写新文件、accepted 需审核人、summarize 报样本数） |
| 前置核验入口（`python -m devflow_ai.preflight`） | 已实测（Windows 真实 Click + 沙箱合成 pack） | Codex 2026-11-12 用固定 Python 3.11.15 与干净 Click checkout 跑通公开基线：两目标 failed、两对照 passed、退出码 1、`click` 从 checkout `src` 导入、checkout 无新增文件；沙箱 `tests/test_preflight.py` 14 条覆盖合成夹具；`test_the_checkout_check_reports_where_the_package_is_imported_from` 的 Windows 失败已改为子进程内实路径判定并回报搜索路径，待 Windows 复跑确认 |
| Docker 独立评测与正式完成事务 | **缺证据** | 仅有门禁与转交测试；未跑真实容器 |

## B. 事实核验（只能由 Codex / 可信控制端完成）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| Click #3019 公开基线 | 已实测（preflight 自动执行）；**Run 内持久化基线仍缺证据** | 2026-11-12 Codex 用固定 Python 3.11.15 与干净 Click checkout 跑 `python -m devflow_ai.preflight`：两目标 `failed`、两对照 `passed`、退出码 1、`click` 从 checkout `src` 导入、checkout 无新增文件；此前 2026-11-09 的手工基线同样为 2 failed / 2 passed（`test_prompt_suffix[empty-suffix]`、`test_confirm_suffix[empty-suffix]`）。preflight 不写 Run/Verification，真实 Run 必须自己记录一次补丁前失败 |
| `external_connections` | **缺证据** | 保持 `unknown`；未核验评测配置与执行过程是否需要外部连接 |
| Policy 快照定稿 | **缺证据** | 草案已整理待审，未定稿 |
| 环境就绪（Docker、`DEEPSEEK_API_KEY`、`DEVFLOW_DEEPSEEK_MODEL`、隔离测试库） | 部分已核验 | Docker 与 `python:3.11-slim` 已核验；隔离库 `devflow_benchmark_8dea52ee2d1d/public` 已迁移到 `f3b8d2a6c1e4`（`--check-db` 会核对库名与 revision）；模型凭据只在进程环境，未在沙箱核验 |

## C. 开跑前必须满足（全部为硬条件）

1. 清单→真实基线集成在 Windows 通过（对应 A 表“清单 → 真实基线执行（构造 Git 夹具）”，2026-11-12 已满足）；
   而“基线→补丁后检查→验收命令核对”的贯通用例仍须在 Windows 跑通。
2. Policy 快照定稿，且 `check_snapshot_supports_run` 通过。
3. `external_connections` 由可信方核验；保持 `unknown` 时正式评测会拒绝，这是预期。
4. 隐藏材料（独立套件版本、用例、参考版本）就位且只在可信侧保管。
5. 执行人与独立审核人确定；审核人不预填。
6. 单次限额确认：1,200 秒 / 80,000 token / 1 次尝试 / 0 自动重试。

## D. 开跑步骤

0. **前置核验 + 公开基线**（不加载模型，也不写任何 Run / ToolCall / Verification）：

   ```
   python -m devflow_ai.preflight --pack benchmarks/click_3019_public --checkout <CLICK_CHECKOUT> --python <PYTHON_3_11_15> --policy benchmarks/click_3019_public/policy.draft.json --import-name click --material <可信材料 JSON> --artifacts-root <PREFLIGHT_DIR> --check-db
   ```

   逐行输出的含义：`policy` 快照能承载本次限额；`interpreter` 绝对路径、版本、能否 `import pytest`；`checkout` HEAD 等于清单 commit、工作区干净、`click` 确实从 `<CLICK_CHECKOUT>/src` 导入；`environment` 只报 `DEEPSEEK_API_KEY` / `DEVFLOW_DEEPSEEK_MODEL` **是否存在**（不读值、不打印值）；`docker` 守护进程版本（可 `--skip-docker`）；`external_connections_observation` 是公开测试里的静态网络线索，**只提出问题、不改声明**；`public_baseline` 按真实 Run 的装配方式跑一次公开检查并用正式验收的同一个解析器读报告，给出 `target_statuses`（两个期望失败）、`controls`（两个对照通过）、`command_sha256`、`public_tests_sha256`、`files_in_checkout`（应为空）、`child_environment_removed`。**摘要范围要看清**：`command_sha256` 覆盖装配后的命令、其中含本次的副本绝对路径，**只在同一 artifact 根内可比**；跨运行能比的只有逐文件内容摘要 `public_tests_sha256`。子进程启动前会移除名字含 `API_KEY`/`TOKEN`/`SECRET`/`PASSWORD`/`CREDENTIAL` 的环境变量（只处理名字），测试输出默认不返回，需要时才加 `--show-output`。最后一行是 `{"preflight": ...}`，**退出码 0 才算通过**。

   **这一步不是基线证据**：它不写 Run/Verification，真实 Run 仍必须自己记录一次“补丁前失败”的基线；这里通过的只说明任务包、解释器、checkout 与装配彼此一致。

1. `python -m devflow_ai.task_pack benchmarks/click_3019_public` 复核清单。
2. **门禁**：`independent_suite` 与 `external_connections` 由可信方定版后，再建 Task（在此之前建 Task 会占掉唯一一次尝试）：
   `python -m devflow_ai.benchmark_task --pack ... --policy benchmarks/click_3019_public/policy.draft.json --repo <checkout> --operator <姓名>`。
3. 一次真实运行（首次会花掉 1,200 秒 / 80,000 token；没有重试参数）：

   ```
   python -m devflow_ai.benchmark_run --pack benchmarks/click_3019_public --task-id <TASK_ID> --operator <姓名> --python-executable <绝对路径解释器> --workspaces-root <WORKSPACES_ROOT> --artifacts-root <ARTIFACTS_ROOT> --record <RECORD_DIR>\click-3019-attempt-1.json --policy benchmarks/click_3019_public/policy.draft.json --material <可信材料 JSON> --attempt-kind formal --confirm-run
   ```

   未提供 `--material` 或 `external_connections=unknown` 时会告警：首轮合格结果上限是 `public_only_pass`。
   环境演练必须用 `--attempt-kind rehearsal`，并且另建一个**演练 Task**（正式 Task 的一次尝试不能被演练用掉）；`attempt_kind` 是记录的正式字段，只有 `formal` 进正式样本，演练与未知类别默认被汇总排除。
4. 判读记录：`python -m devflow_ai.benchmark_record summarize <record.json>`（结果口径见 runbook 第 7 节）。
5. 人工审核写到新文件（原始记录不改）：
   `python -m devflow_ai.benchmark_record review --record <record.json> --conclusion accepted|rejected|pending --reviewer <姓名> --at <UTC 时间>`；
   汇总时对审核副本求和。完整方案与结果保存位置见 `docs/FIRST_RUN_PLAN.md`。

## E. 明确不能据此宣称的事项

- 不能因为专项测试通过就声称真实任务已解决：解决需要正式通过 **且** 人工审核通过。
- 不能用构造 artifact 的用例代替 Docker 独立评测。
- 不能因为手动基线复现成功就声称任务包自动运行已接通。
- 不能因为构造 Git 夹具上的贯通用例通过，就声称真实 Click 任务包已自动跑通。
