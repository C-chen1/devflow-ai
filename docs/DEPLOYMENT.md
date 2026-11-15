# 部署：Compose 中的 API、显式迁移入口与一次性 Worker

本文件说明 `compose.yaml` 现在能启动什么、迁移为什么必须显式执行、Worker 入口能做什么与不能做什么，
以及容器里做不了哪些事。所有命令都以项目根目录为工作目录。

## 1. 当前提供的服务

| 服务 | 来源 | 默认 `up` 是否启动 | 职责 |
| --- | --- | --- | --- |
| `db` | `postgres:17.11` | 是 | 数据库，数据卷 `devflow_pgdata`，宿主机端口 5434 |
| `api` | 本仓库 `Dockerfile` | 是 | 只读数据库配置并服务 HTTP；**启动时不建表、不升级** |
| `migrate` | 同一镜像 | 否（`profiles: ["tools"]`） | Alembic 入口；默认命令是只读的 `alembic current` |
| `worker` | 同一镜像 | 否（`profiles: ["worker"]`） | 一次性执行**调用方指定**的 Task 的 Run；默认命令只打印帮助 |
| `scheduler` | 同一镜像 | 否（`profiles: ["worker"]`） | 常驻调度：只执行可信清单登记的 Task；默认命令只打印帮助 |

四个应用服务用同一个镜像 `devflow-api:local`，构建上下文是仓库根目录，构建输入只有
`pyproject.toml`、`uv.lock`、`alembic.ini`、`migrations/` 与 `src/`（见 `.dockerignore`）。
镜像里没有 `.env`、模型密钥、测试、文档、任务包或独立评测材料。镜像装了 `git`，因为 Worker
初始化工作区要克隆 Task 的仓库。

数据库连接沿用与宿主机相同的五项 `DEVFLOW_DB_*` 配置，只有容器内取值不同：
`DEVFLOW_DB_HOST` 固定为 `db`、`DEVFLOW_DB_PORT` 固定为 `5432`，因为 5434 只是宿主机映射端口。
`api` 只把 8000 发布到宿主机回环地址（`127.0.0.1:8000`），不对外暴露。

```powershell
# 首次或改动依赖后构建；之后启动数据库与 API
docker compose --env-file .env build
docker compose --env-file .env up -d
docker compose --env-file .env ps
```

检查 API 与数据库连接（与 README 中的本机启动方式结果相同）：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/live
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

`ready` 只证明数据库连接可用，不证明迁移已到 head，也不证明任何任务通过。

## 2. 迁移必须显式执行

`api` 的启动路径里没有任何 `create_all`、`upgrade` 或隐式 DDL：`devflow_ai.api` 的 lifespan 只建连接池。
`migrate` 位于 `tools` profile，所以 `docker compose up -d` 不会启动它；即便启动，默认命令也只是
`alembic current`。要升级必须自己把 revision 写出来：

```powershell
# 只看当前库的版本与项目 head，不改数据库
docker compose --env-file .env run --rm migrate
docker compose --env-file .env run --rm migrate heads

# 确认目标库确实是可升级的库之后，才执行
docker compose --env-file .env run --rm migrate upgrade head
```

`run --rm` 会在命令结束后删除一次性容器，不留下半启动的状态。升级仍会改动 `.env` 指向的数据库：
先核对库名、当前 revision 与计划，不要对已有业务数据直接 `upgrade head`。
项目 head 是 `b7f2c31d8a90`；停留在旧 revision 的库不满足要求 `at_head` 的 preflight。
历史 Task 与历史人工审核不会被回填。

## 3. 一次性 Worker 入口：`python -m devflow_ai.worker_run`

这个入口执行**调用方明确指定的一个 Task** 的 Run。它与另外两个容易混淆的入口分工不同：

- `python -m devflow_ai.worker` 仍然只是领取一个 Run 做检查（`--once`，不执行 Agent），本批没有改它；
- `python -m devflow_ai.benchmark_run` 领取、驱动并写出**基准运行记录**；
- `worker_run` 领取并驱动一个具名 Task 的阶段，**不写基准记录、不写正式 Verification**，
  只报告停在哪一步。正式验收仍由可信入口完成。

