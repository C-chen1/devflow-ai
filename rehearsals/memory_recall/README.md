# 跨任务记忆召回的开发演练（准备材料）

**Codex Windows 复核（2026-11-18）：** 使用独立临时目录、模型桩、独立 PostgreSQL schema 和本地构造镜像，实际运行了正式 Verification → 记忆摄入/审核 → 另一 Task 召回及独立评测拒绝的测试；其正式链手工构造了阶段交接，不代表默认调度器或真实模型任务贯通。单独在另一临时目录验证了先准备、构建镜像、再对同目录执行 `--calibrate`，结果为 `verified`；重用时也会拒绝仓库和校准工作区的未提交或未跟踪改动。计划文件本身及全部任务包文件未受独立可信摘要保护，仍需可信操作者核查。这些都是构造证据；`--confirm` 仍禁用，真实演练需要单独确定隔离库、任务、预算和审核。

**Codex Windows 复核（2026-11-18）：** 使用独立临时演练目录及独立本地镜像标签，对构造套件调用
`calibrate_stdio_suite`，得到 `verified`（基线 `failed`、纠正版本 `passed`，镜像
`sha256:1117bebd…6817c`）。那是该临时工件的构造校准，不自动验证其他目录、镜像标签或材料，也不代表
真实任务通过。`--confirm` 仍禁用。

**当前状态（2026-11-18 本批更新）：** 材料仍是**一个**开发仓库、**一个**基线 commit、**两个实质不同**的
开发问题（`src/pricing.py` 空购物车 / `src/slug.py` 折叠空白），适配器在镜像内、案例经 stdin 送入。

本批做了两件事：

1. **`--calibrate` 现在可以重跑。** 镜像只能从 `<root>/adapter` 构建，而它要等第一次准备后才存在，
   所以"新目录 + 先构建镜像 + 再校准"原本无法在同一条命令里完成。现在 `--calibrate` 作用在**已准备**
   的根目录上时只核对适配器/材料摘要与三个 commit，然后重跑校准并写回 `plan.json`，不重写任何材料；
   摘要或 commit 一变就拒绝。本环境仍无 Docker，结果照实打印 `unavailable` 并新增
   `prerequisite`（`docker_missing` / `image_missing` / `present`）。
2. **新增构造端到端路径的测试**（`tests/test_memory_recall_formal_chain.py`）：正式验收 → 记忆 →
   另一 Task 召回 → 独立评测隔离。它需要 Windows Job 的 pytest 捕获与本地评测镜像，两者都没有时
   **整体 skip**；本环境（Linux，无 Docker）跳过，未被当作通过（见 §7.2）。

`--confirm` 继续禁用（写库前要先审 §9.1）。`tests/test_memory_recall_rehearsal.py` 已由原窗口恢复，
本批未触碰。

本目录是**演练准备**，不是演练结果，也不是已验证可真实执行的方案。来源 Task 的安全准入和正式验收
路径仍需补齐并验证；本窗口没有调用任何真实模型，唯一的实跑是隔离 PostgreSQL + 模型桩的
预验证（见 §7）。

相关但**不改动**的东西：`docs/SPEC.md §10.6`（长期经验记忆）与 `§10.7`（记忆对照评测）的设计、
`docs/DEPLOYMENT.md` 的命令说明、`docs/RSI_READINESS.md` 的门槛清单、
`tests/test_memory.py`、`tests/test_worker_run.py`。

本批新增的只有四样：

| 文件 | 作用 |
| --- | --- |
| `rehearsals/memory_recall/README.md` | 本文：设计、准入核实、操作序列、证据边界、真实运行方案 |
| `rehearsals/memory_recall/checks/evidence.sql` | 只读核查 SQL（8 条 SELECT，已对真实迁移链 schema 语法验证） |
| `rehearsals/memory_recall/pack-template/task.json.template` | 开发 Task 的任务包模板（复制为 `task.json` 后替换所有 `<...>` 与占位 commit） |
| `tests/test_memory_recall_rehearsal.py` | 预验证：两个 Task 的召回接线与各道拒绝闸门（隔离 schema + 模型桩；原窗口恢复，本批未改） |
| `rehearsals/memory_recall/prepare.py` | 一次性准备脚本：仓库 + 两个 pack + 适配器/镜像配方 + 构造材料 + 两个校准工作区 + `plan.json`；`--confirm` 禁用、`--calibrate` 真调校准入口 |
| `tests/test_memory_recall_prepare.py` | 准备脚本的 14 项验证（含"两个问题都开放"、"适配器只吃 stdin + 挂载工作区"、"校准只尝试不伪造"、"构造来源跨 Task 预览与召回"、"已准备根目录才可重跑校准且摘要/commit 必须一致"） |
| `tests/test_memory_recall_formal_chain.py` | 构造端到端：正式验收（`finalize_with_independent_evaluation`）→ `ingest` → 审核 → 目标 Task 召回 → 独立评测隔离；Windows + `DEVFLOW_TEST_EVALUATION_CONTAINER=1` 才运行 |

没有修改实验账目、Skills 代码、`docs/PROGRESS.md`，也没有提交、迁移业务库、改历史 Run。

---

## 1. 已核实的准入条件（代码事实，不是假设）

### 1.1 摄入（`MemoryService.ingest`，`src/devflow_ai/memory.py:164-273`）

一条候选要写进 `memory_items`，必须同时满足：

1. **来源 Task 不是已知 benchmark**：`task.admission_record.evidence` 里任何以 `pack:` 开头的字符串
   都会让摄入被拒（`memory.py:56-70`、`181-182`）。
2. **Run 属于该 Task 且 `status = PASSED`**（`memory.py:183-191`）。失败的、仍在跑的、别的 Task 的
   Run 都不行。
3. **`task.repo` 与声明的 `--scope-repository` 完全相等**（`memory.py:192`）。仓储字符串只是范围标签，
   不是身份认证。
4. **Verification 属于该 Run、`result = passed`、`evidence.scope = trusted_v1`、
   `evidence.suite_version` 非空**（`memory.py:195-207`）。少任何一条都拒。
5. `--source-kind verification` 时不得同时引用 Task review；`--source-kind task_review` 时必须引用一条
   同一 Task/Run/Verification 且结论为 `accepted` 的 review（`memory.py:306-311`、`320-337`）。
6. 正文去空白后非空且 ≤ 4000 字符；`content_status` 恒为 `unverified_candidate`，数据库约束固定
   （`models.py:349-351`）。

