# DevFlow AI 实施进度

更新：2026-10-27（两个模型集成测试通过，本地 Git 已初始化）。显式加载 .env.test 并核对实际连接 devflow_test，结果为 2 passed in 0.46s、退出码 0；测试前后 tasks 行数均为 0。已验证省略 status 得到 PENDING，以及 title=None 导致 IntegrityError 且报告违规列为 title。普通测试最近结果为同日 9 passed、2 skipped、1 warning，发生于恢复数据库前；源码未再变动，无需重复运行。Git 已初始化、忽略规则已核对，尚未暂存或提交；下一步只学习暂存区。以本地文件和有日期的实际结果为准；采纳设计、讲解、理解通过、实现、测试与真实任务完成分别记录。

| 范围 | 当前状态 | 证据或缺口 |
|---|---|---|
| 0.1 至 0.5 设计 | 已写入 SPEC；交接记录称理解检查已完成 | `SPEC.md` 第 1 至 5 节；没有本地运行证据 |
| 0.6 Policy、0.7 架构与实施路线 | 既有设计已合入 SPEC，不从旧 0.5 接续 | `SPEC.md` 第 6 至 9 节；阈值尚未由评测校准 |
| Docker 基础概念 | 交接记录称已预习；数据库启动与数据卷已实际验证 | 仍需在后续练习中熟悉 Compose 的日常操作 |
| SQLAlchemy、API/worker 基础概念 | API、Engine/lifespan 和 Task ORM 已有实践 | worker 尚未实现 |
| 数据库访问依赖 | 已安装并完成最小查询 | SQLAlchemy 2.0.54、Psycopg 3.3.6；通过项目函数执行 `SELECT 1` 返回 1，退出码 0 |
| DevFlow AI 业务代码、迁移、测试 | 迁移往返、真实 INSERT/RETURNING 的 status 默认值、title NOT NULL 失败归因及测试回滚已验证 | `test_models_integration.py` 为 2 passed，测试前后任务行数均 0；其余默认值和约束未逐项验证，无 Session/Repository 或 Task 业务路由 |
| Git 工作区 | 2026-10-27 学生已初始化本地仓库，尚无提交 | `git status --short --branch` 为 `No commits yet on master`；26 个未跟踪文件，暂存区为空；已确认本地配置、虚拟环境和 IDE/测试缓存被忽略 |
| Python、uv、Git | 阶段 2 已固定项目解释器 | `.python-version` 为 3.11.15，历史 `uv run` 输出 3.11.15；全局 Python 3.9.1 为早期环境记录，本次未重查 |
| 后端基础依赖与入口 | 存活接口已实现并验证 | `pyproject.toml` 与 `uv.lock` 固定 FastAPI、Uvicorn、pytest、httpx；`GET /health/live` 经 TestClient 实际请求返回 HTTP 200 和 `{"status":"ok"}` |
| Docker/Compose、PostgreSQL | 2026-10-27 用户恢复环境后，真实连接与两个集成测试通过；本轮未重查容器健康字段 | `postgres:17.11`，映射 5434:5432；`devflow_test` 任务行数在测试前后均为 0；最近迁移版本核验为 `41ff63fe9d8a`。开发库结构记录来自 2026-10-24，本次未重查开发库 schema |
| WSL | 早期环境检查可用 | 当时 `wsl --status` 退出码 0；本次未重查 |
| Windows | 早期记录为 Windows 11 专业版，64 位；约 31.8 GB 内存 | Docker Desktop 使用当前用户安装位置；本次未重查系统状态 |
| 主机端口 | 配置为 `localhost:5434` | 2026-10-08 容器恢复后通过该映射完成实际数据库就绪请求 |
| 数据卷持久化 | 已验证并清理探针 | 命名卷 `05_devflow-ai_devflow_pgdata`；写入临时行后执行 `docker compose down` 和 `up -d db`，重建容器后查询到 1 行，已删除临时表 |

