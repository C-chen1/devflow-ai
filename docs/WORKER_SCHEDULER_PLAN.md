# Worker 调度：V1 交付范围

当前 `worker_run` 由可信调用方指定一个 Task 和任务包，执行一次有界流程；它不是自动挑选任务的常驻 Worker，且不会独立提交正式 Verification。该入口已在隔离容器以模型桩走到 Supervisor，但没有证明真实模型任务或 Linux 正式验收。

## 第一版目标

提供一个可停止的常驻调度入口，从可信调用方显式提供的任务清单中选择可运行任务，复用现有 `worker_run` 和阶段驱动。先覆盖单机或受控环境下少量已登记的 Task，不做自动发现任意任务、自动重试或崩溃节点重放。

- 任务清单是可信侧显式指定的本地文件，逐项绑定 Task ID、任务包和 Policy 文件；共享路径、解释器及角色网关配置由启动参数或环境指定。清单不包含模型密钥、隐藏用例或参考答案。
- 每次只考虑清单中已登记的 Task。使用现有的 Task 行锁、状态、固定 commit 和 Policy 复核以及 Run 幂等约束完成真正的领取；清单和预检查本身不授予执行权。
- 缺失或无效配置应给出可诊断结果，不阻塞其他已登记任务；任务完成一次领取后不能被循环反复执行。进程重启后通过数据库状态和现有幂等键识别已领取任务。
- 多 Worker 对同一 Task 竞争时，必须验证只创建一次 Run、只有获执行权者驱动阶段。遇到租约失效、Run 状态不明或孤儿节点时不擅自接管、重放或清理，沿用现有恢复/人工处理边界。
- 停止信号与心跳失效向当前任务传播；停止后不再领取新任务。模型调用与子进程仍为协作式停止，不声称强制中断。
- Linux 的可信 pytest 基线受现有平台限制；不能把演练或成功启动容器当成正式通过。Worker 与可信正式评测的最终接线仍需单独验证。

## 验收

使用 PostgreSQL 隔离 schema、真实受控 Git 夹具和模型桩验证：登记任务可被执行；未登记任务不被领取；无效配置不阻塞其他任务；空闲不空转；停止不再领取；重启不重复执行；两个 Worker 竞争同一 Task 只产生一次 Run；租约/执行权丢失不提交迟到结果。验证不调用真实模型、不迁移现有数据库、不改历史 Run。

这个范围不需要先引入数据库调度表；若实现证明现有 Run/Task 约束不足，再具体说明缺口，不凭推测扩大架构。真实模型、Windows 正式基线、独立评测和端到端人工审核仍各自需要可信侧验收。

## 常驻模式：下一批

保留当前默认的有限批次模式，另提供显式启用的常驻模式。常驻模式按有界间隔重读同一份可信清单，允许其中的任务列表为空；启动时清单无效应拒绝启动，运行中清单暂时缺失或无效时暂停新领取并等待修复，不能将旧清单误当成最新授权。建议可信调用方原子替换文件，以减少读取到半份文件的机会。配置恢复后继续轮询。

每次新一轮以最近成功读取的清单为授权范围：新增项可被处理，已移除项不再新领取；进行中的任务仍沿用既有停止、租约和执行权规则。重启后使用数据库与现有幂等机制识别已创建的 Run；不重复驱动或自动重试。需要定义已拒绝项在清单或相关配置修正后的重新检查策略，但不能把已经创建的 Run 当作新尝试。

保留现有批次模式的兼容行为；常驻模式空闲时等待而非退出或空转，退出信号及时唤醒等待并阻止下一次领取。限制轮询间隔必须为有限正数，避免 NaN 或无穷大导致异常行为。容器只在显式传参时运行该模式；继续明确 Linux 无可信 pytest 报告捕获、调度器不自动完成独立正式验收。