注意第 4 条的现实含义：**`trusted_v1` 只有一处能写入**——`RunCoordinator.finalize_passing_verification`
（`coordinator.py:1968-2047`），而它要求校准过的独立评测回执、`external_connections = none`、固定套件版本、
可信基线命令证据，并且是唯一把 `Run` 置为 `PASSED` 的代码路径（全仓库仅 `coordinator.py:2045`）。
换句话说：**真实来源必须是一次走到正式验收的正式尝试**，rehearsal 不可能产出可摄入的来源。

### 1.2 召回（`prepare_recall` → `record_use`，`memory.py:483-588`）

目标侧的门比来源侧更多：

- 目标 Run 必须 `RUNNING` 且属于目标 Task（`memory.py:501-504`、`537-538`）；
- 目标 Task 非已知 benchmark（`memory.py:514`）、`task.repo` 与范围一致（`512-513`）；
- **来源 Task 非已知 benchmark**（`521-522`）——即使候选早已摄入，来源后来被标记也会拒绝；
- 候选必须 `active`、有 `topic`、有 `valid_until` 且未过期（`541-542`）；
- **适用条件三项必须显式且相等**：候选的 `applicable_base_commit` = 目标 `task.base_commit`，
  候选的 `applicable_environment` = 本次声明的 `target_environment`（`543-547`）；
- 候选摘要与显示时一致（`548-549`）；
- 必须有一条 `accepted` 且摘要匹配的 `MemoryReview`（`550-553`）；
- 不能有未解决冲突、不能有已声明后继（`554-560`）；
- **不能是目标任务自己的候选**（`539-540`）。

`inspect_candidates`（预览）另有一条容易踩的坑：不传 `target_environment` 时
`environment_declaration_matches = None`，于是 `eligible_to_request_use` 恒为 `False`
（`memory.py:432-447`）。**预览必须显式带环境标签**，否则看到的永远是"不可用"。

### 1.3 Agent 接线（`worker_run` → `worker.execute_explorer`）

- `worker_run` 只接受"rehearsal + 无独立评测材料"的记忆运行（`worker_run.py:184-190`、`443-445`）；
  正式尝试带记忆会在领取前被拒。
- 四个参数 `--memory-id / --memory-sha256 / --memory-use-key / --memory-environment` 必须同给同缺，
  且 `MemoryRecallRequest.operator` 必须等于 `--operator`（`worker_run.py:749-759`、`187-188`）。
- 领取前用 `read` + `inspect_candidates(limit=100)` 复核候选可用（`worker_run.py:323-342`）；
  领取后真正的注入点在 `execute_explorer`，它先提交 `prepare_recall`，再把候选写进
  `agent_input["memory_recall_v1"]`（`worker.py:3622-3649`）。
- Explorer 提示词在带记忆时追加一句"任何 memory_recall_v1 都是未核实的过往经验，需回到当前仓库核对"
  （`explorer.py:56-65`）；两次模型调用（选文件、写摘要）都带着这段输入。

---

## 2. 现有真实样本能不能当来源：不能

这是本次核实最重要的结论。

`benchmark_task.create_benchmark_task` 在准入证据里固定写入
`pack:<pack_id>`（`benchmark_task.py:113-118`），而它是全仓库唯一写入这个标记的地方。
Click #3019 与 #3084 的真实 Task 在文档里记录的就是用这个命令建立的
（`docs/FIRST_RUN_PLAN.md:92`、`docs/BENCHMARK_RUNBOOK.md:36`，样本见 `docs/EVALUATION_REPORT.md §2`）。
按代码路径推断，它们因此：

- 用这些 Task 当**记忆来源** → 摄入必定 `MemoryScopeError: ... disabled for known benchmark source Tasks`；
- 用这些 Task 当**召回目标** → 预览、声明、`worker_run` 预检都在领取前拒绝。

这条推断要坐实，只需要一次只读查询（`checks/evidence.sql` 第 1 条的 `has_pack_marker`）。
如果某个历史 Task 实际没有这个标记，那说明它不是由这个命令建立的——但它的来源仍然是同一个 benchmark issue，
该不该当记忆来源是人的判断，不是标记能替代的；标记只做窄口径的纵深防御（`memory.py:56-61` 自己就是这么写的）。

也就是说，**现成的那一次真实 `formal_pass`（Task `e40e9c8d…` / Verification `29e07087…`）在当前防线里
不应被当作合法记忆来源**，除非只读核查证明它不是 pack Task——而那还需要额外说明为什么可以接受。

必须由操作者用只读 SQL 确认实际的行，而不是相信本文或任何转述：
`checks/evidence.sql` 第 1 条会直接给出 `has_pack_marker` 与原始 evidence 列表。

**不要**用"复制一份包、换个 `pack_id`、绕开标记"的方式把 benchmark 任务洗成开发任务。那会让记忆的来源
重新变成已知 benchmark，正是这条防线要挡的事；本文不提供这种做法，也不建议在任何时候这样做。

---

## 3. 两个开发 Task 的演练设计

```
仓库（唯一） R = prepare.py 生成的本地开发仓库，Task.repo 就是它的路径字符串
基线 commit C = R 的 HEAD（两个 Task 都用同一个 C，且没有第二个仓库）

Task S（来源，正式尝试）                Task T（目标，rehearsal）
───────────────                        ──────────────
问题 A：src/pricing.py                   问题 B：src/slug.py
  空列表 → ZeroDivisionError               重复空白/标点留在 slug 里
ELIGIBLE @ R/C，非 pack 标记            ELIGIBLE @ R/C，非 pack 标记
一次 formal 尝试 → Run PASSED           一次 rehearsal，领取后立刻进 Explorer
  → Verification trusted_v1/passed        → Explorer 输入带 1 条候选
  → 人工写一条经验 M（topic=T0）           → 写 1 条 MemoryUse，无 Verification
M 的适用范围：repository=R、base_commit=C、环境标签 E
```

**两个问题必须实质不同，经验才带得过去。** 问题 A 与问题 B 在同一个基线 commit 上同时是**开放**的
（两个公开检查在 `C` 上都会失败，见 §7.1 的真实 pytest 运行），纠正工作区只修 A、B 仍然失败。
因此 M 里能复用的只能是"这个仓库怎么组织、公开检查怎么跑、失败通常长什么样、改哪里不越界"这类
**仓库结构与公开检查经验**；如果把 A 的补丁本身写进 M，对 B 没有任何帮助——那就不是跨任务召回，
只是一次抄袭演练。