**当前接续点**：两个模型集成测试已通过，不再重教或重跑。学生正确选择跟踪 .env.example、忽略 .env；已纠正理由：实际配置会影响排障，忽略主要因为凭据及本机差异。学生已运行 git init；只读核验当前分支 master、尚无提交，26 个未跟踪文件。git check-ignore 确认 .env、.env.test、.venv、.idea、.pytest_cache 被忽略。下一小步只讲暂存区及 git add 的作用，让学生执行暂存后核对清单；尚未执行 add/commit，不能称已建立可恢复版本基线。之后再推进提交和 Session/Repository、Task 持久化与查询。

## 升级路线状态（2026-10-25）

以下内容已纳入 `SPEC.md` §9.1、§10 和 `DECISIONS.md`，不表示学生已逐项完成推导，也不表示已有相应代码或验收结果。

| 范围 | 设计与实现状态 | 关键位置 / 后续门槛 |
|---|---|---|
| Worker、Repository、RunCoordinator、LangGraph、五个 Agent | 既有设计；均未实现 | `SPEC.md` §5、§7、§8、§9；先完成 Task 持久化与查询 |
| Harness、Policy 执行、结构化输出 | 已设计；无运行组件 | `SPEC.md` §5.4、§6、§7、§10.3；真实工具调用前完成权限、参数、路径、预算、超时及审计 |
| 动态 ContextBuilder、Agentic Search | 已采纳基础与增强路线；未实现 | `SPEC.md` §10.4；基础上下文与首个真实 Agent 同步落地 |
| Verifier、失败恢复、Benchmark / EvaluationRunner | 既有设计加独立评测规则；未实现、无真实任务结果 | `SPEC.md` §3、§4、§10.5；固定版本与任务集，不能以替身测试证明修复成功 |
| 日志诊断 | 请求级 JSON 日志和 distinct-ID 单测已有；业务关联和模型成本未实现 | `src/devflow_ai/api.py`、`tests/test_logging.py`；Task/Run/Agent/Tool/实验关联见 `SPEC.md` §10.5 |
| MemoryService、冲突/时效管理、跨任务召回、Skills | V2 采纳方向；无代码、表或记忆评测 | `SPEC.md` §10.6、§10.7；先有真实轨迹，保留来源与范围，摄入不改变正式业务结果 |
| EvolutionRunner / 受控 RSI | 采纳实验路线；无候选、执行版本机制或实验结果 | `SPEC.md` §10.8；E 的真实闭环与独立评测达标后先做 ContextBuilder 小实验，无需等完整记忆系统 |

实施工作包顺序为 **A → B → C → D → E → 首个 H 实验 → F / G → 扩展 H**。A 至 E 完成执行与评测基础；F/G 是长期记忆及 Skills；H 是受控 RSI。阶段映射及门槛见 `SPEC.md` §9.1。现阶段不换框架、不添加模型/向量/图数据库依赖、不创建未来表。

## 最近核验与历史记录

2026-10-27（本地 Git 初始化已核验）：学生提供 Initialized empty Git repository 输出。只读 git status 确认为 master 分支、No commits yet，git ls-files --others --exclude-standard 列出 26 个项目文件，当前均未跟踪；git check-ignore 确认五类本地配置和目录被忽略。未执行暂存或提交，未修改源码或重跑测试。下一步仅教学暂存区及选择本次提交内容。

2026-10-27（环境恢复，模型集成测试通过）：再次读取测试源码确认无额外改动。显式加载 .env.test，先核对 URL 及 SELECT current_database() 均为 devflow_test，再运行 tests/test_models_integration.py，得到 2 passed in 0.46s、退出码 0；tasks 行数在测试前后均为 0。已验证默认状态、title SQL NULL 被拒绝及具体违规列为 title，不代表其余默认值、约束、正式业务创建或 Session 持久化已完成。未重跑此前已绿的普通测试。只读 git status 仍报 not a git repository；已核对根目录与 .gitignore，未初始化、暂存、提交、修改学生代码或开发库。接下来仅进行 Git 基线前的纳入范围理解检查。