它复用既有实现，没有新增调度框架：阶段驱动 `orchestration.drive_run_stages`（与基准入口共用同一份
接线）、`Worker.execute_run` 的心跳与租约、`RunCoordinator` 的执行权与原子领取、模型网关
`model_gateways_from_env`，以及基准入口同一份 `BenchmarkLimits`（1,200 秒 / 80,000 token / 32 阶段 /
一次尝试 / 零重试），因此两个入口的限额定义不会分叉。

**先校验、后领取**。以下条件全部通过才会创建 Run；任一项不满足即退出 2，数据库状态不变：

1. 任务包清单可解析（`load_manifest`），且包目录能提供注册命令要跑的公开测试文件；
2. Task 存在且状态是 `ELIGIBLE`（不是 `PENDING`/`RUNNING`/…)；
3. Task 的 `base_commit` 与清单的 40 位 commit 一致；
4. Task 已固定 Policy，且 `--policy` 文件与库里的快照逐节一致；
5. Task 策略的 `max_llm_tokens_per_run` 不超过本次限额；
6. `--workspaces-root`、`--artifacts-root` 已存在，`--python-executable` 是存在的绝对路径；
7. 该 Task 还没有任何 Run（第二次尝试要建新 Task，不能复用同一个，除非那是本入口自己创建的同一个 Run，见下）。

**准备与领取之间的时间差被显式处理。** `prepare_worker_run` 只是检查，不改变任何状态；真正的创建发生在
`RunCoordinator.start_initial_run` 的 Task 行锁事务里，并把准备的 `base_commit` 与 Policy 快照传进去要求
**在同一事务内复核**：Task 若在准备之后被改过（commit 或 Policy 快照），创建被拒绝、不产生 Run。
领取本身仍有 `TaskStateConflictError` 与 `TaskClaimBusyError` 兜底，并发创建不会产生两个 Run。

**重复调用只有一个答案。** 同一个 Task 的第二次调用（无论是直接再调 API 还是再跑一次 CLI）会看到
该 Task 已存在本入口用同一幂等键 `worker:<task>:1` 创建的 Run，于是返回 `existing_run`（`claimed=false`、
带上 `run_id` 与当前 `run_status`），**不再次执行、也不创建第二个 Run**。只有别的入口/别的键创建的 Run
才会被当作"该 Task 已有 Run"而拒绝。

关于平台：**正式**尝试（`--attempt-kind formal`）在不能记录可信基线报告的平台上会被直接拒绝（退出 2），
因为调用一次必然停在 `BASELINE_UNAVAILABLE`，只会消耗预算。`--attempt-kind rehearsal` 允许在 Linux
容器内执行同一套阶段，用于环境检查；它是演练，不是正式样本，也不写基准记录。

**停止与截止的可见范围。** 调用方的停止信号（CLI 里是 SIGINT/SIGTERM）与本次尝试的截止时间合并成
同一个信号，并且这个信号同时交给工作区初始化和阶段驱动：初始化期间可见，**每个阶段运行期间同样可见**
（心跳自己的失效信号也在其中）。因此中途触发时驱动会停下，而不是继续路由下一阶段。停止是协作式的：
它让可观察该事件的代码停止继续，不强制中断已在进行的模型或子进程调用。已经触发停止的调用在
**领取之前**就返回 `stopped` / `stop_requested`，不创建 Run、不改 Task。

一次调用只领一次 Run，没有重试开关。中途被自己的停止/截止打断时报告 `stopped`，`reason` 为
`stop_requested` 或 `deadline_reached`，`interrupted_mid_stage=true`、`stages=null`（阶段数无法诚实统计），
Run 保持数据库里真实的状态（通常仍是 `RUNNING`）；其他失败才走退出 1 的错误路径，只报告 Run id 与错误类型，
不把它改写成通过或失败。