**两个 Task 必须共用同一个仓库字符串和同一个基线 commit。** `record_use` 要求候选的 `scope_value`
等于目标 `Task.repo`、候选的 `applicable_base_commit` 等于目标 `Task.base_commit`
（`memory.py:483-588`）；两个不同仓库的两个 Task 永远无法互相召回，旧草案的"两个仓库"因此是错的。

为什么必须是**两个不同的 Task**：`record_use` 拒绝"目标 Task 自己的候选"
（`memory.py:539-540`）；`inspect_candidates` 也把 `task_id == target` 的候选过滤掉（`memory.py:388`）。

为什么必须是**同一个 base commit**：召回要求候选的 `applicable_base_commit` 等于目标 `task.base_commit`。
操作者当然可以在摄入时声明"这条经验适用于目标那个 commit"，但若不是真的成立的判断，那只是把声明写得好看。
两个 Task 同 commit、不同 Issue，是能让这条经验（关于仓库结构、公开检查怎么跑、失败通常出在哪）**真实可用**
的最小安排。经验内容建议写成"环境/定位类"而不是"这个补丁怎么改"，否则第二个 Issue 用不上。

为什么要**开发 Task**而不是 pack Task：见 §2。开发 Task 的准入证据里不含 `pack:`，例如
`["dev-task:<operator>/<reason>"]`；`optional_scope_binding_matches` 在没有 `scope-sha256-v1:` 证据时
返回 True（`task_pack.py:87-101`），因此**不带包范围绑定的旧式/开发式 Task 仍可被 `worker_run` 驱动**，
但拿不到范围校验——这一点要在记录里写明，不要宣称范围受约束。

### 3.1 本批已补：一次性准备脚本（不是通用入口）

`rehearsals/memory_recall/prepare.py` 用一个脚本把"两个开发 Task 的全部输入"摆出来，内部只调用
**现有**的 `TaskService.create_task` + `apply_admission_decision`，不新增 CLI、不改准入/权限检查：

```powershell
# 只准备材料（不连数据库）：一个开发仓库、两个任务包、一份构造材料、plan.json
uv run --locked python -m rehearsals.memory_recall.prepare --root D:\devflow-rehearsal

# 再准入两个 Task；--database 必须与连接的库名完全一致，防止误写
$env:DEVFLOW_DB_NAME = 'devflow_mem_rehearsal'      # 隔离库，不是案例库
uv run --env-file .env.rehearsal --locked python -m rehearsals.memory_recall.prepare `
  --root D:\devflow-rehearsal --operator <操作者> --confirm --database devflow_mem_rehearsal
```

脚本产出（`plan.json` 是唯一事实来源，后文所有占位符都从它取值；`plan.json` 里的 `pending_review`
列出了尚未被验证的三件事）：

| 路径 | 内容 |
| --- | --- |
| `<root>/repo/dev-rehearsal` | **一个**小仓库，两个**不同**的未修复问题：`src/pricing.py`（问题 A，来源）与 `src/slug.py`（问题 B，目标）；`git init` + 一个基线 commit `C` |
| `<root>/packs/dev-source`、`<root>/packs/dev-target` | 两个 Task 的 `task.json`（同一 `base_commit=C`、范围 `src/`（禁 `benchmarks/`）、各自公开检查、`unverified` 声明齐全）+ 各自的 `scenarios.py`（`scenarios::test_…` 形式的目标用例名，与 JUnit 记法一致） |
| `<root>/adapter/adapter.py`、`Dockerfile`、`README.md` | 镜像内适配器：stdin 收案例、打印单行结果；`FROM python:3.11-slim` + `COPY adapter.py /suite/adapter.py`；镜像名 `devflow-rehearsal-adapter:v1`，本地构建、`--pull never` |
| `<root>/material/dev-source-material.json` | 可信侧**构造**材料：`image`/`command` 指向上面的适配器，案例为 `{"prices": []}` → `0`；其字节摘要被写进来源包的 `independent_suite.materials_sha256` |
| `<root>/calibration/baseline` | 问题 A/B 都未修的基线工作区（commit `C`） |
| `<root>/calibration/corrected` | **只修 A** 的独立副本（commit `C1 ≠ C`，材料里的 `expected_commit` 指向它；B 仍失败） |
| `plan.json` | 仓库路径、基线 commit、两个 pack 摘要、材料摘要、适配器构建命令与校准 commit/结果，以及待审项清单 |

`--confirm`（写 Task）目前**被禁用**：脚本在写库前退出 2，并提示先完成
「隔离数据库身份与准入方案（待审）」的审阅。根目录必须是**不存在或空**的目录，否则拒绝写入。

`--calibrate` 调用现有的 `calibrate_stdio_suite` 跑一遍两个工作区，把结果写进 `plan.json` 的
`calibration_result`。它需要 Docker 守护进程与本地已构建的适配器镜像；两者缺一，结果为
`unavailable`（带 `baseline_outcome`/`corrected_outcome`/`image_id=null`/`prerequisite`），脚本照实
打印，**不会**把"文件能解析"或"两个 commit 不同"当成校准成功。`verified` 时 `image_id` 是镜像的
`sha256:` 身份，必须一并写进演练记录——那才是"这套材料确实拒绝了基线、接受了纠正"的证据。

正确的执行顺序（本批修复的可复现性问题）：

```powershell
# 1) 准备到新目录（不连库、不调模型）
python -m rehearsals.memory_recall.prepare --root D:\devflow-rehearsal
# 2) 用该目录里的适配器源码构建本地镜像（评测端 --pull never，不联网）
docker build -t devflow-rehearsal-adapter:v1 D:\devflow-rehearsal\adapter
# 3) 在**同一个**已准备的目录上重跑校准：只核对摘要与 commit，不重写材料，结果写回 plan.json
python -m rehearsals.memory_recall.prepare --root D:\devflow-rehearsal --calibrate
```

第 3 步在根目录已存在时走"重用"分支：`adapter.py`、`Dockerfile`、材料字节或三个 commit
（仓库 `C`、baseline `C`、corrected `C1`）任何一处与 `plan.json` 记录不符就拒绝，否则会变成"校准的
不是材料声明的那个东西"。**校准结果绑定到具体的适配器字节与镜像身份**，所以换镜像标签或改适配器后
必须重跑。

两个 Task 的准入证据是 `dev-task:<operator>:<reason>`：**不含** `pack:` 标记，所以不会被当作 benchmark
Task；脚本也不写 `scope-sha256-v1:`，因此**范围 Skill 在这两个 Task 上不可用**（见 §5 的边界）。
脚本不创建 Run、不调用模型、不摄入/召回记忆、不执行评测。

---

## 4. 操作草案（只能执行到准备与只读核查；写库与真实运行都未授权）

变量约定：全部来自 `plan.json`——`$S`/`$T` 两个 Task ID、唯一的 `$REPO` 仓库路径与唯一的 `$C` 基线
commit（两个 Task 共用）、`$MATERIAL` 材料路径、`$E` 环境标签（例如 `dev-box-win11-py311`）、`$T0` 主题
（例如 `repo-layout`）、`$RUN` 运行根目录（仓库外）。

### 4.0 准备（一次性脚本，不花模型）

见 §3.1。跑完后核对 `plan.json`：两个 Task 的 `repo` 与 `base_commit` **相同**、来源包的
`independent_suite.materials_sha256` 等于 `material.sha256`、`calibration.baseline_commit` 与
`corrected_commit` **不同**、材料里的 `image`/`command` 等于 `adapter.image`/适配器路径。
`--confirm` 当前退出 2，不会创建 Task；准入要等 §9.1 的方案被审阅。**材料在校准通过前不能用于正式尝试**：
先把 `<root>/adapter` 构建成 `devflow-rehearsal-adapter:v1`，再在演练机上对**同一个**根目录重跑
`prepare.py --root <root> --calibrate`，确认 `calibration_result.outcome == "verified"` 并抄下 `image_id`。

### 4.1 只读核查（不花模型）

```powershell
python -m devflow_ai.preflight --help            # 不迁移、不建 Run、不调模型
psql $DSN -v task_id=<S> -v run_id=<S 的 Run> -v memory_id=00000000-0000-0000-0000-000000000000 `
          -v target_task_id=<T> -v target_run_id=00000000-0000-0000-0000-000000000000 `
          -f rehearsals/memory_recall/checks/evidence.sql
