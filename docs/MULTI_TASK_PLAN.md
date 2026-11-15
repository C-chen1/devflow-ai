# 多任务验证方案（下一阶段）

目标：在同一套证据规则下跑**多个**独立任务，并给出可以复核的统计，而不是"一次通过就算系统可用"。
本文只列必要的输入与做法；没有核验过的事实一律标 `未核验`，不编造仓库、commit 或基线结果。

## 1. 现状（已核验）

Click #3019 有一次正式通过并经人工审核（Task `e40e9c8d-…`、Run `80a7ba3b-…`、
Verification `29e07087-…`）；其前两次尝试为 `incomplete`。Click #3084 已进行两次正式尝试，
均为 `incomplete`，尚未产生通过结果。`incomplete` 不是补丁验证失败，但如果属于所选的正式样本，
仍计入样本分母；不能仅挑出通过记录后把 1/1 当成全部尝试的成功率。
汇总必须明确所选记录、排除项与去重结果；一次通过不能证明多任务稳定性。

## 1.1 第二个任务：Click #3084（已完成两次正式尝试，尚未通过）

- Issue：[pallets/click#3084](https://github.com/pallets/click/issues/3084) "Bug: Optional value not optional anymore"，
  2025-09-24 创建、2025-11-20 关闭（GitHub API 读取，未凭记忆填写）。
- 缺陷：`is_flag=False, flag_value=<value>` 的选项自 8.3.0 起不再接受"只给选项、不给值"，直接 `SystemExit(2)`。
- 基准 commit：`7f7bbe4569ea68e8dabee232eade069ef3310aea`（`stable` 分支上修复 PR #3152 的父提交，2025-11-18）。
  Windows 侧已核对 checkout 的 HEAD、tree、工作区和公开基线；浅仓库经第三方代理取得的来源限制仍保留。
- 参考修复（只作为"公开检查能区分缺陷与修复"的对照，不是隐藏材料）：PR #3152，分支 tip
  `955ca492a6b9e8de0660dae1c415568ca6f9c4b3`，合并提交 `ebcd548d…`（2025-11-20 合入 `stable`），随后随 8.3.2 发布。
- 本地复现（Linux 沙箱，Python 3.11.17、pytest 9.2.0）：基准 commit → **1 failed / 2 passed，退出码 1**，
  失败节点 `scenarios::test_flag_value_used_when_the_flag_is_given_alone`，另两条为环境对照；参考修复 → **3 passed**。
  这次复现用的是 coding 侧下载的 tarball，**不是**可信 checkout，也**不是** Windows 固定解释器。
- 任务包草案已建：`benchmarks/click_3084_public/`（`task.json`、`scenarios.py`、`policy.draft.json` 版本
  `benchmark-click3084-v1`、`README.md`），用 `python -m devflow_ai.task_pack` 校验通过。
- 供 Windows 独立复核的产物：`benchmarks/click_3084_public/click3084-public-checkouts.tar.gz`
  （sha256 `47a1d209…87fd`），内含 `base/`（HEAD `7f7bbe4…`、tree `260e8630…`）与 `fix/`（HEAD `955ca49…`、tree `00e4ad80…`）
  两个已 checkout 的浅仓库，各 144 个跟踪文件；用项目自己的 `preflight --checkout <base>` 跑过：
  `checkout.head` 等于清单 commit、`dirty=false`、`public_baseline` 目标 failed + 两对照 passed、`files_in_checkout=[]`。
  说明：bundle 做不到——浅克隆的父提交缺失，`git clone` 会拒绝；因此交付浅仓库而不是 bundle。
  来源是第三方代理（沙箱到 github.com:443 不通），对象按 SHA-1 校验、tree 与 GitHub API 一致，但**不等于可信 checkout**。
- Windows 候选环境 Python 3.11.15 / pytest 9.1.1 / colorama 0.4.6 已单独复现公开基线；
  该任务自己的独立材料已在可信侧校准，Policy 与 `external_connections` 声明已固定。
- 两次真实尝试均因预算不足停在完成任务之前，均为 `incomplete`；第二次范围读取覆盖了关键定义，
  但 Planner 未获得模型预算。后续补充的覆盖合并和预算提示尚未经过真实运行验证。

## 2. 后续新任务需要的输入（缺一不可）

下表适用于未来尚未准备的任务；Click #3084 的已核验状态以 §1.1 为准。

| 项 | 要求 | 状态 |
| --- | --- | --- |
| 可信仓库 | 可信方指定，且能在目标机上 clone | 后续新任务未核验 |
| 基准 commit | **完整 40 位**，不得用分支名或短哈希；必须是未修复的缺陷版本 | 未核验 |
| 问题与范围 | 净化后的 issue、允许/禁止修改的路径 | 未核验 |
| 公开检查 | 命令、目标测试、`pythonpath_relative`（如必须导入被 pin 的源码） | 未核验 |
| 环境 | 固定解释器绝对路径、pytest 等依赖版本 | 未核验（沿用已知可用的 Python 3.11.15 / pytest 9.1.1 需在该任务上重验） |
| 基线复现 | **实际跑出**目标测试失败、对照通过，并记录节点名与退出码 | 未核验（必须在目标环境复现，不能引用别处的数字） |
| 可信独立材料 | `suite_version` + `materials_sha256` + adapter argv + 用例 + 两个校准工作区；校准结果必须是缺陷失败/参考通过 | 未核验 |
| `external_connections` | 由可信方核验后声明；`unknown` 时正式门禁会拒绝 | 未核验 |

任务包放 `benchmarks/<pack_id>/`，用 `python -m devflow_ai.task_pack` 校验；清单里每个未定项都要在 `unverified` 声明。

## 3. 每个任务都走同一条链（不跳步、不重试）

1. `preflight`（含 `--material` 摘要校验、`--check-db`、公开基线）→ 退出码 0 且 `formal_acceptance_blockers` 为空；
2. `benchmark_task` 建**新** Task（一个 Task 一次尝试）；
3. `benchmark_run --attempt-kind formal --confirm-run`（1,200 秒 / 80,000 token / 1 次尝试 / 0 重试，不放宽）；
4. `usage_report --run <new> --compare <previous>` 记录逐角色用量与到达阶段；
5. `benchmark_record summarize` → 真人审核 → `review` 写审核副本 → 再 `summarize`；
6. 若记录摘要与证据不一致，用 `benchmark_record correct` 生成**新的**修正副本（`correction` 块指向原记录与审核副本的 sha256），
   原文件与审核副本都不改写。

## 4. 统计口径（提前写死）

- 样本 = 标记为 `formal` 的正式尝试数；`rehearsal` 与 `unknown` 默认排除（`--include-non-formal` 才计入）。
- 成功率 = `solved / sample`，分母必须报出来；`formal_pass` 待审或被否决分别单列。
- 到达阶段（`stages`/`stopped`）与用量一起报：token 更少但阶段更浅，不算优化成功。
- 每个任务的成功都必须同时具备：正式 Verification `trusted_v1/passed` + 真人审核通过；缺一只能记 `incomplete` 或相应标签。

## 5. 风险与边界

- 每个任务都要**自己的**独立材料与校准；不能把一个任务的校准结果复用到另一个任务。
- 预算不放宽；出现普通错误就如实记录，不为掩盖而新建第二次尝试。
- 隐藏材料不进仓库、工作区、Agent 上下文或普通日志；可信侧保管与注入。
- 后续新任务在核实仓库来源、40 位 commit 与实际基线之前，只停在准备阶段，不建正式 Task。