```powershell
# 只打印帮助：不会连接数据库，也不会改任何状态
docker compose --env-file .env run --rm worker

# 在一个 Linux 容器里做一次演练（挂载任务包与工作目录，目录必须已存在）
docker compose --env-file .env run --rm `
  -v D:/packs/click_3084_public:/packs/click_3084_public:ro `
  -v D:/devflow-run/workspaces:/work/workspaces `
  -v D:/devflow-run/artifacts:/work/artifacts `
  worker --pack /packs/click_3084_public --task-id <task-uuid> `
    --policy /packs/click_3084_public/policy.draft.json --operator <name> `
    --python-executable /app/.venv/bin/python `
    --workspaces-root /work/workspaces --artifacts-root /work/artifacts `
    --attempt-kind rehearsal --confirm-run

# 常驻模式：加 --watch，停止信号（Ctrl-C / docker stop）之前一直重读清单
docker compose --env-file .env run --rm `
  -v D:/devflow-config:/config:ro -v D:/packs:/packs:ro `
  -v D:/devflow-run/workspaces:/work/workspaces `
  -v D:/devflow-run/artifacts:/work/artifacts `
  scheduler --manifest /config/manifest.json --operator <name> `
    --python-executable /app/.venv/bin/python `
    --workspaces-root /work/workspaces --artifacts-root /work/artifacts `
    --attempt-kind rehearsal --poll-interval-seconds 15 --watch --confirm-run
```

建议可信侧用原子替换（写临时文件再 rename）更新清单，减少读到半份文件的机会；即使读到半份，
上面的暂停机制也只会暂停领取，不会按旧清单继续。

## 3.1 常驻调度入口：`python -m devflow_ai.worker_scheduler`

这是 `docs/WORKER_SCHEDULER_PLAN.md` 的实现：**只处理可信清单里登记的 Task**，每个登记项复用上面的
`worker_run` 一次性路径（同样的行锁、状态、commit/Policy 复核与幂等键），因此这里没有第二套执行器。
清单是本地 JSON，逐项绑定 Task ID、任务包与 Policy 文件；共享路径、解释器、角色网关和限额由启动参数/环境给出：

```json
{
  "schema_version": 1,
  "tasks": [
    {"task_id": "<task-uuid>", "pack": "packs/click_3019_public", "policy": "packs/click_3019_public/policy.json"}
  ]
}
```

相对路径按清单文件所在目录解析；未知字段、重复 Task、不可读文件都会被拒绝（拒绝信息只报字段位置，
不回显文件内容）。清单本身**不授予执行权**，真正的领取仍由 Task 行锁与幂等约束完成。

两种模式共用同一套领取与驱动：

- **批次模式（默认）**：读一次清单，逐项执行，全部结束时退出（`idle_exit`）。清单里至少要有一项——
  空的批次和打错的清单没法区分。
- **常驻模式（显式 `--watch`）**：按 `--poll-interval-seconds` **重读同一份清单**（间隔必须是有限正数，
  NaN/无穷大直接拒绝），允许启动时清单为空，之后登记的 Task 会被处理。空闲时等待而不空转。

两种模式共有的语义：

- 每个登记项最多一次尝试；领取成功或发现已有 Run 后即结束该项，轮询与进程重启都不会再执行第二次。
- 配置或状态不可用的项记录 `refused` 与原因后跳过，**不阻塞**其余登记项。
  常驻模式下，该项只在**它的任务包 `task.json` 或 Policy 文件字节发生变化**时重新检查一次——
  这正是"修好配置"的信号；文件没变就不会按间隔反复重试。批次模式里拒绝是终态。
- Task 行被其他事务锁住时记为 `waiting`，按 `--poll-interval-seconds` 等待后重试**领取**（没有创建 Run，所以不是重试尝试）。
- 收到 SIGINT/SIGTERM 时停止领取并把信号传给当前任务的阶段（`stopped`），空闲等待会被立即唤醒。
- 不自动重试、不接管、不重放孤儿节点、不关闭不是自己创建的 Run；Run 的真实状态原样保留。

常驻模式额外约定（对应方案"不能把旧清单当成最新授权"）。撤销的生效边界要说清楚：清单读取与数据库领取
**不是同一个事务**——读取成功之后、真正创建 Run 之前清单又被替换，这一次领取仍会发生；进程只能在下一个
读取点看到变化。可以保证的是：没有任何一次领取是"没有紧邻其前的成功读取"的，且领取事务内还会用 Task 的
固定 commit 与 Policy 快照复核一次（准备时读到的 pack/Policy 若已变，领取会被拒绝）。

**可信独立评测材料（可选，按登记项注入）。** 只有控制端显式给出材料，调度任务才可能走到正式验收；
材料以**路径**出现在清单条目里，材料文件本身留在仓库之外（不能放进仓库、镜像、任务包或普通日志）：

```json
{"task_id": "<task-uuid>", "pack": "packs/click_3019_public",
 "policy": "packs/click_3019_public/policy.json",
 "material": "D:/devflow-control/click3019-material-v1.json",
 "record": "records/click3019-attempt-1.json"}