```

第 1 条若显示 `has_pack_marker = t`，停：这个 Task 不能当来源。

### 4.2 来源 Task：一次正式尝试（唯一的高成本步骤）

**阻塞：没有经过验证的来源 Task 准入与运行步骤，暂不执行真实来源任务。**
`benchmark_task` 会写入 `pack:` 标记，即使该 Task 正式通过，`memory ingest` 仍会拒绝；
不得以该入口创建来源 Task，也不得通过修改标记绕开限制。开发 Task 的准入、Policy、
范围绑定及正式评测材料须先形成并验证完整方案，再明确预算与证据范围。

前提条件（缺一个就到不了 `PASSED`，也不该假装到了）：Windows（可信 pytest 基线报告依赖 Job 捕获，
`worker_run.py:142-149`、`195-201`）、Docker 守护进程（独立评测容器）、由可信侧**在仓库外**保管并注入的
独立材料（pack 必须声明 `independent_suite` 的版本与 `materials_sha256`）、真实模型。

### 4.3 写入一条记忆并审核（不花模型）

```powershell
# 1) 摄入：来源必须是 §4.1 那条 PASSED Run 与其 trusted_v1 Verification
python -m devflow_ai.memory ingest --identity mem-rehearsal-<日期>-01 `
  --scope-repository $R --source-kind verification `
  --task-id $S --run-id <S 的 Run> --verification-id <trusted_v1 verification> `
  --content-file D:/devflow-rehearsal/memory-text.md `
  --topic $T0 --valid-until 2026-12-31T00:00:00+08:00 `
  --applicable-base-commit $C --applicable-environment $E --created-by <姓名>

# 2) 取候选摘要：ingest 不回显 candidate_sha256，必须再读一次
python -m devflow_ai.memory read --scope-repository $R --memory-id <候选 UUID>

# 3) 预览：必须带 --target-environment，否则永远 not eligible
python -m devflow_ai.memory preview --scope-repository $R --target-task-id $T `
  --topic $T0 --target-environment $E

# 4) 审核：用第 2 步拿到的 candidate_sha256，审核的是"这一条候选"
python -m devflow_ai.memory review --scope-repository $R --memory-id <候选 UUID> `
  --candidate-sha256 <sha256> --conclusion accepted --reviewer <姓名> --reason "<审了什么>"
```

审核那一步是**人事判断**：这条陈述对不对、完不完整、能不能给模型看、有没有夹带隐藏评测材料。
系统不做任何内容检查（见 `memory.py` 模块文档字符串第 22-26 行的边界声明；`content_status` 恒为
`unverified_candidate`）。

### 4.3.1 任务包

`pack-template/task.json.template` 复制成 `task.json` 后替换所有 `<...>`，再用
`python -m devflow_ai.task_pack <包目录>` 校验格式。注意：**格式通过只代表字段形状对**，
占位 commit 也能通过校验，不代表这个包或这个 commit 可用。来源 Task 的包必须把
`independent_suite` 换成真实的 `IndependentSuiteRef`（版本 + `materials_sha256`）并删掉对应的
`unverified` 条目，否则正式尝试一定停在草稿、拿不到 `trusted_v1`。

### 4.4 目标 Task：一次 rehearsal 召回

```powershell
uv run --env-file .env --locked python -m devflow_ai.worker_run `
  --pack <root>/packs/target --task-id $T --policy <root>/policy-target.json `
  --operator <姓名> --python-executable C:/.../python.exe `
  --workspaces-root $RUN/workspaces --artifacts-root $RUN/artifacts `
  --attempt-kind rehearsal --confirm-run `
  --memory-id <候选 UUID> --memory-sha256 <sha256> `
  --memory-use-key <新 UUID> --memory-environment $E
```

- 不给 `--material`、不给 `--attempt-kind formal`：给了会在领取前被拒（这是预期）。
- 目标 Task 的 pack 不要声明 `independent_suite`（rehearsal 用不上，声明了反而要材料）。
- `policy-T.json` 必须逐节等于该 Task 存的 `policy_snapshot`（`worker_run.py:311-315`）；
  照 `tests/test_worker_run.py:_policy_file` 的做法从库里读出来再写。

### 4.5 检查证据