2026-10-27（title 断言已修正，数据库环境阻塞）：已读取学生源码，error_column = exc_info.value.orig.diag.column_name 及 assert 位于 pytest.raises 结束后、内层 try 内；学生回答报告 repo 时测试失败，归因理解通过。显式加载 .env.test 后，真实连接前置检查在 localhost:5434 超时，未执行 pytest 或任何 INSERT；Engine finally 清理。只读 docker compose ps 报 Docker Desktop Linux 引擎命名管道不存在，尚不能读取容器状态。独立清除五项数据库环境变量运行普通全量测试，得到 9 passed、2 skipped、1 warning in 1.06s，退出码 0；两个集成测试按预期跳过，原 Starlette/httpx 弃用警告仍在。尚未验证新增 NOT NULL 反例或本次测试前后行数，未修改学生代码或数据库。

2026-10-26（title 反例资源框架修正通过）：已读取新保存的函数，库名保护、连接与事务、raises 位置、rollback/dispose 均符合既定框架；AST 输出 SYNTAX_OK、退出码 0。只做源码和语法核对，未运行 pytest 或数据库。已从本地 Psycopg 源码确认 diag/column_name 诊断属性，下一步让学生读取具体违规列并 assert 为 title，再实际验证该完整反例。

2026-10-26（局部连接已补，执行位置仍错误）：读取新反例后确认 engine/create/with/finally 已存在，AST 输出 SYNTAX_OK；但 with pytest.raises 及 connection.execute 位于 with 连接和 Engine dispose 之后。这里是资源生命周期错误，不是 connection 变量未定义；退出 with 后变量名仍可见，Connection 已关闭。另缺少与首个测试一致的测试库入口条件及显式事务回滚框架。只给缩进与复用框架提示，未改测试源码或运行 pytest，不声称真实 NOT NULL 已验证。

2026-10-26（反例已移文件，缺局部连接且存在语法错误）：已读取两个测试文件，新增反例与 IntegrityError 导入现在位于 test_models_integration.py，test_database.py 恢复原配置测试。新反例已构造 title=None 的 statement，并将 connection.execute(statement) 放进 raises，但 connection 未在本函数定义；开头独立 try 没有 except/finally，AST 报 expected 'except' or 'finally' block（实际行号见本轮输出）。旧默认值函数本体仍正确，但整文件当前不能收集；未执行 pytest 或数据库。只针对函数局部作用域及已有资源框架给予提示，不代改代码。

2026-10-26（发现反例草稿位置与执行对象错误）：用户请求检查并询问执行插入写法。先读 test_models_integration.py 未见新函数，进一步搜索 tests/src 后，在 test_database.py 找到 test_task_reject_null_title：title=None 是未使用局部变量，raises 内仍调用 builder_database_url()。该函数仅构造连接 URL，不使用 title、更不执行 SQL，不能触发数据库 NOT NULL。AI 给出执行调用的局部示例，并要求沿用实际集成测试的 connection/statement/事务框架与文件位置；没有代写或运行草稿，不记录为数据库反例失败或通过。

2026-10-26（title 反例输入确认，准备预期异常测试）：学生选择 None、预期抛异常的判断正确；最后“可以为空置”的表述已精确为“NOT NULL 允许空字符串”。源码仍只有已通过的 status 测试，未写 title 反例。下一步复用现有资源框架新增函数 test_task_rejects_null_title，并用 pytest.raises(IntegrityError) 捕获执行阶段的数据库约束异常，变量名 exc_info；介绍这是数据库完整性异常，预期异常未发生会让测试失败。当前未修改测试文件、执行数据库或重跑测试，不给完整实现。

2026-10-26（assert 理解确认，进入约束反例设计）：学生澄清先前看错，并正确复述断言检查状态是否 PENDING，不是就失败，理解检查通过。仅静态核对现有测试仍使用正确断言，以及 Task.title 的 nullable=False 与 SPEC 说明“NOT NULL 不拒绝空字符串”。本轮没有重跑测试或修改测试源码；当前只提出 title=None / 空字符串的输入选择，尚未实现或验证 title 约束反例。