验收至少覆盖：启动空清单后加入任务、已完成项在多轮和重启后不重复、删除尚未领取项、无效清单修复、配置修复、等待期间停止、两调度器竞争同一新任务，以及批次模式原有行为。使用隔离 PostgreSQL 与模型桩，不运行真实模型或修改历史 Run。

---

## 实现记录（V1，2026-11-14）

已实现 `src/devflow_ai/worker_scheduler.py`（`python -m devflow_ai.worker_scheduler`）与
`tests/test_worker_scheduler.py`（15 项，隔离 schema + 受控 git 夹具 + 模型桩）。与方案对照：

- 清单是本地 JSON：`schema_version` 与 `tasks[]`，每项只含 `task_id`/`pack`/`policy`。operator、共享路径、
  解释器、`attempt-kind` 与限额由启动参数给出（方案允许"启动参数或环境"），因此清单里没有密钥、隐藏材料
  或解释器路径。未知字段、重复 Task、空清单、不可读文件都会被拒绝，拒绝信息只报字段位置。
- 每项各走一次一次性入口 `worker_run`（`prepare_worker_run` + `run_worker_once`），没有第二套执行器；
  清单与预检查不授予执行权，领取仍由 Task 行锁、状态与幂等键完成。
- 进程内状态机 `pending → handled | waiting`：领取成功或识别到已有 Run 即 `handled`，不会再执行第二次；
  配置/状态不可用记 `refused` 后 `handled`，不影响其他项；行锁竞争记 `waiting`，按间隔重试**领取**。
- 重启语义依靠数据库：同一幂等键 `worker:<task>:1` 的 Run 已存在时 `prepare` 直接给出 `existing_run`，
  重启后的进程只报告它，不会重放阶段。
- 停止：SIGINT/SIGTERM 传给当前任务的阶段（协作式），循环不再领取；调用前已停止则不领取任何任务。
- 租约/执行权丢失：该项记 `failed` 与错误类型，Run 保持数据库里的真实状态，不提交迟到结果、不清理。

偏离方案的地方与原因：方案写的是"常驻调度入口"，但登记的 Task 全部结束后循环**退出**（`idle_exit`）而不是
永远等待，因为本入口不允许自动发现任务，继续等待只会等一个它无权认领的东西。空闲等待只用于行锁竞争这类
暂时性情况。需要长期驻留时由外部调度器/服务管理重启，或等后续引入持久调度契约——两者都不在本版。

## 实现记录（常驻模式，2026-11-15）

默认仍是有限批次，`--watch` 才进入常驻模式；两种模式共用同一套领取与执行路径，只在"读几次清单"和
"何时退出"上不同。

- 读清单：常驻模式每轮重新 `load_scheduler_manifest(..., allow_empty=True)`。启动时读不到或非法 → 退出 2；
  运行中读不到或非法 → 本轮**不领取**，`manifest_paused_rounds` 加一，并在状态转变时报告一次
  `worker_scheduler_paused`。上一次成功读取的清单不会被复用，因此不会把旧清单当成最新授权。
  修复后报告 `worker_scheduler_resumed` 并继续。
- 删除：某轮成功读取后不在清单里的项，若还是 `pending`/`waiting` 就标记 `removed` 并不再领取；
  从未被领取过的项之后重新登记会重新待处理，已被领取的项（`handled`）不因删除或再次登记而重跑。
- 拒绝后的重新检查：常驻模式下被拒项记录当时输入的签名——任务包与 Policy 的路径、`task.json` 与 Policy
  文件的字节摘要。只有签名变化时（即操作者确实改了这些文件）才重置为待处理并再检查一次，所以不会按轮询间隔
  反复重试；批次模式里拒绝仍是终态。
- 行锁竞争：`waiting` 项在下一轮重新尝试**领取**。这一条在本批被顺手修正——此前 `waiting` 与 `handled`
  一起被跳过，等于永远不会重试，与文档不符。
- 间隔校验：`poll_interval_seconds` 必须是有限正数；`SchedulerRequest` 构造时与 CLI 参数两层都拒绝
  NaN/无穷大/非正数。