```

`record` 是可选的运行记录位置（相对清单目录解析）。写记录复用基准入口同一套 `BenchmarkRunRecord`、同一条分类逻辑
（`classify_outcome`）与同一个排他创建；`formal_pass` 只有在正式 Verification 与证据都能从磁盘读回时才会出现，
绝不从 Agent 自述或"测试通过"推断。记录含 Task、Run、`attempt_kind`、模型、Policy 版本、证据引用与产物目录，
人工审核结论**留空**（`pending`）——之后由真人用 `benchmark_record review` 生成审核副本、再 `task_review` 入库；
调度器不会自动填结论，也不回填历史 Task。记录路径已存在时在**领取之前**拒绝该项；重放（同一 Task 已有 Run）
不写记录也不重新运行；两个登记项不能共用同一个记录路径（清单加载时拒绝）。

**崩溃后的补写（显式入口）**：如果进程在正式事务提交之后、记录写入之前退出，重放只会返回 `existing_run`，
不会自己补写。这时由可信操作者显式执行

```powershell
uv run --env-file .env --locked python -m devflow_ai.record_backfill `
  --task-id <task-uuid> --run-id <run-uuid> --pack D:/packs/<pack> `
  --artifacts-root D:/devflow-run/artifacts --record D:/records/<attempt>.json `
  --operator <name>
```

它只读数据库与产物目录、**不建 Run、不重跑任务、不调用模型、不改任何行**，用同一套 `classify_outcome`
与排他创建写出**一份新的**待审核记录。能重建的：Task/Run、任务包与 base commit、Policy 版本、Run 的开始与
结束时间、公开检查与独立评测证据、以及可核验时的 `formal_pass`（必须能读回正式 Verification、草稿与回执）。
**不能重建、因此如实标注而不推测的**：截止时间（不写 `timeout`）、`attempt_kind`（默认 `unknown`，即使
Run 通过也不进正式样本；只有操作者显式声明才写入并注明是声明）、模型标识（写 `unrecorded`）、人工审核结论
（保持 `pending`），以及"独立评测是否尝试过"（记录为未记录，结果通常是 `incomplete`）。仍在 `RUNNING` 的
Run、Task/Run 不匹配、缺少结束时间、目标文件已存在都会被明确拒绝。

- 绑定方式是既有的：加载时先对文件原始字节算 sha256 并与该条目任务包 `independent_suite.materials_sha256`
  比对，套件版本也要与包一致；不一致在**领取之前**拒绝该项，不会创建 Run。因此材料不会跨任务串用——
  两个登记项各自指向自己的材料，指向别人的材料就通不过摘要校验。
- 满足条件时，调度任务复用 `orchestration.finalize_benchmark_run`：它在**同一次** `Worker.execute_run` 的
  心跳、租约、执行权与停止事件内被调用（不是退出后再验收），沿用既有门禁（草稿存在、公开检查已通过、
  套件版本已固定、证据上限已给），再由协调器在事务内决定是否写入正式 Verification。
- 没有材料、草稿公开检查未通过、评测返回不可用、停止信号已触发、执行权/租约失效，全部保留真实结果：
  停在草稿并在条目 `finalization` 里写明原因，`formal_acceptance` 只有真正提交了正式 Verification
  才会是 `committed`。
- **只有 formal 能进正式事务**：`rehearsal` 与 `unknown` 可以照常跑阶段，但即使收到材料也不会调用可信入口，
  结果记 `finalization = skipped / attempt_kind_not_formal`，Run 不会被标成正式 `PASSED`、也不写 Verification。
  基准入口（`benchmark_run --attempt-kind rehearsal`）遵守同一条规则。
