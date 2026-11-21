# 受控 RSI 首轮启动核对

核对日期：2026-11-18。依据 [SPEC §10.8](SPEC.md#108-评测驱动优化与受控-rsi)，结论为 **尚不能启动自动循环**。完整 Memory 或 Skills 并非首轮的前置条件；本清单也不是运行授权。

| 条件 | 现有基础与限制 | 启动前仍需核对 |
| --- | --- | --- |
| 真实执行与独立验收 | [评测报告](EVALUATION_REPORT.md)记录 Click #3019 一次真实正式通过和人工审核，以及 #3084 两次未完成；可信侧评测与公开检查分开。 | 复核所选实验基线的正式证据、可信材料及人工审核；单次成功不能证明多任务稳定。 |
| 恢复 | V1 有租约、心跳、部分失败恢复及隔离测试；[交付状态](V1_DELIVERY_STATUS.md)说明高级恢复尚未完成。 | 按拟定实验的中断和恢复场景核对可重复证据与人工处置边界；目前不能据此认定整项门槛已通过。 |
| 可重复评测 | [运行手册](BENCHMARK_RUNBOOK.md)定义任务包、固定 Policy 和正式证据；[进度记录](PROGRESS.md)包含构造测试和记录一致性修复。 | 固定任务、版本与口径，补足可复核的真实重复评测证据；模型桩或构造测试不能代替真实重复运行。真实运行须另行明确范围与预算。 |
| 执行版本和回退 | Task 固定目标仓库 base commit 和 Policy 快照，运行记录可保存模型及 Policy 版本；Git 提供代码版本追踪基础。 | 固定 DevFlow 执行代码、配置、模型、提示词、上下文策略及启用的 Skill/记忆快照；明确不可用时拒绝行为、相同 Task 后续 Run 的版本选择，并验证回退。目标仓库 commit 和 Policy 不等于完整执行版本。 |
| 实验隔离 | 现有任务工作区、Harness/Policy、可信评测侧和隔离数据库测试提供可复用机制。 | 针对候选进程、独立工作区、实验数据库及记录验证隔离与权限；限制候选访问隐藏材料和修改评分、晋升控制。工作区本身不是安全沙箱。 |
| 实验总预算与停止 | Policy 可限制单次 Run 的资源；运行记录能记录任务与结果。2026-11-18 增加了实验级账目表与服务（见文末「最小实验账目」），固定候选数、跨候选模型 token 总量、总时长与晋升阈值。 | 固定候选总数、全部模型用量、总时长、停止条件及晋升阈值，并由可信控制端跨候选累计和执行；单次 Run 限额不能自动推导出实验总限额。只有模型 token 由该账目约束，算力、墙钟执行时间和人工时间仍无计量；账目尚未接入任何调度或基准入口。 |
| 晋升与谱系 | 正式 Verification、人工审核及记录汇总可复用；首轮目标按 SPEC 限定 ContextBuilder 的日志与证据选择。实验账目能记录候选谱系、逐次尝试结论和淘汰理由；接受决定暂不可写入。 | 在生成候选前固定 V0、允许改动、任务划分、正确性/成本/回归阈值；记录候选版本、结果、累计成本与接受或淘汰理由，人工审核后才供后续新 Task 使用。当前未建立可验收的自动晋升闭环。 |

下一步优先在不运行模型的前提下，明确执行版本引用及实验记录的最小契约，检查它与现有 Task/Run、记录和 Policy 的兼容性；同时为重复评测和恢复列出可核验材料。是否进行额外真实任务，另行确定范围、预算和证据后执行。不得读取或复制隐藏测试、参考答案或可信评测材料到候选上下文。

## 实验隔离与回退：现状、证据与缺口（2026-11-19）

SPEC §10.8 要求候选代码在单独工作区和进程运行、实验数据库与主业务隔离、候选无权修改评分与晋升控制。
逐项核对当前实现，**有证据的**与**仍只是计划或存在缺口的**如下。表中的"证据"指代码路径或测试，不是
安全审计结论。

| 组件 | 实际位置与进程 | 现有证据 | 缺口 |
| --- | --- | --- | --- |
| 候选代码 | DevFlow 自身的一份修改副本，作为 worker 进程**导入的包**运行 | `execution_binding` 在领取前核验正在运行的包目录（列出的字节、可选模块完整性），`RunCoordinator.start_initial_run` 把核验过的摘要写进 `runs.execution_sha256`，`Worker.execute_run` 执行前再比对 | 候选与可信控制端是**同一进程、同一份代码**；没有"候选在单独进程运行"的方式，因此候选若改写控制端代码，进程内没有第二份可信副本可对照 |
| 可信控制端 | `python -m devflow_ai.experiment_record` 等命令，属于同一个包；晋升与分类读取数据库行与磁盘工件 | 诊断与决定共用同一份条件实现；登记表按 Run 冻结记录摘要；`accepted` 与自动循环禁用 | 这些逻辑本身就在候选要修改的包里。当前真正的外部约束只有人工审核与禁用开关，不是代码隔离 |
| 实验数据库 | 与 Task/Run 同一个库，仅表级分离；测试使用隔离 schema 或独立测试库 | 表级唯一约束、行锁与并发测试（同一 Run 唯一归属、预算不超卖） | 生产没有独立库或独立凭据：候选进程只要拿到 DSN 就能直接写实验表。测试里的隔离不能替代部署期隔离 |
| 工作区 | 每 Run 一个目录（`workspaces_root/<run_id>`），排他创建，运行期有 workspace 锁 | `harness` 对读取、补丁、pytest 的 `pythonpath` 做工作区内路径检查；独立评测只把工作区**只读**挂载进容器 | 工作区不是进程沙箱：`run_command` 只固定已注册命令与 cwd，被启动的进程仍以运行用户身份工作，可以写工作区之外的路径 |
| 评测材料 | 控制端目录，位于 pack/工作区/产物/本地 checkout 之外 | `require_material_placement` 拒绝上述位置；容器运行使用 `--network none`、只读挂载、丢弃全部 capability、非 root 用户 | 材料在收尾时由**同一个 worker 进程**读取；放置检查是路径检查，不是访问控制。候选若就是该进程的代码，就能读它 |
| 回退 | 旧工件副本所在的目录 + 其 manifest 与摘要 | `execution_binding.verify_task_version`：只读地取该 Task 记录的摘要，要求历史唯一且无 NULL（未知与混合都拒绝），并核验指定副本的字节与 manifest 一致。新 Task 用 `start_initial_run(execution_sha256=...)` 显式记录选择；同一 Task 的后续尝试仍由 `RunVersionConflictError` 固定。**构造验证**（`tests/test_artifact_launch.py`）：独立子进程被明确指向一份本地副本，先由控制端核验同一份字节，子进程再用该副本自带的 `verify_optional_binding(require_complete=True)` 核验正在运行的这个包，通过后才执行可观察逻辑，并报告 `devflow_ai.__file__`、已加载模块与摘要 | 仍没有产品内的启动旧版本入口：入口（`worker_run`、`Worker.execute_run`）核验的始终是本进程导入的包，所以真实回退仍要求操作者部署旧包并从它启动 worker；`runs` 只记录摘要，不覆盖字节码缓存、第三方依赖、模型与提示词 |

因此可以说的是：候选版本的指定副本**可被核验，摘要可被记录**；实际从该副本启动及回退仍待验证。
不能说候选已在独立进程或安全沙箱中运行，也不能说候选无法影响评分与晋升——后者目前只由"人工审核 +
`accepted` 禁用 + 控制端自己的代码"约束，属于已知缺口。想闭合它，需要把控制端/评分与候选执行拆成两个
进程、两套凭据（至少是两次部署），并为实验库准备独立凭据；这些都不是本轮的小改动，未实现。

`verify_task_version` 的边界：它证明的是"该目录下的字节与那份 manifest 一致，且这份 manifest 的摘要
正是该 Task 记录的那个"，不证明运行这些字节的进程就是该副本，也不证明清单完整（调用方需提供完整清单）、
不证明新 Run 会使用它。它只读数据库，不改写历史 Run，也不为缺失摘要的历史猜测版本。

回退相关测试：`tests/test_version_rollback.py` 6 项（记录的版本可在一份副本上重新核验；无记录、含 NULL
或历史混合时拒绝；字节变化、文件缺失、换 manifest 时拒绝；新 Task 显式选择旧版本、已固定 Task 不因同一
预检而被换成别的版本）与 `tests/test_artifact_launch.py` 5 项、`tests/test_worker_artifact_execution.py` 5 项（见下），均为临时工件与构造 Task。

**旧工件实际启动：做到哪一步（2026-11-19 构造验证）。** `tests/test_artifact_launch.py` 复制当前包到两个
本地目录 V0/V1，分别给 `__init__.py` 追加一个可观察的探针函数，为整包建立清单与摘要，然后：

* 部分用例由测试父进程用 `verify_execution_binding(root=<副本>)` 检查工件；另有独立用例检查摘要不符、
  字节被改、文件缺失和目录不存在时该检查会拒绝。测试帮助函数本身不强制每次启动前执行该检查；
* 子进程的环境被显式重置：只把该副本目录放进 `PYTHONPATH`，清掉继承来的 `PYTHONPATH`/`DEVFLOW_*`，
  `PYTHONDONTWRITEBYTECODE=1`，工作目录是一个空的临时目录；并显式移除可编辑安装可能通过 `.pth`
  添加的当前源码目录；
* 子进程导入 `devflow_ai` 后先确认 `__init__.py` 就在请求的目录里，再用该副本自带的
  `verify_optional_binding(require_complete=True)` 核验运行中的包（清单摘要、文件字节、已加载模块是否
  都在该目录、清单是否覆盖目录下全部模块），**通过之后**才执行探针与一个真实的 DevFlow 函数
  （`command_evidence_sha256`）；
* 结果可与当前工作树、另一份版本区分：V0/V1 的探针返回值不同，`devflow_ai.__file__` 分别指向各自目录，
  而当前工作树的包没有探针；子进程的 `sys.path` 里没有工作树目录。

本机还观察到一个有用的负面证据：测试环境的解释器里另有一份可导入的 `devflow_ai`（site-packages），
当请求的目录不存在时子进程**并没有**退回去执行它，而是报 `wrong_artifact` 并退出（另一种可能是
`import_failed`）——这正是"不静默使用当前版本"要挡的情形。

三件事必须分开说，本批**只做到第一件**：

1. **测试中的子进程确实从指定副本加载并执行了代码** —— 有证据（上述断言），但这是构造的启动方式；
2. **真实 Worker 从指定副本执行完整 Run** —— **没有**证据：产品内没有这样的启动路径，`worker_run` 与
   `Worker.execute_run` 核验的是本进程导入的包，入口摘要仍由调用方提供；
3. **候选与控制端安全隔离** —— **没有**证据，见上表的进程/凭据缺口。

**控制端显式启动指定版本 Worker（2026-11-19 最小实现）。** `devflow_ai.version_launch` 是控制端唯一新增的
"按名字启动某个已部署版本"的能力，范围刻意很窄：

```
python -m devflow_ai.version_launch --artifact-root <deploy>/devflow_ai --manifest <M> \
    --execution-sha256 <PIN> --python <abs interpreter> [--passthrough NAME ...] \
    -- --pack <pack> --task-id <uuid> --policy <file> --operator <name> \
       --python-executable <abs interpreter> --workspaces-root <dir> --artifacts-root <dir> \
       --attempt-kind rehearsal --confirm-run
```

* `--artifact-root` 是**包目录**（含 `__init__.py` 的那个），与子进程自检所用的根一致；
* 启动前由当前控制端做 `verify_execution_binding(root=<该副本>, require_complete=True)`：清单摘要必须等于
  独立传入的 pin，列出的文件字节必须一致，且清单必须覆盖该副本下的全部 Python 模块。`require_complete`
  里"本进程已加载模块来自别处"的检查只对**正在运行的包**生效，对另一个副本会自动跳过——这是现有语义，
  没有为适配它而放宽任何东西，子进程仍对自己的运行包做完整检查；
* 命令是**一个解释器、一个固定 bootstrap、一个参数数组**：
  `<python> -P -B -c <bootstrap> -- <工件父目录> <worker_run 参数>`，再由启动器补齐
  `--execution-manifest` / `--execution-sha256`（调用方自带这两个参数会被拒绝）。没有 shell 字符串；
  `-P` 不让工作目录进入 `sys.path`，`-B` 不往工件目录写字节码；工件父目录**只**作为命令行参数出现，由
  bootstrap 在启动之后插入 `sys.path[0]` 再 `runpy.run_module("devflow_ai.worker_run")`；
* 子进程环境是显式白名单：`PATH`/`SystemRoot`/`TEMP`/`HOME` 等少数变量、`DEVFLOW_DB_*`，以及操作者用
  `--passthrough` 逐一列出的变量（例如测试用的 `PGOPTIONS`）。`PYTHONPATH` 被替换为该副本的父目录，
  `PYTHONNOUSERSITE=1`。**模型凭据默认不继承**：要用必须显式列出；启动器不主动输出环境值，但子进程输出和事件仍需按其实际内容处理；
* 版本核验依然发生两次：控制端在启动前，`worker_run` 在领取前对它**自己导入的包**，`Worker.execute_run`
  在工作区初始化前再核验一次。启动器不替代、也不绕过这三处。

实测证据（`tests/test_version_launch.py`，构造测试、隔离 schema 与临时工件，无模型调用）：

* 子进程输出 `worker_run` 事件，最后是 "Model gateways are not configured"；测试观察到其通过版本和
  Task/Policy 检查并在模型调用前停下，但此路径未直接报告或断言实际导入模块的文件路径。构造的 Run 仍只有一条、摘要不变；
* 副本被改一个字节、pin 不匹配、工件目录缺失、解释器不可用，都在**启动进程之前**拒绝（测试用一个会记录
  调用的假 runner 断言"没有启动"）；
* Run 固定为 V0 时，用 V1 的副本去启动会被子进程入口以 "different execution version" 拒绝（这是子进程
  自己的判断，不是父进程的预检）；
* 环境白名单、固定命令数组、CLI 拒绝路径各有断言。

**启动钩子的复现与修正（2026-11-19）。** 先复现：工件父目录在 `PYTHONPATH` 上时，解释器启动阶段会
导入该目录里的 `sitecustomize`，它**先于** Worker 的版本核验执行（测试用只写标记文件的钩子实测到这一点，
见 `test_the_previous_startup_shape_did_import_the_deploy_hook`）。修正后启动器不再用环境变量指向工件目录：
`<python> -P -B -c <bootstrap> -- <工件父目录> …`，工件目录在启动之后由固定 bootstrap 插入 `sys.path`。
实测（`test_a_sitecustomize_beside_the_deployed_package_never_runs`，真实子进程）：同样的钩子放在工件
父目录，子进程跑通 `worker_run` 的自检并停在"模型网关未配置"，**标记文件未被写入**。

比较过的方案与取舍：`-S` 能挡住 `site` 的启动钩子，但实测本机虚拟环境下 `-S` 会丢掉 site-packages
（`import sqlalchemy` 直接失败），要用它就得手工把 site-packages 拼进 `PYTHONPATH` 并放弃 `.pth` 语义；
`-c` bootstrap 不改依赖加载路径，只把工件目录延后到启动之后插入，因此选用它。代价是入口从 `-m` 变成
"固定 bootstrap + `runpy.run_module`"，仍是同一个模块、同一个 `__main__` 语义。

**这一步没有覆盖的**：site-packages 里的 `sitecustomize` 或 `.pth`（例如可编辑安装）仍会在启动时执行，
也可能提前把工件父目录加入启动路径；因此只有该目录本来不在解释器启动路径上时，测试中的钩子才不会由
这一路径提前加载。解释器、依赖与字节码均未固定；预检到自检之间的文件替换可能在检测之前已执行代码。
候选进程与控制端/数据库凭据的隔离也未改变。这不是安全沙箱、完整版本证明，也不构成 RSI 门槛。

**启动器 → 领取 → 受控阶段（构造证据，注意其启动形态）。** `tests/test_version_launch.py` 中
`test_the_entry_from_a_copy_claims_a_run_and_reaches_real_stages_with_stub_gateways` 用的是**旧的启动形态**
（`PYTHONPATH` 指向工件父目录 + 该目录里的 `sitecustomize` 注入模型桩），**不是**加固后的启动器路径——
加固恰恰移除了能这样注入的钩子。它证明的是：该副本的 `worker_run` 能领取 Run（`execution_sha256` 与副本
pin 一致）、真实 `git clone`/`git checkout`、并驱动 Supervisor → Explorer（真实 `read_file`）→ Planner →
Supervisor（BLOCKED）等真实阶段，模型回复是桩。由于无法在不改产品接口的前提下既走加固启动器又注入桩，
两条证据分开保留：启动器侧证明"启动、自检、拒绝"，阶段侧证明"领取与阶段"，**不拼成一条链**。

仍需保留的限制：控制端预检、进程启动与子进程自检之间可能发生文件变化；自检前已经导入的代码不能由 pin
证明安全。启动器不冻结副本、不是沙箱、不构成候选与控制端之间的隔离；数据库凭据仍被子进程继承。默认不设
额外启动器超时；显式设置超时时，超时会终止子进程，可能留下需恢复的 Run，且不保证后代进程同步终止。
没有实现"自动安装/切换版本"，也没有生产级回退。

**子进程里的 Worker 执行边界（2026-11-19 第二批构造验证）。** `tests/test_worker_artifact_execution.py`
在同一套"副本 + 清单 + 摘要"上再走一层：子进程从指定副本导入 `devflow_ai.worker`，用**该副本自己的**
`Worker.execute_run` 执行一个已领取的 Run（Run 记录的摘要就是该副本的清单摘要）。边界核验通过后，真实执行
`git clone` 与 `git checkout` 到临时工作区，再进入受控阶段桩，记录工作区路径、实际 commit 与文件列表；
测试核对子进程里 `devflow_ai.worker.__file__` 与 `devflow_ai.__file__` 都在指定副本内、`outside` 为空、
子进程预检摘要等于 Run 记录的摘要、`git_clone`/`git_checkout` 两条 ToolCall 存在、Run 的身份和版本及 Task 的基线保持不变。Worker 会写入执行过程的 ToolCall，不能据此称 Run 的所有状态均未变化。
负路径四条：**跳过子进程预检**、让 `execute_run` 自己的版本门来拒绝（摘要不符、工件被改、清单缺失 →
`ExecutionBindingError`，工作区目录未创建、无 ToolCall、阶段未运行）；Run 固定为 V0 时用 V1 执行被
`RunVersionConflictError` 拒绝；请求目录不存在时**不**回退到可导入的当前副本。

仍要分清界限：这里执行的是**受控阶段桩**，不是五角色完整阶段流程；子进程必须先导入代码才能调用边界，
所以"拒绝"的含义是"在工作区初始化与目标阶段之前"，不是"在导入之前"；部署时怎么从旧工件启动、候选与
评分控制端是否隔离，仍没有实现或证据（见上表）。

还要说清它**不是**什么：子进程只是进程边界，不是沙箱，也没有独立的信任根——子进程是用**副本自带的
代码**核验自己的，导入及自检代码会在核验通过之前执行；父进程单独检查不是强制启动门禁。两者都不能证明内存中的字节等于磁盘上的字节，也不能
证明清单完整（清单由操作者选择）。

## 手动触发的构造实验演示（2026-11-19）

一条可以直接跑的演示：

```
uv run --env-file .env.test --locked --no-sync python -m pytest tests/test_rsi_demo_flow.py -s
```

它自己启动一个**全新的临时 PostgreSQL 集群**（两个角色：Worker 侧与控制侧）、跑项目真实迁移链、建一个
临时工作目录，然后用模型桩走完一次构造流程。使用 pgserver 或本地已有的 Docker 镜像 postgres:17.11；
两者均不可用时测试跳过。默认不调用真实模型，不连任何既有数据库，结束后销毁临时实例。

**这一次实际串起来的**（都在同一个测试进程里，但用两个真实数据库身份）：

1. `ExperimentService.open` 固定 V0 引用、声明 Task、候选上限 2、token 上限 10 000、时长上限 1800 秒、
   晋升阈值；`admit_candidate` 登记候选 `v1` 及其摘要（V0/V1 是构造引用，不是真实部署包的清单摘要）。
2. Worker 身份 `start_initial_run(..., execution_sha256=v1)` 领取 Run，Run 记录的版本与候选摘要一致。
3. `drive_run_stages` 用 `model_gateways_from_env(..., experiment_budget=client)` 组装网关驱动真实阶段：
   Supervisor → Explorer（真实 `read_file`）→ Planner → Supervisor（BLOCKED），6 次模型调用全部是桩。
4. 每次模型请求都经**可信预算通道**（控制端身份）完成预留、dispatch 与结算：账目 6 条预留、全部
   `settled`、合计 90 token，与 Run 侧记录的用量一致；Worker 身份对实验表仍然没有任何权限。
5. `record_task_result(outcome="incomplete")` 按声明来源登记（没有正式验收，**不记** `formal_pass`）。
6. `explain_acceptance` 说明未满足的晋升条件（可分类正式通过 0 个 Task 等）；`decide(accepted)` 被拒，
   随后以书面理由 `rejected` 淘汰该候选——`accepted` 依然禁用。

测试同时断言：打印出的摘要与数据库行一致（实验、候选、Task/Run、结果、桩用量、账目状态、晋升问题）；
另有一条失败路径用例证明额度不足时**不发送**模型请求、也不产生预留。

**它是构造演示，不是真实实验**：模型是桩、任务是构造的、V0/V1 只是引用摘要；工作区由测试直接创建，
并非由 Worker 克隆或从指定版本启动。阶段结束后测试直接将构造 Run 状态改为 FAILED，以便登记 incomplete，
这不是通过产品终止/恢复路径完成的。没有独立评测、没有正式验收、
没有自动候选生成或循环。默认的 `worker_run`、调度器与 `benchmark_run` 行为不变，也没有接入自动 RSI。

**双进程版本（同一文件的第二个用例）。** 控制端进程固定实验与候选、持有账目凭据，并开一条本地会话；
Worker 子进程用自己的数据库角色与自己的连接参数（经 **stdin** 送达，环境里没有任何 `DEVFLOW_DB_*`）
自行领取 Run、驱动同样的三个阶段，把每次模型桩请求交给会话完成预留/dispatch/结算，最后把构造 Run 收尾为
FAILED。控制端在同一个测试里核对：子进程报告的 pid 与本进程不同、数据库角色为 Worker 角色（两者是子进程自述）、Run 的
`execution_sha256` 等于候选摘要、AgentExecution 中确有 supervisor/explorer/planner、账目 6 条预留全部
settled 且合计 90 token 与 Run 侧用量一致、会话共处理 18 条消息（每次调用 reserve+dispatch+settle）、
Worker 角色对实验表仍无权限；随后登记 `incomplete`、拒绝 `accepted`、以书面理由淘汰。另有一条失败路径：
额度不足时会话拒绝，子进程一次模型桩都没有调用，账目里没有预留。阶段覆盖仍只是 Supervisor → Explorer →
Planner，Coder 与 Verifier 没有被路由。

## 可信控制端 + 受限 Worker 的预算闭环（2026-11-19，构造验证，未接入默认入口）

在上一节的进程内原型之上，本批补上了**两个进程 + 两个数据库角色 + 一条本地会话**的最小闭环。默认仍然关闭：
`worker_run`、调度器、`benchmark_run` 都不构造它。

**通道与会话**：`experiment_channel` 增加 `LocalBudgetSession`（控制端）、`connect_session` /
`connection_transport`（Worker 端）与 `session_address`。地址是私有临时目录里的 Unix socket（Windows 为命名
管道），监听器由上下文管理；构造测试中会话密钥由控制端生成 32 字节随机值，用
`multiprocessing.connection` 的 keyed handshake 做握手——密钥不对的一端拿不到任何应答。**密钥与 Worker 的
数据库连接参数通过子进程的 stdin 一次性送达**，测试子进程使用环境白名单；这是测试接线，不是现成部署方案。会话在控制端构造时固定
实验与候选，Worker 无法改写绑定；`ExperimentService` 的行锁、候选归属核对、单飞与 unknown 规则原样保留。

**实测（`tests/test_experiment_db_privileges.py`，独立临时 PostgreSQL，两个真实角色，模型桩）**：

* 成功路径：控制端进程建 Task/Run/AgentExecution 并开实验；Worker 子进程用自己的角色与 DSN 连库，经本地
  会话请求 预留 → dispatch → 结算。控制端计 3 条消息，实验账目一条 `settled`、15 token，模型桩被调用 **1**
  次；结算后 Worker 角色对 `experiment_reservations` 的 SELECT/INSERT/UPDATE/DELETE 仍然全为假。
* 会话密钥不对：控制端 `accept()` 抛 `AuthenticationError`，Worker 子进程以 `connect_failed` 退出（退出码 2），
  没有记账、没有模型调用；空密钥在连接前就被拒。
* 既有的进程内用例继续覆盖：预留/dispatch 答复丢失、结算失败、额度不足、竞争发送、跨实验/跨候选、
  别的 Task 的 Run 或不属于该 Run 的 AgentExecution——全部按现有状态机拒绝或保守保留。

**边界**：密钥是共享秘密而不是身份认证——同一用户下的其他进程若能读到密钥文件或连上 socket 就能请求；
没有 TLS、没有防重放（只有握手）、没有跨机部署；Worker 自报的用量仍可能不可信；候选若绕过网关直接调用
提供方，通道拦不住；数据库权限、执行版本、解释器/依赖与文件系统隔离仍需单独解决。因此本批能声称的只有：
"在构造测试环境中，两个进程和两个真实数据库角色经受限本地通道完成了一次模型桩预算闭环，并验证了所列
失败路径"，**不是**硬性总预算、完整候选隔离或 RSI 有效性证据。`accepted` 与自动循环保持禁用。

审查修复：原型最初用 `Connection.recv()` 自动反序列化 pickle，已认证的候选因此可能在控制端执行代码；现改为
有 64 KiB 上限的 JSON 字节消息，拒绝非法输入，客户端等待响应默认 5 秒，超时关闭连接以避免把迟到响应误当作
后续请求的结果。该超时只覆盖发送后等待响应，不覆盖连接建立、认证握手、阻塞发送或控制端 accept/空闲等待；
服务端缺少完整的生命周期和拒绝服务控制，本原型仍不能部署到不可信环境。

## 可信预算通道原型（2026-11-19，未接入任何默认入口）

`src/devflow_ai/experiment_channel.py` 把 `ExperimentBudgetHook` 的五个方法拆成两侧：

* **Worker 侧** `TrustedBudgetClient` 提供与 hook 相同的接口，但**没有数据库会话**，不读写任何实验表；
  它只发消息，失败时按保守规则行事（见下）。`ModelGateway(experiment_budget=client)` 可以直接用它，
  网关代码不需要改。
* **控制端** `TrustedBudgetServer` 在构造时固定实验、候选标签与操作者，账目决定全部交给既有
  `ExperimentService` 状态机；dispatch/settle/unknown/release 在同一事务持实验及预留锁后先核对候选归属，
  再调用原服务。没有第二套预算判定，也没有因此实现调用方认证。

**请求身份**：Worker 为每个逻辑请求生成一个 `client_key`（UUID），控制端把它与实验 ID 派生成
`request_key = uuid5(namespace, "<experiment>:<client_key>")`。因此预留响应丢失后的重试会命中同一
`request_key`，`reserve` 的幂等分支不会再记一笔；`mark_dispatched`/`settle` 也按 key 幂等（同额重放成功、
不同额冲突）。**限制**：这只保证"记账最多一次"，不保证模型恰好调用一次——Worker 在发送后崩溃再重试，提供方
可能被调用两次，而账目仍只有一条，用量以提供方回报为准。

**保守失败规则（已用真实数据库与失败注入验证）**：预留答复不明确 → **不调用**；dispatch 答复不明确 →
**不调用**，预留在服务端可能停在 `reserved` 或 `dispatched`，两种都不当作已释放；dispatch 之后模型抛错或
用量未知 → `unknown`；结算记录失败 → 保留 dispatched 未结算并阻止下一次请求；`release` 只用于可证明未发送
的请求，dispatched 之后服务端会拒绝（客户端把拒绝抛出，网关的 best-effort 释放会吞掉它，占用保留）。

**实测（`tests/test_experiment_db_privileges.py`，独立临时 PostgreSQL + 两个真实角色 + 模型桩）**：
Worker 身份经客户端完成一次预留→dispatch→结算，账目为 `settled`/15 token，模型桩只被调用一次，而 Worker
身份仍然读不到预留表；响应丢失的重试只产生一条预留、一次调用；dispatch 答复丢失时不发送、保留 dispatched
未结算并让下一次请求被拒；额度不足时不发送、不记账；结算无法记录时模型被调用一次但账目保持 dispatched
未结算并阻止后续；拿另一个实验的 `request_key` 去 dispatch 被拒；**用 Worker 凭据自建服务端**时数据库直接
拒绝（`permission denied`），不会记账；请求指向别的 Task 的 Run 或不属于该 Run 的 AgentExecution 也被拒。
顺带发现控制端角色还需要 `agent_executions` 的 SELECT（归属校验会读它），已体现在测试授权里。

**不是**：没有认证——任何能到达该服务端的进程都能请求预留，保护来自"它没有账目凭据"而不是身份验证；
传输是**进程内可注入函数**，没有网络服务、没有队列、没有跨进程部署；默认的 `worker_run`、调度器与
`benchmark_run` **都不构造**它，V1 行为不变；它也无法限制绕过网关直接调用提供方的候选，不构成硬性总预算
或完整候选隔离；`accepted` 与 RSI 循环仍禁用。

## 候选 Worker 与实验账目的数据库权限（构造验证，2026-11-19）

**做法**：`tests/test_experiment_db_privileges.py` 在本批新建的**独立临时 PostgreSQL 集群**（`pgserver`
在该测试自己的临时目录里 initdb，凭据为临时随机值、不打印）上建库、建两个角色、跑项目真实迁移链，
再用两个真实连接身份验证边界。不涉及共享测试库、业务库或任何既有实例。

**两条路径的实际需要**（据代码核对）：Worker 侧构造一次尝试需要读写 `tasks`（领取时改状态）、插入
`runs`、`agent_executions`、`tool_calls`、`handoffs`、`artifacts` 以及序列；控制端需要写 `experiments`、
`experiment_tasks`、`experiment_candidates`、`experiment_reservations`、`experiment_task_results`、
`experiment_records`，并读 `tasks`/`runs`。测试按这个清单授权：Worker 只拿到 Run 侧六张表与序列的
SELECT/INSERT/UPDATE/DELETE，实验账目六张表**一条都没给**；控制端反之。

**实测（真实 SQL 与真实连接，全部通过）**：

* Worker 身份对六张实验表做 INSERT/UPDATE/DELETE/**SELECT** 一律 `permission denied`（SQLSTATE 42501）；
* 控制端身份能真实创建实验（`ExperimentService.open` 写入 `experiments`/`experiment_tasks`）并读回；
* 没有旁路：`pg_has_role(worker, control, 'member')` 为假、`SET ROLE control` 被拒、
  `has_table_privilege(worker, <实验表>, ...)` 全为假、`has_schema_privilege(worker,'public','CREATE')`
  为假、`pg_default_acl` 为空、`public` 模式下没有授予 `PUBLIC` 的表权限；
* Worker 身份**能**完成构造尝试：`Worker.claim_once()` 领取并写入 Run，`Worker.execute_run` 通过版本门、
  真实 `git clone` + `git checkout` 到临时工作区并进入阶段桩；Run 与 Task 行由该身份写入；
* 该集群确实是新实例：`current_database()` 为本次新建库，数据目录与共享测试环境不同。

**关键发现：现有实验预算 hook 与这条边界直接冲突。** 把 `ExperimentBudgetHook` 换到 Worker 身份后，
`open_request()` 在 **`experiments`** 表上就被拒绝（`permission denied for table experiments`，即
`ExperimentService.reserve` 的第一步），根本到不了写 `experiment_reservations`。这不是测试桩造成的，
而是现有设计把账目写入放在 Worker 进程内。**没有**为了跑通而给 Worker 授予账目权限，也没有让 Worker
改用控制端凭据；结论就是"今天不能既做权限分离又保留现有预算路径"。

**最小后续契约（设计，未实现、未验证）**：预算必须由可信侧执行，Worker 端只发请求、不碰账目表：

1. Worker 进程用**自己的**身份运行，不持有任何实验表权限；
2. 每次模型请求前，Worker 把（实验 ID、候选标签、Run/AgentExecution、估算 token、控制端预先分配的
   request_key）交给可信侧的预算通道；通道用**控制端身份**执行 `reserve`/`mark_dispatched`/`settle`/
   `settle_unknown`/`release`，并把"可以调用/不可以调用"作为结果返回；
3. 通道不可达、超时或返回不明确时按**不可调用**处理：不发送请求，也不假设额度已释放；
4. 已经 dispatch 过的请求不能 release；用量未知只能 `settle_unknown`，账目保持不完整并停止后续预留；
5. 通道自身的身份认证、审计与本条契约的实现都属于后续工作，本批只给出形状，不声称可用。

**边界**：以上只证明"在同一实例的不同角色之间，Worker 不能读写实验表、控制端可以"；测试 Worker 对六张
Run 侧表获得全部 DML，并非逐语句最小授权，仍可直接修改其中的数据，不能证明评分证据完整或候选无法伪造结果。
Windows 复验在本批专门新建的临时 PostgreSQL 容器上运行，迁移只作用于该容器；缺少 pgserver 时，测试可使用
本地已有的 postgres:17.11 镜像，无可用引擎或镜像则跳过。它**不是**安全
沙箱。真实部署仍是一库一凭据、Worker 进程直接连库；解释器、依赖、启动钩子与凭据隔离都没有解决；
候选与评分控制端的进程隔离也未实现；`accepted` 与 RSI 循环仍禁用。

## 最小执行版本契约（设计；清单核验与启动绑定已实现，其余仍为设计）

执行入口边界（2026-11-18 更新）：`Worker.execute_run` 现在**自己**接收并核验执行清单（`execution_manifest` +
`execution_sha256`，成对、按包完整性核验），核验后再与该 Run 领取时记录的摘要比较——摘要不同即拒绝执行，
且拒绝发生在工作区初始化与任何 Agent 阶段之前；没有固定摘要的 Run 保持 V1 行为。因此"只在上层入口核验"的
缺口已闭合到执行边界，`worker_run`/`benchmark_run` 会把这对参数传进来而不是只传摘要。仍然成立、不能被这套
检查覆盖的限制：**不能证明内存中的代码与磁盘字节一致**、不覆盖字节码缓存与第三方依赖/解释器版本、阶段级
方法（`execute_initial_supervisor`、`execute_explorer` 等）是驱动内部接口而非对外入口，记录来源仍由调用方
提供（`record_provenance = caller_supplied`）。RSI 接受仍禁用。

审查补充：清单核验发生在进程导入之后，只核验当时磁盘上的包源码；不能证明已加载代码、字节码、依赖或后续运行一直与清单一致。再诊断重放已有 Run 并可能继续执行，须核对本次提供的摘要与该 Run 的记录；无摘要或不匹配时拒绝。此比较仍不是独立执行证明，`accepted` 继续禁用。

2026-11-18 的局部基础：`devflow_ai.execution_manifest` 提供可信调用方显式指定的文件清单生成、规范化摘要与核验函数；`devflow_ai.execution_binding` 在领取前按独立 pin 核验磁盘包源码，并在 Run 记录摘要，`Worker.execute_run` 在执行前再次核验。清单仍不采集模型、配置、提示词或运行时状态，也不覆盖依赖；包入口检查当前包目录的 Python 模块是否全部列入。路径检查不构成对抗恶意并发文件替换的安全沙箱，生成或核验后文件仍可能变化；它不能证明实际执行版本。调用方须自行选择非敏感文件，不得把凭据或隐藏材料加入清单。

清单读取拒绝路径组件中的符号链接及 Windows 重解析点；已分别在 Linux 构造文件测试及 Windows 目录联接测试中核验。检查与打开之间仍存在并发替换窗口，不应将这些路径检查作为不可信代码隔离边界。

可信操作者可用 `python -m devflow_ai.execution_manifest create --root <目录> --file <相对路径> [--file <相对路径> ...]` 生成清单 JSON（标准输出）和规范摘要（标准错误）；保存清单时应使用 UTF-8。随后用 `python -m devflow_ai.execution_manifest verify --root <目录> --manifest <清单文件> --expected-sha256 <独立保存的可信摘要>` 核验；成功只报告所列文件数，失败退出码为 1。摘要必须通过可信渠道独立保存和提供，与清单放在一起并不能证明清单可信。该入口不会写文件、检查未列文件、调用模型或启动 Task。

一个版本引用必须能定位可信控制端固定的执行清单，清单至少覆盖 DevFlow 可执行代码及依赖、非敏感配置、模型提供方及模型标识、提示词与上下文策略、工具及启用的 Skill 版本、记忆快照或明确禁用状态。清单须有确定的版本和摘要；摘要只证明所比较的字节一致，不证明代码安全、资源隔离或提供方模型行为不可变。API 密钥和可信评测材料不属于清单；模型提供方若不能提供不可变版本或允许后台变更，应记录该限制，不能把模型名当成权重固定的证据。

新实验 Task 创建时，由可信控制端在启动前绑定一个版本引用；后续 Run、恢复和重放沿用该引用，不能通过重试切换。每次启动前必须解析引用、核对实际待执行工件与清单，缺失、不匹配或无法核验即拒绝启动并保留原因。待证明的是**实际执行**使用了指定工件：仅在数据库或记录里写入摘要，不能满足这项要求。候选执行代码与控制端核验、评分和晋升机制须分离；候选不能修改自己的核验标准。

已存在 Task、Run 和 BenchmarkRunRecord 不得自动赋予无法证明的版本；历史缺失显式标为未知，不回填猜测值。旧运行记录保持可读，实验记录通过明确引用和摘要关联，不能改写旧文件。Task 的 `base_commit` 是待修复仓库基线，`policy_snapshot` 是预算与权限快照，记录中的 `model_id` 是声明：三者均不能代替执行清单。人工审核接受后的版本只能供新 Task 显式选择；回退意味着在新 Task 选择仍可核验的旧工件，并验证其确实可运行，不能热切换已有 Task。

实现验收至少覆盖：同 Task 多 Run 和恢复固定同一版本；清单或工件缺失、字节变化及不可验证时启动前拒绝；不同 Task 可显式选择不同版本；已有 Task/Run 和旧记录保留未知；并发领取不能绕过版本绑定；回退工件不可用时拒绝而不是静默换版本。还需验证候选无法更改可信控制端、隐藏材料与评分；这些是后续接线和隔离测试要求，不是本设计已提供的保证。

## 最小实验记录与证据（设计，尚未实现）

在生成候选前冻结实验身份、V0 引用、允许修改的单一因素、开发/独立任务划分及任务包身份、指标口径、候选数和模型用量/总时长上限、停止条件与晋升阈值。实验期间为每个候选保存父版本、补丁与清单摘要、每次尝试的 Task/Run 和正式证据引用、状态及停止原因，并区分准备失败、未完成和可判定结果。保留失败及无收益候选，登记人工接受或淘汰理由及可回退版本；摘要和操作者声明不代替证据核验。

成本同时列每次尝试和实验累计：生成、开发检查、独立评测、重试与失败均计入；用量未知、结算未完成或可能超限时不得将未知记为零。正式运行的原始记录、独立回执和人工审核须可追溯，但记录不得携带隐藏测试、答案或凭据。并发预算分配需要可信控制端原子预留与结算、异常中止和未知结果处置；现有单 Run Policy 不能保证实验总上限，具体持久化和并发协议尚待设计及验证。

`usage_report --run` 统计已记录的 AgentExecution 用量；`usage_report --task` 用单条数据库查询列出 Task 准入和该 Task 每次 Run 的已记录用量，准入只计一次。未结算、空用量或旧格式时需要保留未知，数据格式不完整则拒绝报告；历史准入数据或其他成本可能缺失，查询后记录也可能变化，因此不能当成完整账单、整个实验的累计成本或预算控制。

重复评测所需材料包括固定的任务划分、目标仓库基线、执行清单、Policy、环境与可信套件身份、每次运行的完整结果和成本；标明缺失与环境差异，不能从同一案例重复声明推出跨任务稳定。恢复证据应覆盖拟使用的中断点、执行权丢失、重启与重复领取，并核验无重复外部执行及旧执行不能迟到提交；构造测试与真实运行分开记录，无法恢复时保留人工处置和停止证据。

## 最小实验账目与候选记录（最小实现，2026-11-18）

2026-11-18 阶段化复核：`dispatched` 防止可能已发送的预留被释放，并阻止其他请求预留或发送；发送前还重新检查 unknown、截止、关闭及预算条件。费用对账、候选归属和默认入口接线仍待完成；实验预算接线不得用于真实 RSI，`accepted` 禁用。

2026-11-18 预算接线审查：实验预留与 Run 预留分事务提交；模型调用后若标记 unknown 失败，预留保持 dispatched 未结算，阻止后续实验请求。输入 token 为估算值、供应商实际用量可能超过预留，结算后才能记录超额，不能宣称硬性总上限。已核对 Run 属于声明 Task、AgentExecution 属于 Run，但候选真实归属仍未得到证明。`accepted` 继续禁用。

审查更新：记录登记已保存 Run ID，并在实验内分别限制 Run 和摘要唯一；未登记的结果入账后不能事后补登记以改变来源。登记只固定记录字节，不证明作者或实际执行；`accepted` 继续禁用。

审查补充：出现未知用量后，剩余 token 显示为未知并拒绝新预留；仅靠操作者声明释放仍不能证明无用量。`record_classified_result` 可读取运行记录中的截止上下文，也可使用明确的调用方声明；套件版本仍由调用方提供，finalization 字典未持久化，因此不能声称完整还原原运行上下文。候选版本和 Run 归属仍未经独立验证；跨实验复用 Run 的协议也尚未确定。`accepted` 继续禁用。

本节记录**已经写进代码**的部分，用来替换上面两节中"设计，尚未实现"里关于预算与晋升记录的那一半。
它不改变本文件开头"尚不能启动自动循环"的结论：执行版本、实验隔离与可重复真实评测仍未完成。

实现位于 `src/devflow_ai/experiment.py` 与迁移 `d9b3c7e1a250`（新增 `experiments`、
`experiment_tasks`、`experiment_candidates`、`experiment_reservations`、`experiment_task_results`
六张表（另含 `experiment_records`；未应用到任何现有数据库）。打开实验时冻结 V0 执行清单摘要、**声明的任务集**、候选上限、
跨候选模型 token 总预算、墙钟预算（换算成 `deadline_at`）、晋升阈值（最少正式通过数、最多失败数）
和操作者。服务没有把这些字段做成可修改项：`open` 之后再没有更新它们的路径；直接改数据库仍是可能的，
那属于数据库权限问题，本实现不声称能阻止。

**跨候选预算是"先预留、后结算"，不是每次 Run 的限额。** 预算按"已结算 + 未结算预留"合计判定，
所以两个候选不能各自花掉整个上限；结算时把未用的部分退回，实际用量超出上限则把实验标为
`over_budget` 而不是改写数字。超预算的实验不能再记录 `accepted`。候选准入与预算预留都在实验行的
`FOR UPDATE` 下进行，并发预留由同一把锁串行化——`tests/test_experiment.py` 里 8 个线程各预留 300、
预算 1000，真实数据库上稳定只有 3 次成功。期限（`deadline_at`）之后拒绝新的候选与新的预留，
但仍允许记录已经结束的尝试、以及结算/释放已经预留的额度：停止的是新开销，不是已经发生的事实。

**尝试结论有两个来源，彼此不互相顶替。** 结论词表限定为 `formal_pass`、`failed`、
`public_only_pass`、`incomplete`、`timeout`。`evidence_source = 'declared'` 是可信调用方的声明：
服务只核对数据库能确认的前置条件（Run 存在、属于该 Task、已结束；`formal_pass` 还要求存在
`result=passed`、`evidence.scope = trusted_v1` 且带草稿、回执、套件版本引用的 Verification）。
`evidence_source = 'classified'` 是复用 `benchmark_run.classify_outcome` 得到的判定：同一个分类器、
同一条磁盘可信链，能和基准、调度记录、补写入口给出同样的结论。只有 classified 能带
`verification_id`，也只有 classified 进入晋升阈值计算；声明出的正式通过即使数据库前置条件满足，
也不会被算作通过。分类器认为链读不回来时如实记 `incomplete`，不会因为在库里看到 passed 就升级为
`formal_pass`。服务只保存 Verification id 和分类器给出的短原因，不读取、不记录评测材料内容。

**分类所需的尝试上下文以持久运行记录为准，不再依赖事后回忆。** 分类器需要 `deadline_fired` 与可选的
`finalization`，两者都无法从 Run 行读回：`runs` 没有截止时间列，finalization 只存在于执行它的进程里。
因此运行记录 `BenchmarkRunRecord` 在 schema 3 增加 `attempt_context`，由真正执行尝试的入口
（`benchmark_run`、`worker_run`）在写记录时填入它亲眼看到的事实——截止是否触发、有没有 finalization
及其步骤结果、以及来源标签；`record_backfill` 这类没有看到尝试的写入方只能填 `unknown`，旧记录没有
该字段，读作"未知"而不是"没有超时、没有评测"。`record_classified_result` 因此接受 `record_path`：
按同一份字节解析并计算摘要，要求记录指向本 Run（有 `task_id` 时必须一致）、不与该实验固定的套件版本
冲突、并且**与分类器结论一致**；不一致按冲突上报，不取平均、不二选一。记录摘要写入
`experiment_task_results.record_sha256`，账目因此能指回它用过的那份文件。

**记录来源现在可以追溯到控制端固定的、按尝试绑定的一份。** 可信控制端用
`python -m devflow_ai.experiment_record register-record` 为**某个候选的某一次尝试（一个 Run）**登记运行
记录的摘要（只存摘要，不复制文件）。登记表明确保存 `run_id`：一个实验内**一个 Run 至多固定一份记录**
（`uq_experiment_records_run`），**同一份字节也不能固定给两个 Run**（`uq_experiment_records_digest`）。
登记时从同一份被摘要的字节里取出 Run，并核验它属于该 Task；重放只在实验、Run、候选、Task 与摘要全部
一致时才是幂等，任何一处不同都是冲突，不静默覆盖。

账目读记录时**按 Run 判断**（不是按候选或 Task 聚合，因此同一 Task 的两次尝试互不影响）：该 Run 有登记且
候选/Task/摘要一致 → `control_plane_registered`；该 Run 有登记但摘要不同 → 冲突（不是被固定的那份）；
该摘要被固定在别的 Run 上 → 冲突（跨引用）；完全没有登记 → `caller_supplied`。晋升条件因此增加第七项：
可晋升的正式通过必须来自**已登记**的记录。这证明的是"字节就是控制端为这次尝试固定的那份"，**不**证明
作者、也不替代分类器对结论的独立得出。

**"先入账、后登记"被明确拒绝。** 结果行在写入时冻结了来源；事后登记无法改写它，改写就等于改写证据。
因此对已经入账的尝试再登记会得到可操作的错误（先固定记录再入账，或让这次结果永久保持不可晋升），
不提供静默升级路径。

**没有记录就只能算声明。** 调用方仍可在没有记录时给出 `deadline_context`（`fired` / `not_fired` /
`unknown`）与 `context_source`，但这条路径记为 `context_evidence = 'declared'`，**永远不进可晋升集合**；
`unknown` 以"未超时"传给分类器（唯一安全取值）并同样排除在晋升之外——分类器先看截止时间再看正式链，
一次超时优先于一次正式通过。`finalization` 只在调用方仍持有原结果时传入，未传只表示"未提供"。被停止
的尝试通常让 Run 停在 `RUNNING`：这种 Run 只接受"已触发截止"的上下文（分类器据此给出 `timeout`），
其余一律拒绝，不会给未结束的 Run 记判定。服务不声称还原了原运行的完整上下文；套件版本仍由调用方的
pin 提供，只是会被记录与分类器双向对照。

**归属靠约束，不靠先查再写。** 同一实验内一个 Run 只能记给一个候选：
`uq_experiment_task_results_run` 是数据库唯一约束，服务在插入冲突时报告"该 Run 已记给某候选"，
而不是静默再记一次。每次尝试各占一行，重复尝试不会被丢弃；但晋升阈值数的是**有 classified 正式通过、
截止上下文已知、来自运行记录、记录标明该次尝试是 `formal`，并且 Run 自身记录了与候选相同执行摘要的不同
声明 Task 数**，不是通过行数，所以把同一个 Task 反复跑通不会制造任务覆盖，`rehearsal` 的记录即使链能
读回也不进样本，Run 未记录执行版本时同样不进。

**声明的任务集连目标基线一起冻结。** `open` 现在要求每个声明 Task 都存在并已固定 `base_commit`（未准入的
Task 会被拒绝），把该 commit 写进 `experiment_tasks.base_commit`；`summary` 的 `task_baselines` 给出这张表。
之后若某个 Task 的 `base_commit` 被改动，用该 Task 记录结果会按冲突拒绝，用同一身份重放 `open` 也会被拒
（声明已变，不是重放）。这样"所有候选都测在同一目标基线上"是账目能自己回答的问题，而不必去查可能已经变化
的 Task 行。它与执行版本、与结果行里冻结的 checkout commit 是三件不同的事。

**同一实验最多一个"可能已发送"的未结算请求。** 这套架构分不清"正常在途"与"进程崩溃后未结算"，因此
两者按同一条规则处理：只要存在一个 `dispatched` 且未结算的请求，`reserve` 就拒绝新的预留，`mark_dispatched`
也拒绝新的发送——**两个请求先各自预留、再竞争发送**的窗口因此被堵住（在实验行锁下判定）。请求在这个窗口里
被拒绝时，外层预留可以释放（可证明没离开进程），而已经取得的 Run 预算预留保留为保守占用，不回退、也不绕过
Run 预算。等它被可靠结算后，后续请求才能继续；被标为 `unknown` 则整轮停止（未知用量规则）。进程崩溃只会
留下这种"一个未结算"的状态，不会留下可继续花费的空档。

**请求阶段与"只在确认未发送时释放"。** 预留现在带 `stage`：`reserved` 表示可以证明请求没离开本进程，
`dispatched`（带 `dispatched_at`）表示**可能已到达提供方**。网关在取到内层预留之后、真正调用之前把它标为
`dispatched`；此后账目**拒绝 release**，只能 `settle` 或 `settle_unknown`。因此进程崩溃、结算失败、
unknown 标记失败都只会留下"可能已发送、未结算"的预留：它仍占用额度、阻止后续超出剩余额度的请求，并在
`summary.usage.dispatched_unsettled` 与 `accounting_gaps`（"may have been sent and are not settled"）里
被单列。跨进程并发由实验行锁串行化：dispatch 与 release 竞争时，release 只有在 dispatch 之前才可能成功。

**它是保守预留，不是硬性上限，也无法事后叫停已在途的请求。** 预留按"输入估算 + 最大输出"取值；提供方实际用量可能超过预留，甚至在
并发在途请求下让**实际总用量**超过实验上限。账目在结算时如实记录（超限即置 `over_budget`，并从此拒绝
新的预留），但真正不可逾越的上限只能来自提供方侧的配额/限流；本实现不声称能防止这种超出，只保证超出会
被记录并停止后续花费。输入是估算值，这一点无法用本仓库消除。

**归属要核验，不是记账。** 预留所带的 `run_id`/`agent_execution_id` 在写入前会核对：Run 必须属于该实验
声明的 Task、AgentExecution 必须属于该 Run；不符即拒绝。它们仍只是"这次请求由哪次尝试发出"的记录，不是
执行版本证明（版本由执行清单与 Run 摘要单独约束）。

**模型请求的两层预算（显式启用）。** `devflow_ai.experiment_budget.ExperimentBudgetHook` 把实验总预算挂到
模型网关上：请求发出前先取实验级预留，再取原有的 Run 或准入预留；任一层拒绝都不会调用模型。拿不到调用
机会时把外层预留释放并记录理由；一旦尝试过调用（抛错或供应商没有返回用量），网关尝试将外层预留标为 `unknown`；
标记成功后阻止后续预留，标记失败时保留 `dispatched` 未结算并阻止后续实验请求。两层是**两次独立事务**：进程在两者之间崩溃会让额度被占住
（不会超支），调用后未结算同样保持占住并让账目显示不完整。每个逻辑请求生成一个新的 `request_key`，不存在
用同一预留调用两次的路径，也不自动重试外部调用。

覆盖范围要逐项说清：**已覆盖**（当调用方显式把 hook 传给网关时）是经由 `ModelGateway`/`PlannerModelGateway`
发出的 Run 内请求，以及 `AdmissionModelGateway` 的准入请求这条代码路径；`model_gateways_from_env` 现在也接受
显式可选的 `experiment_budget`，把这**一个** hook 交给全部五个角色网关，五角色的请求因此记在同一实验账目上
（不传时行为与 V1 完全一致，也不会写任何实验行；传入形状不符的 hook 会在组装时报配置错误）。**未覆盖**：
候选生成（尚无代码）、独立评测（在容器内跑，不调用模型）、任何不经过网关的模型调用；而且**默认没有任何
入口接线**——`worker_run`、`worker_scheduler`、`control` 与 `benchmark_run` 构造网关时都不传这个 hook，
因此不能声称真实实验总预算已被保证。阶段驱动 `orchestration.drive_run_stages` 本身接受调用方给的网关对象：
把上面这套显式装配传进去，Supervisor / Explorer / Planner（以及在能记录可信基线的平台上，Coder 与
语义 Verifier）的每次模型请求都会先经实验账目、再经该 Run 的预算；但默认入口没有一个这样传。另一个结构性限制：
`AdmissionModelGateway` 只在 Task 为 `PENDING` 时可用，而实验 `open` 要求声明 Task 已有固定基线（即已准入），
所以准入调用在当前规则下无法归属到某个实验；这条路径保留但暂不可达，需要先决定"实验是否允许声明未准入
Task"。hook 由构造网关的人传入，系统无法证明调用方一定传了它。

**费用未知不等于零。** 预留结束有三种方式：结算（给出实际用量）、释放（调用方声明未产生用量）、
未知（用量无法量化）。释放和未知都必须记录谁、为什么；未知不会把金额记成 0，也不回填预算，
实验的 `usage.accounting_complete` 因此为 false 并列出缺口。存在未结算或未知用量的实验不能记录
`accepted`。

**晋升仍然禁用。** 服务会依次检查七项：未超预算、账目完整、每个声明 Task 都有结论、截止上下文已知且
来自运行记录的 classified 正式通过的不同 Task 数达到阈值、失败的不同 Task 数不超过上限、**每个可晋升
正式通过都带着与声明一致的目标基线**、可晋升的正式通过均有已登记记录（缺失=未知、不一致=矛盾）。即使全部通过，
`accepted` 也会被拒绝，因为候选执行版本仍只是声明、Run 与候选的归属仍是调用方声明而非已核验事实。
`rejected`/`abandoned` 不校验阈值但必须写理由；决定会阻止候选再接收新结果或新预留。没有任何代码把
候选应用到 V0、Task 或后续运行。

**这一版明确没有做的**：没有把账目接入 `worker_run`、`worker_scheduler`、`benchmark_run` 或任何
Agent 路径，因此不会自动开始任何 RSI 循环；`benchmark_run.classify_outcome` 的结论要由可信调用方
显式调用 `record_classified_result` 才会进入账目，没有自动写入；模型 token 数量由可信调用方在预留与
结算时**声明**，服务只校验合计上限、记录声明值，不自行测量用量；不计量算力、墙钟执行时间或人工时间，
`deadline_at` 只拦新开销，不终止正在运行的工作；没有候选补丁、执行清单文件的存储与绑定，候选只带
一个由调用方给出的摘要（`summary` 把它标为 `declared_by_caller`）；没有 CLI。按 SPEC §10.8，这些都属
后续接线与隔离工作，本版不声称已经具备。

阶段驱动另有 `tests/test_experiment_stage_budget.py` 的 6 项：用实际装配出来的五角色网关驱动
`drive_run_stages`，断言每次模型请求对应恰好一条提交结算的实验预留、且归属到记录用量的那次
AgentExecution（未接 hook 的网关会表现为"调用多、预留少"），Run 侧合计与实验结算一致并仍受原
`max_llm_tokens_per_run` 约束；另外覆盖额度不足时第一段就不发送、Run 的 `max_llm_tokens_per_run` 仍先于提供方调用生效、
未知用量与实验层结算失败后后续阶段不再发送（预留保持 unknown / dispatched 未结算）、以及不传 hook 时
同样的阶段流程不产生任何实验行。
**如实说明：Linux 上可信 pytest 报告无法捕获，基线 Verifier 以 `BASELINE_UNAVAILABLE` 结束，驱动在
Coder 之前停下，所以本机构造流程实际路由到的是 Supervisor / Explorer / Planner 三个角色；Coder 与
语义 Verifier 未被路由，只有在能记录可信基线的平台上才会出现。这条测试按平台分别断言，不用五网关直调
冒充阶段驱动。**

装配层另有 `tests/test_experiment_budget.py` 的 9 项：五角色共享同一个 hook（不传时五个网关都没有实验层、
形状不符的 hook 被拒绝）、五角色请求进入同一账目并逐条归属到各自 AgentExecution、额度在第三个角色处被拒
且不发送、任一角色用量未知后其余角色全部被拒、实验层结算失败后请求保持 dispatched 未结算并阻止后续、
两个角色竞争唯一 dispatch 时只有一方送达且**落败方的 Run 预留保守占用**（外层预留释放、Run 侧
`actual_usage` 仍为 None）、assembly 不能绕过 Run 执行权（另一个 worker 的网关被拒且外层预留退回）、
以及 Run Policy 预算仍先于模型调用生效。全部是模型桩与构造数据，测的是装配与记账顺序，不是提供方用量。

已验证（Linux，隔离 schema 与嵌入式 PostgreSQL，模型未调用）：`tests/test_experiment.py` 71 项，
覆盖限额不可改与重放、候选上限与谱系、共享预算与预留幂等、并发不超卖、结算越界标记、期限与新开销、
释放/未知用量的声明与幂等、同一 Run 不可记给两个候选（服务、直接插入、两线程并发三条路径）、
分类器结论的写入与读不回来时的 `incomplete`、上下文来源的三种情形（运行记录给出已知截止、旧记录读作
未知、调用方声明且永不晋升）、记录与分类器结论冲突、记录指向别的 Run/Task、记录套件版本与 pin 冲突、
调用方与记录矛盾、同一尝试换一份记录重放被拒、`rehearsal` 记录只记证据不进样本、
被停止而仍为 RUNNING 的 Run 只接受"已触发截止"、
`finalization` 只传递不虚构、声明通过不进入阈值、重复尝试不制造任务覆盖、晋升阈值与禁用边界、
关闭后仍可结算，以及模型与迁移的列/可空性一致性；迁移的 upgrade→downgrade→upgrade 由
`tests/test_memory_migration_chain.py` 在独立 schema 上用真实 Alembic 执行，迁移测试还直接用 SQL 验证
数据库约束（同一 Run 唯一归属、释放必须带声明、分类行必须带截止上下文、record 证据必须带摘要与
尝试类型）。
记录侧另有 `tests/test_benchmark_record.py` 的上下文往返/缺省/校验，`tests/test_worker_scheduler_record.py`
核对调度入口写下的上下文不是 unknown，`tests/test_record_backfill.py` 核对补写明确写 unknown。
上述全部是**构造夹具**：
Run、Verification、草稿与回执由测试写入，只证明记账、分类器接线与约束行为，不构成任何任务通过、
候选更优或实验已完成的证据；分类器本身读的是测试写出的文件。

## 分类参数的可恢复性与执行版本绑定缺口（第四批更新）

分类器需要的四项输入，按目前持久化的事实逐项核对：

- **套件版本**：调用方给的 pin 与 Verification 记录里的 `suite_version` 会被比较，不一致时分类器给出
  `incomplete`（`suite_version_mismatch`）；账目另外还会把 pin 与运行记录里 `independent_evaluation`
  的套件版本对照，不一致按冲突拒绝。pin 本身仍来自调用方，不是从运行现场恢复的。
- **工件**：草稿、diff、独立回执通过 Artifact 行与 `--artifacts-root` 下的文件读回；缺失或字节不符时
  分类器返回 `incomplete`，不会降级成通过。
- **截止时间**：**已可恢复——前提是这次尝试写过运行记录**。`BenchmarkRunRecord` 的 `attempt_context`
  由执行入口在写记录时填入它亲眼看到的 `fired`/`not_fired`；账目读取该记录并据此分类，摘要一并落库。
  `runs` 表本身仍没有截止时间列，因此没有记录（旧 Run、未指定记录路径、或补写重建）时只能是
  `unknown`，绝不写成"没有超时"。补写入口明确写入 `unknown`。
- **finalization**：同样由 `attempt_context` 记录"是否发生"与步骤结果（`recorded`/`not_recorded`/
  `unknown`）。分类器需要的仍是原字典，而字典本身并未持久化：账目只记录发生与否，不据此重建字典，
  也不把"未提供字典"当成"没有评测"。

执行版本现在有了可验证的最小基础，但只在"本进程核验过的字节"这一层：

1. `devflow_ai.execution_binding` 在领取**之前**按控制端固定的清单核验实际工件：清单摘要必须等于独立
   提供的 pin，所列文件必须存在且字节一致，否则拒绝启动（不创建 Run）。核验默认根目录是**本进程实际
   导入的 `devflow_ai` 包目录**，不能由命令行指定，指向别的目录不构成对运行代码的证明。入口还要求清单
   **覆盖该目录下的全部 Python 模块**：核验时本地枚举 `*.py`（跳过 `__pycache__`），只要有一个未列出就
   拒绝——未列出的模块仍然可能被导入，只核对列出的文件不足以描述"跑的是哪个程序"。
2. `runs.execution_sha256` 由入口在领取事务内写入（`RunCoordinator.start_initial_run`），因此 Run
   不可能带着"稍后补写"的版本存在；未提供清单时写 NULL，表示未知，不回填历史。运行记录也在
   `attempt_context.execution_sha256` 里写下同一次尝试的摘要（补写路径从 Run 行复制，deadline 与
   finalization 仍为未知）；账目读记录时会把它与 Run 记录的值对照，不一致按冲突拒绝——一份从别的版本
   抄来的记录无法冒充本次尝试。
3. 同一 Task 的后续尝试（重试、再诊断）必须沿用已固定的摘要：历史全部未知时允许从现在开始固定；历史
   已固定而本次未提供或提供不同摘要一律拒绝（`RunVersionConflictError`），不会静默换版本。恢复驱动
   （`resume_implementation_recovery` / `resume_rediagnosis_recovery`）也接受同一对参数：提供则先核验
   再创建重试/再诊断 Run；**不提供就没有权限继续已固定的 Task**（拒绝而不是静默沿用），调用方目前
   只有 API 能传这对参数。
4. 账目把候选摘要与 Run 记录的摘要对照：不一致拒绝记录（`ExperimentConflictError`），Run 未记录摘要
   则照实记录但不进入可晋升集合。写入时把该摘要**冻结**在结果行（`run_execution_sha256`），晋升规则
   读的是这一行，而不是事后去查 Run：即使有人后来改了 `runs.execution_sha256`，账目仍保留当时看到的
   版本，不会把一次尝试改标到别的候选。`summary` 给出 `candidate_execution_binding = per_candidate`、
   每个候选的 `verified_runs`/`no_verified_run`，以及每条结果冻结的 `run_execution_sha256`。

**目标仓库基线已纳入晋升必要条件。** 账目在写入结果时读取该 Run 的 `git_checkout` ToolCall
（`execute_worker_git_checkout` 只有在 HEAD 与 Task 固定的 commit 一致时才记成功），把验证过的 commit 冻结进
结果行的 `workspace_base_commit`；没有这条记录时记 NULL（未知），与 Task 固定值不一致时按冲突拒绝。`summary`
给出每个候选的 `workspace_baseline`（`recorded`/`partial`/`unverified`）与计数，`explain-acceptance` 的
`observed` 里也列出计数。它与执行版本是两件事：前者是"任务在哪个目标基线上跑"，后者是"跑的哪份 DevFlow"。
该证据现在也进入晋升门槛：可晋升的正式通过必须带着与声明一致的基线，缺失（未知）或不一致（矛盾）都会
让 `decide(accepted)` 与 `explain-acceptance` 给出具体原因，而结果本身仍如实保留。

仍然没有、也不声称有的：**进程自证**（写入摘要的进程可以撒谎；本实现只让诚实入口在核验失败时拒绝
启动）、清单**不覆盖未列文件**（模块发现与导入不在保证范围内）、**工作区快照**（Agent 之后编辑的目标
仓库 checkout 由 `Task.base_commit` 固定，与本清单无关）、以及**部署期替换**（导入之后再替换磁盘文件
超出进程内检查的能力）。因此候选与 Run 的归属关系现在是"由本进程核验过的执行版本相同"，而不是
"由可信第三方证明了这次执行"；`accepted` 仍然禁用。

下一步按实验协议补齐可信记录来源、实际执行工件与 Run 的可靠关联、工作区及回退验证，再评估是否接入
受控评测和实验记录导出。现有摘要与操作者提供的记录不足以证明实际执行或独立来源；真实重复评测仍需
另行确定范围、预算与证据，在核实 §10.8 门槛前不启动自动循环。

**受控录入口（不新增执行路径）。** `python -m devflow_ai.experiment_record record-result` 让可信操作者
把一次**已结束**尝试按共享分类器记入账目：它不创建 Run、不调用模型、不评测，只在账目里写一行（或拒绝，
退出码 2）；结论只能来自 `classify_outcome`，`--record` 指向运行记录时用记录里的上下文并对其字节计摘要，
没有记录时必须显式给 `--deadline-context`，否则拒绝。`summary` 子命令只读地打印账目的限额、用量、逐候选
结果与它们背后的上下文/版本证据；`explain-acceptance` 则逐条列出该候选**哪些必要条件成立、哪些不成立**
（预算、账目完整、声明 Task 覆盖、不同 Task 的正式通过数、失败数、可晋升通过的目标基线、
可晋升通过是否来自控制端已登记的记录），并固定返回 `acceptable: false` 与
`blocked_by`（进程自证、记录作者未验证、归属仍是声明）。三个命令都不写决定，也都不会让 `accepted` 变为
可用；诊断与 `decide` 共用同一份条件实现，不会出现"诊断说可以、决定却说不行"。

**仍未闭合的上下文限制**：`attempt_context` 由写入方自己填写，记录本身不证明它来自可信进程；记录摘要
只固定字节（登记也不能证明作者）；套件 pin 仍由调用方给出；finalization
字典未持久化，因此非正式结论仍可能与当时进程内的判定不同；旧 Run 与补写记录保持 `unknown`，不回填。

执行版本绑定由 `tests/test_execution_binding.py`（6 项：清单/pin/文件字节/格式错误/CLI 输出）、
`tests/test_coordinator_integration.py`（61 项，含领取事务写入摘要、未知摘要写 NULL、非法摘要拒绝、
数据库 CHECK、后续尝试沿用或拒绝的参数化规则）、`tests/test_worker_run.py`（36 项）与
`tests/test_benchmark_run.py`（24 项，入口在领取前核验、把摘要写进 Run 并报告）验证；
`tests/test_experiment.py`（42 项）验证账目的归属对照与可晋升规则。上述仍全部是构造产物与隔离数据库，
没有真实模型或真实评测。