- 停止：SIGINT/SIGTERM 设置的事件同时是等待的唤醒条件，因此停在空闲轮次也会立刻退出；退出后不再领取。
- 未做：自动发现、优先级、公平调度、独立调度状态表；数据库状态变化不会触发被拒项重新检查（只认文件变化）。

## 修复记录：撤销时序（2026-11-15）

Codex 复现：一轮里有两个登记项，第一个执行期间清单被替换为空，第二个仍按本轮开头的快照被领取。
原因是常驻循环在一轮开始时读一次清单，然后对快照逐项领取；"等待下一轮时删除"能生效，"本轮前一项执行时删除"
不生效。现改为**每一项领取之前重新读取并核对**：条目已删除或 pack/Policy 路径变化时不使用旧条目，
按最新清单进入后续处理；那一刻清单不可用则本轮暂停领取。新增 5 项测试覆盖撤销、写坏、改写路径、撤销加停止
和批次兼容；已用去掉重读的旧实现验证过这 3 项会失败，说明测试确实指向该缺陷。

边界（不宣称消除所有竞态）：清单读取与数据库领取不是同一事务，读取成功后、创建 Run 前清单再次变化，
这次领取仍会发生，下一个读取点才能看到。可保证的是没有"缺少紧邻成功读取"的领取，且领取事务内会按 Task 的
固定 commit 与 Policy 快照复核配置内容。

## 实现记录：可信正式验收接线（2026-11-16）

- 材料入口：`worker_run` 新增 `--material`，清单条目新增可选 `material` 字段。两处都只写**路径**，
  文件由控制端保管在仓库之外；读取时沿用 `load_independent_material`（先对原始字节算 sha256 与任务包的
  `independent_suite.materials_sha256` 比对，再解析），套件版本也必须与包一致。摘要不匹配就在领取前拒该项。
- 调用位置：`run_worker_once` 的 `process` 内在 `drive_run_stages` 之后、`Worker.execute_run` 返回之前调用
  `orchestration.finalize_benchmark_run`，因此评测与正式事务都在同一心跳/租约/执行权/停止事件范围内；
  不在 Worker 退出之后验收。
- 结果如实：无材料 → `skipped/no_trusted_material`；停止信号已触发 → `not_finalized/stop_requested`；
  草稿公开检查未通过 → `not_finalized/public_checks_not_passed`；评测不可用 → `finalized` + `unavailable`；
  评测异常 → `finalization_error` 记录后原样抛出（调度项记 `failed`）。`formal_acceptance` 由数据库状态决定，
  只有 Run 真的 `PASSED` 才是 `committed`。
- 绑定与重放：材料与任务包的摘要绑定，两个登记项各指自己的材料；跨包材料在领取前被拒。同一 Task 的重放走
  `existing_run`，不重复评测、不写第二份正式结果（正式事务后 Verifier 执行不再是 `VERIFICATION_BLOCKED`，
  再调用 finalize 会得到 `no_blocked_verifier_draft`）。
- 测试与边界：`tests/test_worker_finalize_chain.py` 9 项把容器评测与正式事务整体替换为桩，只证明调度侧接线与
  门禁；**不代表 Docker 独立评测或真实任务通过**。Linux 的可信 pytest 基线限制不放宽。

## 修复记录：演练边界与材料存放（2026-11-16）

- **只有 formal 进入正式事务**：`worker_run` 的 `_finalize_stage` 与基准入口的 `run_one_task` 都在调用
  `finalize_benchmark_run` 之前检查 `attempt_kind`；`rehearsal`/`unknown` 得到
  `finalization = skipped / attempt_kind_not_formal`，不写 Verification、不改 Run 状态。此前两处都没有这道检查，
  演练只要带上材料就能提交正式结果——这是本批修掉的缺陷。