2026-10-26（首条数据库集成测试通过，assert 理解待确认）：学生已写出 result.scalar_one() 与 `assert actual_status == TaskStatus.PENDING`，保存源码正确。首次数据库前置连接超时，尚未进入 pytest；用户确认当时未启动数据库并随后启动。只读检查显示 db running/healthy、PostgreSQL 接受连接；恢复后显式加载 `.env.test`、核对实际库 devflow_test，运行指定测试得到 **1 passed in 0.45s，退出码 0**，测试前后 tasks 行数均 0，确认成功路径插入、默认状态、返回读取与回滚。清除子进程五项数据库环境变量后普通全量收集得到 **9 passed、1 skipped、1 warning in 0.87s，退出码 0**，新集成测试按预期跳过；Starlette/httpx 原有弃用警告未处理。首次沙箱内普通测试卡在 TestClient 的 socketpair，30 秒超时终止；沙箱外复核成功，未修改源码解决环境限制。另有不接触数据库的内存探针输出 RUNNING_EQUALS_PENDING=False 并捕获 AssertionError，纠正学生“会通过”的回答；理解仍待其重新复述。此次未改用户测试、业务代码、表结构或开发库，未声称 Session 持久化、约束失败分支或业务链路完成。

2026-10-26（RETURNING 与执行调用已保存）：已读取学生的测试文件，values 后正确接 returning(Task.status)，下一行在同一内层 try 中调用 connection.execute(statement) 并保存 result；原回滚与 Engine 清理保留。当前只是源码核对，没有运行函数或数据库操作，不能将“执行调用已写好”混同于测试已经执行。下一步让学生用 result.scalar_one() 读取唯一一行的第一列，并与 TaskStatus.PENDING 断言比较，尚未添加读取、断言或正式测试结果。

2026-10-26（values 输入源码核对通过）：已读取学生保存的语句，包含 title、description、repo、policy_version、policy_snapshot 五项，status 未显式提供，Policy 夹具标记为 db_test_only / test_only。AST 检查输出 SYNTAX_OK、五字段列表及 STATUS_EXPLICITLY_PROVIDED=False，退出码 0；未连接数据库或执行 pytest。下一步指导学生配置 RETURNING status 并通过当前 connection 执行，返回值保存到 result；尚未写入这些调用，也未验证结果读取或断言。

2026-10-26（语句构造理解通过，准备 values）：学生正确说明当前只有语句对象、没有数据且未执行。源码复核仍为 statement = insert(Task)。下一步讲 `.values()` 把列与值加入待执行语句，先给 title 示例，其余由学生补写；为避免编造正式 Policy，明确本次仅用带 db-test-only/test_only 标记的、最终回滚的数据库测试夹具，既不声称是完整策略，也不用于业务创建服务。当前 Task 表仅对这两列施加类型与非空约束；正式 Task 仍必须保存 SPEC 的完整预算和权限快照。未修改测试文件、未执行数据库或 pytest。

2026-10-26（插入语句对象已保存，解释职责）：已读取学生保存的测试文件，新增 insert/Task 导入，内层 try 为 statement = insert(Task)，位置正确。用户询问作用，当前说明其只构造针对映射表的 INSERT 对象并保存在 Python 变量中，不会创建数据库记录；尚未配置 values、执行或断言。不标记理解已通过，也未运行 pytest 或数据库操作。

2026-10-26（必需字段确认与 Policy 字段回顾）：学生正确解释 id 非空但能省略，因为 PostgreSQL 生成默认 UUID；policy_version 和 policy_snapshot 作为其余必需输入已找对。应用户要求回顾版本标识与固定配置内容的区别，依据 SPEC §2、§6.3；不新增阈值、快照结构或请求字段。当前只准备学习 insert(Task) 的语句构造，尚未写数据、执行插入或运行测试。

2026-10-26（事务与清理框架核对）：已读取学生保存的测试文件，connection.begin() 位于连接上下文内，变量 transaction；内层 try 暂为 pass、finally rollback，外层 finally dispose，层级正确。项目虚拟环境 Python 的 AST 语法检查输出 SYNTAX_OK、退出码 0。此次只证明代码结构与语法，没有执行 pytest、连接数据库或插入数据，不代表 status 默认值已经验证。下一步让学生对照 Task 模型确定插入的其余必需字段。