- **材料放在哪里**：材料路径不能落在四处之内——任务包目录（公开内容）、`--workspaces-root`（Agent 工作区）、
  `--artifacts-root`（产出物），以及**本地 `Task.repo` 指向的源码 checkout**（被测代码）。判断用真实路径
  （`realpath` 解析软链接/junction）加平台大小写规则（Windows 上 `normcase`），所以指进这些目录的符号链接
  也会被拒。两个入口共用 `independent_evaluation.require_material_placement` 这一份实现，语义一致。
- **`Task.repo` 的解析**：`file://` URL 按以下形式还原成本地路径——`file:///C:/repos/x` 与
  `file://localhost/C:/repos/x` → `C:/repos/x`；`file:///srv/repos/x` → POSIX 路径；**网络共享
  `file://server/share/repo` → `\\server\share\repo`（保留 server，不会被压成盘符相对的 `\share\repo`）**；
  百分号编码（含 `%20`）与字面空格都会解码。普通路径（Windows 盘符、UNC `\\server\share\repo`、相对路径）
  直接按本地路径处理。
- **适用边界（不虚构）**：`Task.repo` 是 `https://…`、`ssh://…`、`git://…` 或 `git@host:org/repo` 这类远程地址时，
  本地文件系统无法据此判断材料相对 checkout 的位置，因此只做前三项检查——这一点在文档里明说，不假装已覆盖。
  Run 自己的 checkout 建在 `--workspaces-root` 之下，已被第一项覆盖。UNC 路径的比较依赖 Windows 自身的路径
  规则，Linux 上只能验证解析结果（`\\server\share\repo` 字符串），实际包含判断需在 Windows 复验。
- **检查时机**：worker 入口在读到 Task 之后、读取材料之前检查四项；benchmark 入口先在启动参数阶段检查
  pack/工作区/产物三项（不需要数据库），再在领取之前的 Task 事务里补上本地仓库这一项。两者的拒绝理由与
  措辞相同，都不会在检查通过前创建 Run。
- **清单本身是可信控制端文件**：它含材料路径，因此不能放进公开任务包、镜像仓库或 Agent 上下文，也不随任务分发。
  公开任务包清单（`task.json`）没有材料字段，写进去会被 `extra="forbid"` 拒绝。
- 重放（同一 Task 再次被调度、进程重启后再跑）只会返回 `existing_run`，不会重复评测或写入第二份正式结果。

常驻模式额外约定（对应方案"不能把旧清单当成最新授权"）：

- 启动时清单不可用 → 退出 2，一行都不领。
- 运行中清单缺失或无效 → **暂停领取**并报告一次 `worker_scheduler_paused`（摘要里给出
  `manifest_paused_rounds`/`manifest_errors`），修复后报告 `worker_scheduler_resumed` 并继续。
- 暂停期间不会复用上一次成功读到的清单。
- 从清单里删掉的项不再领取；若它还没被领取过，之后重新登记会重新变成待处理；已被领取的项删了再加也不会再跑。
- 撤销在"本轮第一个任务执行期间"也要生效：常驻模式在**每一项领取之前**重新读取并核对清单——条目已删除、
  或它的任务包/Policy 路径已变化时，不会拿本轮开头读到的旧条目去领取；清单在那一刻不可用则本轮暂停领取。
  已经领取的 Run 不受撤销影响，不会被取消、关闭或重试。

```powershell
# 只打印帮助
docker compose --env-file .env run --rm scheduler

# 用挂载的清单做一次演练（Linux 容器里只能演练，不能完成正式验收）
docker compose --env-file .env run --rm `
  -v D:/devflow-config/manifest.json:/config/manifest.json:ro `
  -v D:/packs:/packs:ro `
  -v D:/devflow-run/workspaces:/work/workspaces `
  -v D:/devflow-run/artifacts:/work/artifacts `
  scheduler --manifest /config/manifest.json --operator <name> `
    --python-executable /app/.venv/bin/python `
    --workspaces-root /work/workspaces --artifacts-root /work/artifacts `
    --attempt-kind rehearsal --confirm-run
```

**仍然缺的部分**：调度范围是"可信侧登记的任务"，没有自动发现任意 Task 的能力；进程崩溃后由重启时读数据库状态
与幂等键识别已领取任务，没有独立的调度状态表；多 Worker 之间的分工完全依靠 Task 行锁与执行权，
没有队列或分区；常驻模式的"修复后重新检查"只认清单条目与任务包/Policy 文件的变化，不轮询数据库状态。
若将来需要自动发现、优先级或公平调度，那需要新增持久契约，本批没有做也不会假装已有。