- **测试改走 formal 路径**：`tests/test_worker_finalize_chain.py` 的正式链路测试改用 `attempt_kind="formal"`，
  并通过 `prepare_worker_run(..., platform_supports_trusted_baseline=True)` 这个既有测试接缝显式注入平台能力；
  真实平台门禁没有改（Linux 上仍然拒绝 formal）。新增反例：`rehearsal` 与 `unknown` 即使有材料也不提交。
- **材料存放边界**：加载材料之前按真实路径拒绝位于任务包目录、`--workspaces-root`、`--artifacts-root` 之内的材料路径；
  公开任务包清单没有材料字段（`extra="forbid"`），由测试守护。含材料路径的调度清单是可信控制端文件，
  不得出现在公开任务包、镜像或 Agent 上下文中。
- 新增测试还扫描了 Run 的工作区与产物目录，确认材料里的期望输出没有出现在任何文件或调度摘要里。

## 补充：材料位置检查补齐与两入口统一（2026-11-16）

- 共享实现：`independent_evaluation.require_material_placement(material_path, pack_dir=…, workspaces_root=…,
  artifacts_root=…, repository=…)`，配套 `local_repository_path()` 与 `is_within()`（`realpath` 解析软链接、
  `normcase` 处理 Windows 大小写）。拒绝信息统一为 "The trusted material must be kept outside the …"。
- 新增本地源码仓库一项：`Task.repo` 指向本地目录（普通路径、相对路径、Windows 盘符路径、`file://`）时，
  材料不能在其中；`https/ssh/git/scp-like` 远程地址没有本地路径，只做前三项检查并**明确记录为边界**，
  不虚构本地 checkout 路径。
- 时机：worker 入口在读到 Task 之后、读取材料之前检查四项；benchmark 入口先在参数阶段检查三项（无需数据库），
  再在领取前的 Task 事务里补本地仓库一项。两者共用同一函数与措辞，检查通过前不会创建 Run。
- 测试：参数化覆盖四种位置 × 两种入口（同一措辞、均不建 Run）、独立可信目录可用、符号链接指进任务包被拒、
  远程 URL 的边界声明，以及 `local_repository_path` 对 URL/`file://`/盘符/普通路径/相对路径的解析。

## 修复记录：`file://` URL 的网络共享（2026-11-16）

Codex 实测 `file://server/share/repo` 被解析成 `\share\repo`（丢了 server），因此位于网络共享源码目录内的
材料可能漏检。`local_repository_path` 现在把 `file:` URL 交给 `local_path_from_file_url`：

- 有 authority 且不是 `localhost` → UNC：`\\server\share\repo`（共享名为空时返回 `None`）；
- `file://localhost/…` 与 `file:///…` 等价，盘符前的 `/` 去掉；
- `percent-encoding`（含 `%20`）与字面空格都解码；
- 用户信息（`user@server`）从 authority 中剥离。

回归：`file:///C:/…`、`file://localhost/C:/…`、`file:///srv/…`、`file://server/share/repo`、`%20` 与空格、
普通 UNC 路径、`https`/`ssh`/`git@host:` 远程（返回 `None`）。UNC 的**包含判断**依赖 Windows 路径规则，
Linux 上只验证了解析结果，该项用例在非 Windows 跳过，交由 Codex 复验。

## 贯通测试：调度 → 草稿 → 真实 Docker 评测 → 正式事务（2026-11-16）

`tests/test_worker_scheduler_evaluation_chain.py`：

- 真实部分：隔离 PostgreSQL schema、受控 Git 仓库（一个 base 提交 + 一个修正提交，另有两个校准检出）、
  Verifier 草稿由**既有 Verifier 阶段**产出（真实基线运行、真实补丁后公开检查、真实语义草稿），
  Docker 独立评测与正式事务也是既有代码（`run_independent_evaluation` / `finalize_passing_verification`）。