2026-10-26（Engine 赋值修正通过）：已读取保存后的测试文件，赋值已改为 `engine = create_db_engine()`，学生说明库名字符串与函数返回 Engine 对象的区别，源码核对正确；没有执行 pytest 或数据库操作。下一步讲解 connection.begin() 返回本次事务的管理对象，用变量 transaction 接收，并让学生添加确保回滚的内层 try/finally。事务代码尚未编写，不能标记默认值测试或测试数据隔离已实际运行通过。

2026-10-26（连接生命周期源码核查）：学生已保存 create_db_engine 导入、try 内的连接上下文与 pass 占位、finally 中 dispose，结构位置正确；实际赋值却是 engine = database，把环境库名字符串当成 Engine。AI 指出类型与函数调用问题，等待学生自行修正一行。未执行该错误函数、真实数据库操作或 pytest；没有新的集成测试通过结果。

2026-10-26（入口比较理解通过，准备连接生命周期）：通过字符串内容与对象身份的解释，学生正确回答库名内容为 devflow_test 时 != 条件为 False，不进入 skip。当前源文件仍仅有环境入口判断，尚无数据库资源代码。接下来复用学生已在数据库检查和迁移中写过的 Engine/Connection、with、try/finally 清理方式；只指导该小步，不代写完整测试、不调用真实数据库或新增测试结果。

2026-10-26（集成测试入口已保存）：已读取新建的 `tests/test_models_integration.py`，包含 os/pytest 导入、正确测试函数名、环境库名读取以及 `database != "devflow_test"` 时 skip 的判断。学生使用变量名 database 合法，后续按实际代码衔接。此前 is not 比较对象身份而非字符串值的问题已纠正，待学生简短解释以确认理解。函数尚无数据库连接、事务、插入及断言，因此本轮仅检查源码，没有运行 pytest 或数据库操作，也未代写测试代码。

2026-10-26（事务范围理解通过，开始集成测试入口）：学生起初认为事务回滚会连表删除；在区分迁移降级与当前数据事务后，学生正确解释表在事务之前已建立所以保留，AI 补充“已提交”的准确边界。现有测试目录仍只有四个正式测试文件，未发现模型集成测试。下一步只指导新建 `test_models_integration.py` 并写 `devflow_test` 环境条件与 `pytest.skip()`；跳过表示当前环境未运行该集成测试，不是测试失败或默认值验证通过。未创建该文件、未执行插入或新测试。

2026-10-26（status 默认值测试输入）：学生回答测试时不直接填写 PENDING，以免给定输入遮蔽默认值行为，选择省略 status，核心判断正确。AI 精确化理由：显式提供 PENDING 时不需要数据库补值，不能证明默认声明生效，也不意味着记录状态不可修改。当前进入下一小步“事务提交/回滚及测试数据清理”的概念讲解与核对；未创建测试文件、未插入记录、未重跑测试，不标记事务概念已经理解通过。

2026-10-26（再次升级与迁移往返通过）：学生再次提供 `Running upgrade -> 41ff63fe9d8a, create tasks table` 日志。AI 加载 `.env.test` 后只读核对实际库 devflow_test、版本 `41ff63fe9d8a`、tasks 九列、task_status 九值、任务行数 0，退出码 0。结合此前首次升级及降级检查，首份迁移的真实升级—降级—再升级已通过。另读取到 id 的 `gen_random_uuid()`、status 的 `'PENDING'::task_status`、两个时间字段的 `now()` 默认声明；仅证明实际表定义，未用插入测试默认值、约束或 ORM 行为。下一步单独推导 status 默认值测试的输入方式。