## 4. 容器内不能做的事：平台能力边界

Linux 容器与本机 Windows 运行的差别不止是路径，以下能力**只在 Windows 上可用**：

- 受控 pytest 命令的 JUnit 报告采集依赖 Windows Job 等待整棵进程树退出。非 Windows 上
  可信基线（`execute_baseline_verifier`）得不到可用的 `pytest_junit` Artifact，
  会得到 `BASELINE_UNAVAILABLE`，因此**不会**产生正式 Verification。
- 由此，容器里的 API 可以创建、查询 Task 与准入状态，但**不能**作为可信基线或正式验收环境。
  需要该报告的正式验收和 preflight 基线检查仍需具备 Windows Job 能力的环境；人工审核入库不依赖该报告采集机制。
- 独立评测用的容器（`benchmarks/click_3084_public/Dockerfile`、`--network none`）是另一套镜像，
  与本文件的应用镜像无关，不要混用。

本批之前，这个平台差异表现为 Linux 上 17 项测试失败（都把可信基线当成本可用）。现在这些用例
显式标注为 Windows-only 跳过，Linux 上的失败不再被误读为回归；跳过不等于通过，报告里单独计数。

## 5. 实际执行与未执行的验证

已在 Linux（无 Docker 守护进程）执行：

- `python -c "yaml.safe_load(open('compose.yaml'))"` —— YAML 可解析，`db`/`api`/`migrate`/`worker`
  四个服务、profile 与占位符展开均符合预期。
- `tests/test_worker_run.py` 24 项在隔离 schema + 模型桩下通过：领取、阶段驱动到第一个 Supervisor 阶段、
  配置不匹配、未准入/已开始的 Task、重复调用（API 与 CLI 两条路径都返回 `existing_run`）、
  准备后 Task 被改（commit / Policy）在行锁事务内被拒、两个并发调用只产生一个 Run、执行权、
  调用前已停止不领取、阶段内触发停止与截止、失败路径与 CLI 退出码。
- `tests/test_record_backfill.py` 9 项在隔离 schema 与构造证据下通过：可核验的正式 Run 补写为
  `formal_pass`（引用同一条 Verification、回执哈希一致、审核结论 `pending`、`attempt_kind` 仍为
  `unknown` 因而不进正式样本）、证据被篡改或产物缺失时不得记 `formal_pass`、Task/Run 不匹配、
  仍在 `RUNNING`、目标文件已存在、重复调用不覆盖，以及补写前后 Run/执行/ToolCall/Verification
  行数与 Run 状态完全不变（不建 Run、不改库）。
- `tests/test_worker_scheduler_record.py` 9 项在隔离 schema、受控 git 夹具与模型桩下通过：完成的尝试写出
  可读记录（Task/Run/attempt_kind/模型/Policy/产物齐备、审核结论 pending）、演练记为演练、
  失败先写记录再抛错、超时记为 `timeout`、记录路径已存在则在领取前被拒、重放不写第二份、
  两个登记项各写一份互不覆盖、清单拒绝共用记录路径，以及真人 `benchmark_record review` 在原记录之外
  生成审核副本（原文件字节不变）。
- `tests/test_worker_scheduler_evaluation_chain.py` 5 项：其中 4 项默认跳过，是构造场景下的贯通测试
  （调度入口 → 真实 Verifier 草稿 → 真实 Docker 独立评测 → 正式事务），同时要求 Windows Job（可信基线
  与公开检查）和显式 Docker 开关，缺任一项就跳过——**跳过不是通过**；另有本地可跑的桩分发表守卫
  （用真实提示词构造函数核对每个角色桩的返回）。运行方式（Windows）：
  `$env:DEVFLOW_TEST_EVALUATION_CONTAINER=1; uv run --env-file .env.test --locked pytest tests/test_worker_scheduler_evaluation_chain.py`
  需要本机已有 `python:3.11-slim` 镜像。成功用例核对 Verification（`trusted_v1/passed`、套件版本）、
  Run `PASSED`、Task `AWAITING_REVIEW`，以及第二次调度调用只返回 `existing_run`、不产生第二份正式结果；
  失败用例让补丁只满足公开检查而行为不变，独立评测于是返回失败，断言不写正式 Verification、Run 保持
  `RUNNING`、Task 保持 `RUNNING`。Codex 已在 Windows + Docker 隔离环境实跑本文件 5 passed；
  默认阶段驱动覆盖独立评测成功、失败与重复调用，另两条用例由测试手动编排阶段。模型和材料是构造的，不能证明真实任务成功。