```powershell
psql $DSN -v task_id=$S -v run_id=<S 的 Run> -v memory_id=<候选 UUID> `
          -v target_task_id=$T -v target_run_id=<T 的 Run> `
          -f rehearsals/memory_recall/checks/evidence.sql
```

看第 3、4、5、6、7、8 条：候选与审核、声明行、Explorer 输入里的 `memory_recall_v1`、
以及"目标 Run 上没有 Verification"和"目标 Run 仍是 RUNNING"。

---

## 5. 哪些结果能证明"系统传入了经验"，哪些不能

### 能证明的（到网关调用边界为止）

1. **一行操作者声明**：`memory_uses` 里有 Task/Run/候选摘要/审核引用/目标 base commit/环境标签，
   写入发生在该 Run `RUNNING` 期间，且是在锁下逐条复核候选状态后提交的（`memory.py:497-574`）。
2. **Explorer 的 Agent 输入里确实有这条候选**：`agent_executions.input ? 'memory_recall_v1'`，
   含 `candidate_id`、`candidate_sha256`、`content`、`content_status=unverified_candidate`、
   来源 Task/Run/Verification、`use_id`（`worker.py:3636-3649`）。这是**系统构造的输入**的字节级证据。
3. **它进入了发给网关的 prompt**：`explore_one_file` 把 `agent_input` 序列化进 prompt 字符串
   （`explorer.py:56-65`），两次调用都带。
4. **这个 Run 不会产出正式结果**：`verifications` 里没有该 Run 的行；`runs.status` 仍是执行到的状态；
   `assert_independent_memory_isolation` 对该 Task 的任何 Run 都抛错（见 §6）。

### 不能证明的（不要写进任何结论）

1. **内容正确、完整、无隐藏材料**：系统没有内容核验，也没有隐藏材料扫描；`source_state=trusted_passed`
   只说"所引用的那次 Run 有 trusted_v1 通过"，`content_status` 仍是 `unverified_candidate`。
2. **模型读到并采纳了它**：网关只记录用量，不保存 prompt 与回复原文；Explorer 输出只有 summary。
   "prompt 里有"与"模型用了"之间没有任何字节级证据。
3. **效果改善**：没有对照 Run、没有独立评测、样本是 1。不能说"召回让这次更好"。
4. **系统自动选中了它**：选择是可信操作者显式给的，默认调度器不自动选取（`DEPLOYMENT.md:166`）。
5. **其它渠道没注入过**：标记只覆盖这条接线。手工往库里塞、或别的通道把内容喂给模型，
   `assert_independent_memory_isolation` 看不见（`docs/PROGRESS.md` 2026-11-18 记录已写明）。
6. **仓库范围受约束**：开发 Task 的准入证据没有 `scope-sha256-v1:`，`optional_scope_binding_matches`
   直接放行，范围 Skill 不会生效。

---

## 6. 这个演练为什么不参加独立评测

三层，各自独立：

1. `worker_run` 在**领取前**要求 `memory_recall` 只与 `attempt_kind=rehearsal`、无 `--material` 同时出现
   （`worker_run.py:184-190`），并在 `run_worker_once` 里再查一次（`443-445`）。
2. 即使拿到 Verifier 草稿，`_finalize_stage` 对非 formal 直接把结果记为
   `{"outcome": "skipped", "reason": "attempt_kind_not_formal"}`，不进入可信事务（`worker_run.py:362-367`）。
3. 已经有记忆输入的 Task，任何 Run 都过不了 `assert_independent_memory_isolation`
   （`coordinator.py:2407-2416`，按 **Task** 查 `AgentExecution.input` 是否带键）；失败的完成、正式通过、
   恢复复用入口都在入口处拒绝（`worker.py:2334`、`coordinator.py:1987`）。

因此这次演练的结果**不得**进入任何正式样本、成功率、`counts_as_solved` 或评测报告；
它也不是"独立评测被绕过"的证据，只是标注清楚的开发演练。

它同样**不是** `SPEC §10.7` 说的"记忆对照评测"。那个口径要求固定模型、工具、任务、预算与评测开始时的记忆快照，
至少对照"无长期记忆 / 普通检索记忆 / 带范围来源时间冲突治理的记忆"三臂，并按 §10.5 的隔离规则运行。
本演练只有一臂、一次、没有对照组，因此不能回答"记忆有没有用"，只能回答"这条链路有没有把东西传进去"。

---

## 7. 本窗口已做的预验证（构造数据 + 模型桩）

`tests/test_memory_recall_rehearsal.py`，4 个用例，全部在隔离 PostgreSQL schema 中运行，模型全部是桩：

| 用例 | 它验证什么 |
| --- | --- |
| `test_a_pack_created_source_is_refused_while_a_dev_source_is_admitted` | 用**真实的** `create_benchmark_task` 造 Task（证据里确实有 `pack:`），它的来源摄入被拒；同形状但证据为 `dev-task:…` 的开发 Task 可以摄入，且 `source_state=trusted_passed`、`content_status=unverified_candidate` |
| `test_a_pack_created_target_refuses_preview_and_declaration` | pack Task 当目标时，预览与声明都在写任何 `memory_uses` 之前被拒 |
| `test_two_dev_tasks_run_a_recall_rehearsal_without_a_formal_result` | 完整接线：预览（不带 env → not eligible；带 env + 审核后 → eligible）→ CLI rehearsal → Explorer prompt 与 Agent 输入都含候选 → 恰好 1 条声明 → 目标 Run 没有 Verification → 隔离断言抛错 |
| `test_recall_is_refused_before_claim_when_the_declaration_does_not_fit` | 环境不符、base commit 不符 → 领取前 exit 2 且不建 Run；同一 `--memory-use-key` 不能花在第二个 Task 上 |

实际执行（Linux 沙箱、`pgserver` 自带 PostgreSQL、仓库副本与工作树的 156 个源码/迁移文件 md5 全一致）：

```
pytest tests/test_memory_recall_rehearsal.py -q       → 4 passed in 12.75s
pytest tests/test_memory.py tests/test_memory_recall_rehearsal.py tests/test_worker_run.py -q
                                                      → 117 passed in 111.88s
```

`checks/evidence.sql` 在完整迁移链建出的 schema 上跑过一遍（8 条 SELECT 全部解析并执行，返回 0 行）。

**这些都不是真实成功。** 来源 Run 与 `trusted_v1` Verification 是直接写进隔离 schema 的构造行
（和 `tests/test_memory.py` 的做法一样）；它证明的是"闸门与接线按代码所说工作"，
不证明"存在一个真实可用的来源"。