2026-10-26（测试库真实降级）：学生提供 `Running downgrade 41ff63fe9d8a -> , create tasks table` 日志。AI 显式加载 `.env.test`，先确认 URL 的目标库，再通过现有 Engine 只读核验：实际库名 devflow_test、tasks=False、task_status=False、alembic_version=True、版本列表为空，退出码 0。版本表本身保留是正常现象，迁移说明文字 `create tasks table` 不改变实际执行 downgrade 的事实。本次未执行数据库写入或全量测试；下一步指导学生再次升级，完整升级—降级—再升级尚待最后一步。

2026-10-25（测试库首次真实升级）：学生提供 `Running upgrade -> 41ff63fe9d8a, create tasks table` 且命令返回终端的日志。AI 显式加载 `.env.test`，通过现有 Engine 只读核验：实际库名 `devflow_test`；`alembic_version` 为 `41ff63fe9d8a`；tasks 列为 id、title、description、repo、status、policy_version、policy_snapshot、created_at、updated_at；task_status 为九个预期状态；任务行数 0，退出码 0。此结果证明首次真实升级及上述结构，不代表约束/默认值、ORM 写入、降级/再升级或业务链路完成。当前先验证新增枚举清理的真实降级，再重新升级和验证数据行为；升级由学生执行，AI 未执行数据库写入。

2026-10-25（运行时连接目标核验）：学生报告运行库名检查正常。AI 使用 `uv run --env-file .env.test --locked --no-sync` 和现有数据库函数运行只读探针；先校验 URL 库名为 `devflow_test` 再连接，查询 `current_database()` 及目标对象是否存在，得到实际库名 `devflow_test`、tasks=False、task_status=False、alembic_version=False，退出码 0。连接与 Engine 已清理，未执行迁移、写入数据或输出凭据。下一步已给学生首次升级命令，尚待执行和核查。

2026-10-25（测试配置保存核验）：读取并比较配置，仅输出非敏感结果：开发库名 `devflow`、测试库名 `devflow_test`，测试五项必需值均非空，其余四项一致，`.env.test` 忽略规则存在；检查退出码 0。未输出用户名、主机或密码内容。本地 `uv run --help` 确认支持 `--env-file`。下一步由学生显式加载 `.env.test`，调用 `builder_database_url().database` 检查该命令实际采用的库名；尚未执行迁移。

2026-10-25（测试连接配置）：已向学生提示 `DEVFLOW_DB_NAME=devflow_test`，学生明确回答若仍为 `devflow`，迁移仍操作开发库，配置目标理解通过。静态检查 `.env` 存在、`.env.test` 不存在；AI 仅在 `.gitignore` 增加 `.env.test` 并更新进度/交接，指导学生自行复制和修改测试配置。未读取或输出本地凭据，尚未确认测试配置创建完成，也未执行迁移。

2026-10-25（独立测试库创建成功）：学生报告 `CREATE DATABASE devflow_test` 命令成功。只读通过现有 Docker `db` 服务、`devflow` 用户连接 `devflow_test`，查询当前库名和 public schema 表数，返回 `devflow_test`、0，退出码 0。只确认建库与初始可连接状态，没有执行建表、升级、降级或修改配置。下一步为测试连接配置的理解核对与独立配置准备。

2026-10-25（独立测试库准备）：学生已用自己的话解释“迁移脚本只描述表变化，不备份原记录，因此降级后再升级不会恢复任务数据”，理解检查通过。只读 Docker/psql 检查成功，容器运行且 healthy，角色与开发数据库均为 `devflow`，数据库列表为 `devflow`、`postgres`、`template0`、`template1`，未发现 `devflow_test`。首次沙箱访问 Docker pipe 被权限限制，沙箱外只读复核成功；未执行建库、迁移或修改 `.env`。当前只指导学生执行 `CREATE DATABASE devflow_test`，等待实际输出，不标记创建完成。

2026-10-25（学生补齐枚举清理）：已读取保存后的迁移文件，`downgrade()` 在删表后创建名为 `task_status` 的 PostgreSQL ENUM 描述对象，并通过 `drop(op.get_bind())` 删除类型；位置参数写法有效。AST 语法检查通过。在 PostgreSQL 方言的 Alembic 离线 MigrationContext 中分别调用升级、降级函数，升级生成 `CREATE TYPE task_status` 与 `CREATE TABLE tasks`，降级依次生成 `DROP TABLE tasks;`、`DROP TYPE task_status;`，探针退出码 0。此核验没有连接数据库、不验证真实约束或事务，也不代表实际迁移完成；尚未重跑全量测试。AI 只核查学生代码并更新进度/交接，未代写迁移。