- `tests/test_worker_finalize_chain.py` 31 项（Linux 上 1 项 Windows-only UNC 用例跳过）在同一套隔离 schema、真实受控 git 夹具与模型桩下通过：
  调度任务到达草稿后把材料交给可信入口（断言调用发生在执行权有效、停止事件未触发时，且 Task/套件/材料
  绑定正确）、无材料停在草稿、材料属于别的包时领取前被拒、两个登记项不会串用材料、公开检查未通过不评测、
  停止后不评测、执行权失效记为失败且不写正式结果、评测不可用如实报告、重放不重复评测；
  以及演练边界与存放边界——`rehearsal`/`unknown` 收到材料也不提交正式结果（调度与基准入口各一条）、
  材料放在任务包/工作区/产物根/本地源码仓库内被两种入口以同一措辞拒绝（参数化一组）、
  独立可信目录可用、符号链接指进任务包仍被拒、远程仓库 URL 的适用边界、公开任务包不能携带材料字段，
  和一次扫描确认材料内容没有出现在 Run 工作区、产物目录或调度摘要里。
  **注意**：这一组把容器评测与正式提交事务整体替换为桩，只证明调度侧接线与门禁，不代表 Docker 独立评测
  或真实任务通过。
- `tests/test_worker_scheduler.py` 32 项在同一套隔离 schema、真实受控 git 夹具与模型桩下通过：
  批次模式下只领取登记项、未登记项不被领取、无效配置不阻塞其他项、行锁竞争时等待而不是空转、
  调用前已停止不领取、停止后不领下一项、重启只报 `existing_run`、两个调度者竞争只产生一个 Run、
  租约/执行权丢失记为失败且不提交结果、清单校验（未知字段/重复/空/不可读/相对路径）、
  非有限轮询间隔被拒；常驻模式下空清单启动后加入任务、清单无效时暂停且不复用旧清单、
  修复后恢复、删除未领取项不再领取、被拒项只在任务包/Policy 字节变化后重新检查一次、
  空闲等待中被停止、两个常驻调度者竞争只产生一个 Run、第一项执行期间撤销第二项/写坏清单/改写第二项路径
  都不使用旧条目领取（写坏清单后修复再领取）、撤销加停止后已领取的 Run 保持原状、批次模式仍是单次快照，
  以及 CLI 的批次/常驻/NaN 三条。
- `tests/test_benchmark_run.py` 22 项、`tests/test_worker_integration.py` 143 项在接线重构后仍通过。
- CI 定向集合与全量测试的 Linux 结果见 docs/PROGRESS.md 最新条目。

**本批没有执行**：`docker compose build`、`docker compose run worker` 与镜像构建本身。本沙箱没有
Docker 守护进程，所以「worker 容器能启动」「挂载的任务包能被读到」「容器内的 git 克隆能成功」都只是
配置层推断，需要 Codex 在 Windows + Docker 上验收。

Codex 已在此前的 Windows + Docker 上使用单独 Compose 项目和全新数据卷验证：应用镜像构建成功；
启动 API 与数据库、默认 `migrate` 查看版本均未创建迁移表；显式 `migrate upgrade head` 升至
`b7f2c31d8a90`；API 连接数据库，创建的任务在数据库及 API 重启后仍可查询。测试使用独立端口
和数据卷，不代表已有数据库已升级。那次验证不含 worker 服务。

## 6. 停止与清理

```powershell
# 停止容器，保留数据卷（正式记录重启后仍可查）
docker compose --env-file .env down

# 连同数据卷一起删除会丢失数据库内容，只在确认可以丢弃时执行
# docker compose --env-file .env down -v
```