- 桩部分：四个角色的模型调用（supervisor 按提示词里的 schema 标题与 `baseline_available` 分流，
  planner/coder/verifier 各返回固定结构）；构造的评测材料（一个 `python -B -c` 适配器 + 一条用例 +
  两个校准检出）；走 `run_worker_once(drive=…)` 这个既有测试接缝，`drive` 内手写阶段顺序，
  收尾仍由 `run_worker_once` 调 `finalize_benchmark_run`。
- 门禁：`os.name == "nt"` 且 `DEVFLOW_TEST_EVALUATION_CONTAINER=1`，否则跳过；单独的桩守卫用例本地可跑。
- **状态：** Codex 已在 Windows + Docker、隔离 PostgreSQL 上实跑 5 passed；修正了 Explorer 桩、
  Windows 换行、公开目标命令、对照测试和差异引用。新增用例覆盖默认阶段驱动的成功、失败和重复调用，原成功/失败
  用例仍由测试手动编排阶段。模型与材料均为构造，不能证明真实任务成功。

## 实现记录：调度结果 → 待审核运行记录（2026-11-17）

- 入口：`worker_run --record` 与清单条目 `record`（相对清单目录解析）。记录由入口在**运行结束后**用
  `benchmark_run.compose_run_record` 组合：内部调用既有 `classify_outcome`，因此 `formal_pass` 只有在正式
  Verification、草稿与独立回执都能从磁盘读回时才出现；`failed` 需要可核验且已校准的失败；`timeout` 来自截止
  时间；其余记 `incomplete`。这段组合逻辑同时被 `run_one_task` 与 `run_worker_once` 使用，没有第二套统计规则。
- 内容：Task、Run、`attempt_kind`、模型（环境变量，缺省 `unrecorded`）、Policy 版本、public/independent 证据摘要、
  formal 引用、产物目录、限制与停止原因；`human_review` 保持 `pending`，不替真人下结论，也不回填历史 Task。
- 写入：`write_run_record` 排他创建。记录路径已存在时在**领取之前**拒绝（不创建 Run）；重放直接返回
  `existing_run` 且不写记录；清单加载时拒绝两个条目共用同一路径。执行抛错时先写记录再抛出
  `WorkerRunExecutionError`，异常上携带该记录的信息供调度摘要使用。
- 已知缺口：进程在"运行结束"和"写记录"之间崩溃时不会留下记录，重启后的重放也不会补写——这一段的补偿需要
  单独入口，本版没做，也没有假装能自动补。

## 实现记录：已有 Run 的记录补写（2026-11-19）

上面记的缺口（提交后、写记录前崩溃）现在由可信操作者显式调用 `python -m devflow_ai.record_backfill` 补上，
但它**不是自动恢复**：不建 Run、不重跑、不接管、不改库，只读证据并写一份新记录。

- 复用：`compose_run_record` → `classify_outcome`（为支持"没有运行期输入"的调用，它改成接收 `task_id` /
  `independent_suite_version` 与可选的 `limits`/`model_id`/`attempt_index`/`finished_at`，两个在线入口的调用
  保持不变），写入仍用 `write_run_record` 的排他创建。
- 证据边界：`formal_pass` 要求正式 Verification、草稿与独立回执都能从磁盘读回并互相吻合；篡改或缺失一律
  降级为 `incomplete`（或直接拒绝），不按标签补造。独立评测是否尝试过无法从行里恢复，传 `finalization=None`，
  分类器据此记为未尝试/未记录。
- 不推测的字段：截止时间（`deadline_fired=False`，不写 timeout）、`attempt_kind`（默认 `unknown`，显式声明时在
  notes 里注明是操作者声明）、模型（`unrecorded`）、人工审核（`pending`）。
- 拒绝条件：Task/Run 不匹配、Run 仍 RUNNING、Run 没有结束时间、任务包与 Task 的 base commit 不一致、目标文件
  已存在（含并发写入时排他创建失败）。
- 未做：自动发现"该补哪次 Run"、批量补写、把补写结果回填数据库或原记录。补写产生的是**新文件**，与在线记录
  一样需要真人 `benchmark_record review`。