2026-10-25（此前文档整合）：完整对照升级材料、四份项目文档、源码、测试、迁移与依赖文件。当时 `downgrade()` 仍只有 `op.drop_table('tasks')`，尚无枚举清理。静态确认目录仍不是 Git 仓库。该轮仅更新 SPEC、DECISIONS、PROGRESS、HANDOFF，不改业务代码、迁移、依赖或数据库，不新增测试结果。HANDOFF 已替换过时的 0.5 接续点，保留小步教学与用户编写核心代码的规则。

2026-10-24（最近实际运行核验）：全量测试 **9 passed，1 warning，0.93s，退出码 0**；命令为项目虚拟环境 Python 执行 `-B -m pytest -q -p no:cacheprovider -o faulthandler_timeout=15`。四个测试文件覆盖配置校验、健康接口、请求模型和请求日志；就绪接口使用替身检查函数，不代表真实 ORM 写入或修复链路已通过。另有 12 个 Python 文件语法检查通过。Starlette TestClient 使用 httpx 的弃用警告仍在，未为消除警告改依赖。

同日离线 SQL 核验：升级包含 `CREATE TYPE task_status` 和 `CREATE TABLE tasks`，降级只有删表，确认枚举清理缺口。Docker 容器运行且 healthy；只读查询确认 PostgreSQL 17.11、当前库仅有 `public.alembic_version` 且版本行数为 0，无 `tasks` 和 `task_status`。未执行升级、降级或真实 Task 插入。首次沙箱诊断遇到测试等待和 Docker 权限限制，获准后的核验成功；不把环境执行限制归因于业务代码。尚无真实任务成功率或 RSI 收益数据。

以下为按日期保留的学习和执行历史，旧条目中的“当前”仅指记录当日，不覆盖以上接续点。

2026-10-22（迁移候选生成）：学生已将 online 连接接入 `create_db_engine()`，在连接上下文内执行迁移，并用 `finally` 调用 Engine 的 `dispose()`；offline 使用 `builder_database_url()`。保存内容与 Python 语法检查通过。学生加载 `.env` 执行 `alembic current`，输出 PostgreSQL 实现且未显示已应用版本；随后执行 `revision --autogenerate`，检测到新增 `tasks` 表并生成 `41ff63fe9d8a_create_tasks_table.py`，文件已核对。建表部分与当前 Task 模型字段、主键和数据库默认值一致；候选降级目前仅删除表，未显式清理独立的 `task_status` 枚举类型。尚未运行 `upgrade` 或 `downgrade`，没有执行真实 ORM 插入，本次未重跑全量 pytest。

2026-10-22：学生已在 `migrations/env.py` 正确导入 `from devflow_ai.models import Base`，并设置 `target_metadata = Base.metadata`。保存内容核对和 Base 本地导入检查通过，元数据中已登记 `tasks` 表。本次没有连接数据库，也没有运行迁移生成或升级命令。现有 `builder_database_url()` 从五项 `DEVFLOW_DB_*` 环境变量构造 URL，后续需将迁移连接接入该项目配置。

2026-10-21：学生已将 Task.id 的默认函数名修正为 `func.gen_random_uuid()`。本地导入、主键和类型标注检查通过，离线 PostgreSQL 列定义为 `id UUID DEFAULT gen_random_uuid() NOT NULL`；没有执行真实数据库插入。随后学生安装 Alembic 1.20.0：`pyproject.toml` 精确声明、`uv.lock` 锁定版本，以及 `uv run --locked alembic --version` 输出均核验通过。学生执行 `alembic init migrations` 后，已核验 `alembic.ini`、`migrations/env.py`、`script.py.mako` 和说明文件生成；versions 目录尚无具体迁移脚本。学生已确认版本脚本存放在 versions 目录。当前 `env.py` 仍为 `target_metadata = None`，数据库连接尚未接入项目配置。