### 7.1 准备脚本的验证（本批新增，`tests/test_memory_recall_prepare.py`）

| 用例 | 它验证什么 |
| --- | --- |
| `test_prepare_uses_one_repository_and_one_baseline_for_both_tasks` | **同一个**仓库、**同一个**基线 commit 上的两个不同 Task；两个 `task.json` 都能被 `load_manifest` 解析、范围（`src/`、禁 `benchmarks/`）与公开检查齐全且公开检查互不相同；来源包固定的材料摘要等于脚本写出的字节摘要；目标包声明 `independent_suite` 缺失 |
| `test_the_two_tasks_are_distinct_open_defects_on_the_same_baseline` | 真的用 pytest 跑两个包的公开检查：在共享基线 `C` 上**两个都失败**（问题 A/B 都是开放的）；在 corrected 上 A 通过、B 仍失败——证明两件事是不同缺陷，且"纠正"没有顺手把 B 一起修掉 |
| `test_calibration_is_attempted_through_the_existing_entry_and_never_asserted` | 真调用 `calibrate_stdio_suite`：`verified` 时断言 `(failed, passed)` 且 `image_id` 形如 `sha256:…`；否则断言 `unavailable` 且 `image_id is null`，并核对 `prepare` 本身不写 `calibration_result`（只有 `--calibrate` 写） |
| `test_the_adapter_uses_the_material_cases_against_the_mounted_workspace` | 适配器在镜像内、`COPY adapter.py /suite/adapter.py`、`FROM python:3.11-slim`；材料的 `image`/`command` 与适配器一致；用材料的 stdin 分别打基线与纠正工作区：基线**不是崩溃**（退出 0 但输出不符，否则入口会记成 `unavailable`），纠正输出与期望字节完全相等 |
| `test_calibration_workspaces_are_different_commits_and_material_names_the_corrected_one` | baseline/corrected 是**不同 commit**，corrected 只带 A 的修复、基线不带；材料里的 `expected_commit` 指向 corrected；工作仓库仍停在基线 commit；`load_independent_material` 按包 pin 接受它 |
| `test_constructed_material_is_accepted_by_the_entry_placement_check` | 入口的 `require_material_placement` 接受该路径（在 pack/工作区/产物/源码之外），放进 pack 则被拒 |
| `test_one_changed_byte_of_the_material_is_refused` | 材料字节改动一个字符即被摘要拒绝 |
| `test_admit_tasks_creates_two_eligible_development_tasks` | 用现有 `TaskService` 准入两个 ELIGIBLE 开发 Task：repo/base commit 相同、Policy 快照落库、证据为 `dev-task:…`、重复运行返回同一对 Task |
| `test_scope_gate_accepts_the_shared_repository_and_refuses_another_one` | 目标 Task 用**自己的** `repo` 走 `prepare_recall` 时通过范围门（随后因候选不存在报 `Candidate missing`）；换成另一个仓库字符串则在范围门就被拒（`Target Task missing`）——这是"同仓库可召回、异仓库被拒"的对照 |
| `test_constructed_source_is_previewed_and_recalled_by_the_other_task_on_the_same_repo` | **构造**来源（Task + PASSED Run + `trusted_v1` Verification 全部由测试写入，明确不是真实通过）经 `ingest`、`review` 后：目标 Task 在同一仓库/基线/环境标签下 `inspect_candidates` 能找到它且 `eligible_to_request_use = True`（不传环境标签则为 False）；换一个仓库字符串被 `MemoryScopeError` 拒绝；`prepare_recall` 真的记录了 `memory_uses` 并回带候选，内容仍是 `unverified_candidate` |
| `test_development_admission_evidence_carries_no_pack_marker` | 证据不含 `pack:`；`admission_has_scope_digest` 为 False、`optional_scope_binding_matches` 为 True——范围 Skill 对开发 Task 不可用，且不会被误判为已绑定 |
| `test_confirm_is_disabled_and_a_non_empty_root_is_refused` | `--confirm` 退出 2 且不写任何东西；非空根目录被拒 |
| `test_calibration_can_be_rerun_on_a_prepared_root_only_while_it_is_unchanged` | 重跑校准不重写材料（逐文件比对整棵准备目录）；`adapter.py` 改动一个字节、或 baseline/corrected 的 HEAD 被移动，都被 `SystemExit` 拒绝而不是"照样校准" |
| `test_the_cli_calibrates_a_prepared_root_without_preparing_it_again` | `prepare.py --root <已准备> --calibrate` 走重用分支：退出 0、材料逐字节不变、`plan.json` 写回 `calibration_result` |

```
pytest tests/test_memory_recall_prepare.py -q     → 14 passed in 1.75s   （Linux 沙箱，本批）
pytest tests/test_memory.py tests/test_memory_recall_* -q  → 见下（本批目标集合）
```

同一批里手动核对过的旁证（不构成校准）：基线工作区跑两个包的公开检查分别得到
`ZeroDivisionError` 与 `AssertionError`；适配器对基线打印 `error:ZeroDivisionError`、对纠正打印 `0`；
`--calibrate` 打印 `{"outcome": "unavailable", ..., "prerequisite": "docker_missing",
"image_id": null}`；把 `corrected` 工作区的 HEAD 前移一个提交后，重跑被拒绝
（`corrected workspace no longer sits on the recorded commit`）；删掉材料文件后，重跑被拒绝
（`material is missing from the prepared root`）。

> `tests/test_memory_recall_rehearsal.py` 由原窗口恢复（Codex 已按 MD5 核对）；本批仍只改
> `prepare.py`、`tests/test_memory_recall_prepare.py`、新增的
> `tests/test_memory_recall_formal_chain.py` 与本 README。

### 7.2 构造端到端：正式验收 → 记忆 → 另一 Task 召回（`tests/test_memory_recall_formal_chain.py`）

这是一个**只在 Windows + 本地评测镜像**上运行的用例（`os.name == "nt"` 且
`DEVFLOW_TEST_EVALUATION_CONTAINER=1`），否则整体 `skip` 并给出原因。它和 §7.1 的"构造来源"不同：
来源 Run 不是写进去的 PASSED 行，而是走 `Worker.finalize_with_independent_evaluation` ——
先跑校准过的适配器镜像（同一套 `<root>/adapter` 与材料），拿到可回读的独立回执，再由
`finalize_passing_verification` 在锁下重验草稿、diff、公开检查与基线证据，才写出 `trusted_v1`。