2026-10-20：学生选择由 PostgreSQL 在省略 id 时生成 UUID，以覆盖 SQLAlchemy 和原生 SQL 插入路径。已确认 PostgreSQL 17 提供 `gen_random_uuid()`，并将该规则写入 SPEC；尚未补写或验证 ORM 参数，尚未执行真实数据库生成验证。

2026-10-19：学生已定义共同的 `Base`、`TaskStatus` 九值枚举，以及 Task 的九个字段映射。已用本地导入、元数据检查和 PostgreSQL SQL 离线编译验证列类型、非空声明、`PENDING` 插入默认值、JSONB 的 Python `None` 转 SQL `NULL` 行为、两个时间字段的插入默认值及 SQLAlchemy 更新时附加 `updated_at=now()`。原生 SQL 更新不触发 `onupdate`，须显式维护更新时间。当前 `id` 尚无 Python 侧或数据库侧默认生成规则。尚未执行迁移，以上不代表真实 PostgreSQL 的建表、插入或更新时间验证；本次没有重跑全量 pytest，最近的全量结果仍为 2026-10-16 的 9 passed。

**已验证历史**：2026-10-12 学生在 FastAPI 中间件中为每次请求生成 `request.state.request_id`，构建 `event`、`request_id`、`status_code` 字段并调用 `logger.info(json.dumps(log_data))`。2026-10-13 配置 INFO 级别与 `StreamHandler` 后，实际两次 `/health/live` 测试请求均输出 JSON 日志。2026-10-16 正式 `test_http_requests_have_distinct_ids` 先按 logger 名称、再按 `http_request_completed` 事件筛选日志，比较两次日志的 `request_id`；单测 1 passed、全量 9 passed（1 条依赖库弃用警告）。此前相同 ID 的断言会失败已用独立小探针验证。2026-10-10 学生在 `schemas.py` 写出 `TaskCreateRequest(pydantic.BaseModel)`，包含 `title`、`description`、`repo` 三个字符串字段；正式测试覆盖合法输入与缺少 `repo` 的错误位置。五项必需数据库配置均经 `require_env()` 校验；2026-10-09 加载 `.env` 后，五项配置读取断言均通过（未输出密码）。`builder_database_url()`、`create_db_engine()` 和 `check_database_connection(engine)` 已完成；学生已在 `create_engine()` 配置 `connect_args={"connect_timeout": 5}`。FastAPI lifespan 创建并保存 Engine，正常退出时调用 `dispose()`；就绪接口将 `SQLAlchemyError` 转换为 503。2026-10-08 实际请求分别在数据库停机、运行时返回 503 和 200。`DEVFLOW_DB_NAME` 缺失测试曾因删除错误的环境变量产生假通过，已修正，并在加载与不加载 `.env` 时验证。

项目根目录已有 `.env.example`、`.gitignore`、本地 `.env` 和 `compose.yaml`。`.env` 密码未输出；早期验证时 Codex 进程的 PATH 无 Docker 安装目录，使用 Docker 绝对路径完成验证，未更改系统环境变量。

2026-10-07 连接练习：第一次查询因 Docker 服务未启动而等待，探针已停止。用户启动 Docker 后，容器检查为 `running/healthy`；通过学生的 `check_database_connection(engine)` 执行 `SELECT 1` 返回 1，退出码 0。第二次探针仅在测试进程设置 `PGCONNECT_TIMEOUT=5`；随后学生把连接超时写入项目 Engine，未设置该环境变量的探针再次返回 1，退出码 0。学生在 psql 查询 `current_database()` 和 `current_user`，均返回 `devflow`，并已确认 PostgreSQL 服务独立于 PyCharm。密码未输出。

**首个业务闭环**：Task 创建与查询、准入成为 `ELIGIBLE`、单 worker 原子领取并创建 Run1、查询关联记录。每次只给学生一项小实现练习；事务回滚、幂等和唯一约束使用真实 PostgreSQL 验证。