它按顺序做：准备材料 → 克隆仓库到 `C` → 公开检查基线失败（`failed`）→ 模型桩给出 `src/pricing.py`
的补丁 → 公开检查通过（`succeeded`）→ 语义评审（桩）→ 独立评测 + 正式收尾 → `ingest`（
`source_state = trusted_passed`、`content_status = unverified_candidate`）→ 人工 `accepted` 审核 →
第二个 Task（同仓库字符串、同基线）领取 Run 并由 Explorer 带记忆执行 → 断言
`agent_executions.input.memory_recall_v1` 与 `memory_uses` 行 → 目标 Run 的
`run_independent_evaluation` / `finalize_passing_verification` 都以 `Memory-assisted` 拒绝 →
`check_diff_scope` v2 因准入里没有 `scope-sha256-v1:` 而 `SkillNotApplicable`，v1 的
`run_registered_check` 仍记录一条 succeeded 的 ToolCall，且该 Run 没有任何 Verification。

**它不能证明**：真实模型解了真实任务、召回改善了效果、或这套材料可以用于正式样本。所有模型调用都是桩，
仓库是构造的演练仓库。**本环境（Linux、无 Docker）无法运行它**：正式尝试在创建 Run 之前就会因
`trusted_baseline_platform_supported()` 为假被拒（见 `tests/test_worker_run.py` 的同名负向用例），
所以本批只做到"用例已写好并在本机被 skip"，没有把它当成通过。

---

## 8. 交 Codex 审核的真实运行方案（本窗口不执行）

### 8.1 身份与隔离

| 项 | 值（建议，需确认） |
| --- | --- |
| 数据库 | 专用开发库（如 `devflow_mem_rehearsal`），**不是** Click 案例库；`prepare.py --confirm --database <同名>` 会先核对库名，跑前再用 `preflight` 只读探测记录 Alembic revision 与 head 是否一致 |
| 仓库 | `prepare.py` 生成的**一个**仓库（`<root>/repo/dev-rehearsal`），两个 Task 共用它的路径字符串与同一个基线 commit |
| workspace / artifacts | `D:\devflow-rehearsal\{workspaces,artifacts}`，每次 Run 一个 `run_id` 子目录（必须在仓库之外） |
| 材料 | `prepare.py` 写在 `<root>/material/` 下的构造材料；它与仓库、pack、workspace、artifacts 都不重叠，因此通过 `require_material_placement`。若要改用 `D:\devflow-control\` 保管，复制后必须同步更新来源包里的 `materials_sha256` 并重新准入 Task |
| 记录 | `D:\devflow-rehearsal\records\<阶段>-attempt-1.json`，排他创建，绝不覆盖 |

### 8.2 每次调用的模型

五角色（supervisor / explorer / planner / coder / verifier）共用同一个适配器与同一个模型标识
（`DEEPSEEK_API_KEY` + `DEVFLOW_DEEPSEEK_MODEL`，`model_gateways.py:1-10`），单请求
`max_output_tokens = 1024`、超时 60s（`model_gateways.py:24-25`）。真实模型标识必须写进运行记录
（`compose_run_record` 的 model 字段），不要用会话里的别名当证据。

### 8.3 预算、次数与停止条件

| | 来源 Task（formal） | 目标 Task（rehearsal） |
| --- | --- | --- |
| 尝试次数 | 1（`BenchmarkLimits.attempts=1`，无重试标志） | 1 |
| token 上限 | Policy `max_llm_tokens_per_run ≤ 80000`；入口再比一次，超过拒启动 | 建议在**该 Task 的** Policy 快照里写 `max_llm_tokens_per_run=40000`（只能往下压，不能超过 80000） |
| 时长上限 | `min(Policy.max_run_duration_seconds, 1200)`；`1200` 是入口内部计时器，CLI 没有调它的参数 | 建议 `max_run_duration_seconds=600`；到期由 Run 硬截止与内部计时器双重覆盖 |
| 阶段上限 | `max_stages=32` | 同 |
| 停止条件 | Run 离开 RUNNING；deadline 触发；token/阶段预算耗尽；Verifier 阻塞；正式通过已记录 | 阶段驱动给出停止原因即结束（无需等 deadline） |

预算可给建议、但**必须由 Codex 明确**：一次来源正式尝试按既有真实数据（`docs/EVALUATION_REPORT.md`）
落在 5–8 万 token 区间、耗时数分钟到 20 分钟，且**可能不通过**；目标 rehearsal 通常在前几个阶段就停，
主要成本是 Explorer + Supervisor 的若干次请求。

### 8.4 证据位置

`memory_items` / `memory_reviews` / `memory_uses` / `agent_executions.input` / `verifications`（应为空）/
`runs.status`，加上 `artifacts/<run_id>/` 与（若给 `--record`）运行记录文件。全部由
`checks/evidence.sql` 一次取出。谁在什么时候核对了哪一行，写进交付报告的"证据"一节。

### 8.5 失败处理

- **领取前被拒**（exit 2）：什么都没发生，修正参数后换**新 Task** 重来；不要复用旧 Task。
- **领取后失败/中途停止**（exit 1 或正常返回但停在半路）：Run 保留它实际到达的状态，**不关闭、不接管、
  不重试**；记录 `run_id` 与停止原因，作为这次尝试的结果汇报。
- **来源正式尝试没到 PASSED**：这次就没有可摄入的来源。**不得**用构造的 Verification 顶替，
  也不得把 rehearsal 说成来源。需要新 Task 再试（一次新尝试）。
- **rehearsal 成功后遗留的 RUNNING Run**：一 Task 只能有一个 Run（`worker_run.py:316-321`），
  想再演练必须新建 Task；旧 Run 交给既有的过期/恢复流程，不要手工关。

### 8.6 明确不做

不提交独立评测结果；不把这两个 Task 计入任何样本或成功率；不改历史 Run；不迁移业务库；
不把材料、密钥或隐藏用例写进任何文档、日志或对话。

---

## 9. 阻塞点与待定项

**B1（硬阻塞，来源侧）**：现有真实成功样本是 pack Task，摄入被 `pack:` 标记拒绝（§2）。
绕开它就是取消这条防线，不建议。

**B2（成本阻塞，来源侧）**：合法来源必须是一次 **formal** 尝试，需要 Windows + Docker + 可信独立材料 +
真实模型，并且可能一次不通过。也就是说，这个演练的"真实来源"不是零成本的一步，而是一次真正花钱的评测尝试。
在 Codex 明确预算之前不应启动。

**B3（已缓解，仍需你确认）**：`prepare.py` 用**一次性脚本**调现有 `TaskService` 完成"创建 + 准入"，
证据写 `dev-task:<operator>:<reason>`、不含 `pack:`、不碰 `scope-sha256-v1:`；它不新增 CLI、不改准入检查。
是否需要将来把它提升为通用命令行入口，仍由你决定；本批不新增框架。

**B4（能力缺口，需知情）**：`worker_run` CLI 没有时长/token/阶段参数，只能靠 Policy 快照收紧；
内部计时器固定 1200s。想更短只能改代码或在操作台上发 SIGINT（协作式停止，不保证切断在途调用）。

**B5（边界，必须写进报告）**：记忆正文无内容检查、无隐藏材料扫描；开发 Task 无范围绑定；
标记只覆盖这条接线；同一 Task 一旦被标记，后续所有 Run 都不能独立评测。

**B6（操作后果）**：rehearsal 会留下一个 RUNNING Run 和一个已标记的 Task，二者都不能"清理"。
演练前先确认这个库不是案例库、这些 Task 不需要再用于正式评测。

**B7（已由 Codex 在 Windows 上校准；脚本侧仍无本机证明，来源侧）**：Codex 在独立临时目录、独立镜像
标签上实际调用 `calibrate_stdio_suite` 得到 `verified`，并记录了镜像 `sha256:1117bebd…6817c`。这说明
**这套构造材料与适配器可以被真实校准**；但它不自动覆盖别的目录/镜像标签，也没有把结果写回
`<root>/plan.json`。要让脚本自己的记录也成为证据，在该机器上按 §3.1 的三步执行一次
（准备 → `docker build` → 同一目录 `--calibrate`），确认 `calibration_result.outcome == "verified"`。
本环境（Linux、无 Docker）只会打印 `unavailable` 与 `prerequisite`，不得当成校准失败以外的任何结论。

**B8（写库待审）**：`--confirm` 已禁用。先按 §9.1 确定"如何证明连到隔离库"和准入方案，再决定是否重新启用；
本批不擅自改这条协议。

**B9（正式链端到端未在本机跑过）**：`tests/test_memory_recall_formal_chain.py` 已写好但在 Linux 上
`skip`——正式尝试在这一步之前就被拒（`trusted_baseline_platform_supported()` 为假，先于创建 Run），
且独立评测需要 Docker。因此"正式验收 → 记忆 → 另一 Task 召回"这条路径**只有设计、代码与用例**，
没有本机的运行证据；需要在 Windows（`.env.test`、隔离 schema）上、并本地构建好适配器镜像后运行它，
再按运行结果决定是否进入真实运行评审。

### 9.1 隔离数据库身份与准入方案（待审，未执行）

只比对数据库名不足以证明连到的是演练库（名字可以撞、DSN 也可能指错）。写库前建议的最小核对（全部只读，
是否采纳由 Codex 决定）：

1. **连接事实**：`SELECT current_database(), current_user, inet_server_addr(), inet_server_port();`
   打印并写入操作记录（不含密码），与 `DEVFLOW_DB_*` 的期望值逐项比对。
2. **演练标记表**：演练库初始化时建一张单行表（如 `rehearsal_marker(created_by, created_at, purpose)`），
   准入脚本要求它存在且 `purpose` 与本次演练一致；没有标记的库一律拒写。这比"库名前缀"更难被误触。
3. **迁移与 schema**：`alembic current` 必须等于项目 head；`current_schema()` 必须是演练专用 schema
   （或 public 但已由第 2 步确认）。
4. **不是案例库**：用中性查询确认 `tasks`/`runs` 等表为空或只有演练数据。
5. **二次确认**：`--confirm --database <name>` 中的名字必须等于第 1 步读到的 `current_database()`。

准入方案（仍走现有 `TaskService`，不新增入口）：两个 Task 的证据都是 `dev-task:<operator>:<reason>`
（不含 `pack:`、不含 `scope-sha256-v1:`），`repo` 与 `base_commit` 取自 `plan.json`，幂等键
`uuid5(URL, "devflow-rehearsal:<role>:v1")`；准入后跑 `checks/evidence.sql`，两个 Task 都应
`has_pack_marker = f`。

### 仍需人工确定的事（本批不代答）

| 项 | 现在的状态 | 谁/怎么定 |
| --- | --- | --- |
| 两个真实任务 | 只有构造的 A/B（pricing / slug），不是真人提出的 Issue；若要用真实开发任务，须在**同一个**真实仓库同一个 baseline 上挑两个**不同**缺陷，并保证第二个问题的经验能复用 | Codex/操作者选定，并说明为什么不是 pack Task（§2） |
| 校准环境 | Codex 已在 Windows 临时目录得到 `verified`；脚本自己的 `plan.json` 还没有 verified 记录 | 操作者在演练机按 §3.1 三步在**同一**目录重跑，回填 `image_id` |
| 正式链端到端 | 用例已写好，Linux 上 skip（见 B9） | 在 Windows + 已构建镜像下运行 `tests/test_memory_recall_formal_chain.py`，把结果贴进评审 |
| 来源正式尝试的预算与允许范围 | 一次 formal 可能 5–8 万 token、数分钟到 20 分钟且可能不通过；本批不启动 | Codex 明确预算、次数与停止条件后才可跑 |
| 目标 rehearsal 预算 | 建议 token 4 万 / 时长 600s，仅建议 | Codex 确认后写进该 Task 的 Policy 快照 |
| 记忆正文与审核人 | 无内容检查、无隐藏材料扫描；正文必须能公开给模型 | 指定审核人并说明审核了什么（§4.3） |
| 隔离数据库与工作区路径 | §9.1 方案未审；`--confirm` 仍退出 2 | 审阅 §9.1 后再决定是否重新启用写库 |
| 证据与结论口径 | 目标 Run 不产出 Verification，也不进入任何样本或成功率 | 交付报告里逐条引用 `checks/evidence.sql` 的查询结果 |

`prepare.py` 这种"一次性脚本 + 现有 `TaskService`"的方式是否长期沿用（而不是新增通用入口），仍未定。
