# Codex Quality Harness

完整目标为本机 Codex 开发与独立 CR、确定性 CI、TEST 和生产只读证据闭环。用户于 2026-10-27 明确先继续 Harness 开发：工具能力与 Backend 接入分别验收。运行时隔离失败仍阻断自动 Loop；Backend 的失败测试、覆盖率和报告缺陷继续阻断该仓库的阶段门禁。TEST、数据库写入、合并与生产发布保留各自批准边界。

## 执行闭环契约（版本 3，CR 后补强）

新增 `harness.py` 保存本机运行状态、证据和开发 Loop，`adapters.py` 承担 Git 与外部事实核验；复用 `doctor.py` 的唯一受限 Codex 启动路径、进程预算和 XML 检查。CLI 保持六个命令。新增 `test_harness.py` 验证负例，不另造测试框架。无新增运行依赖、后台服务或云组件。

SQLite 的运行、证据、操作及批准记录是唯一运行状态来源，原始产物按摘要存储。M0 没有状态表，诊断 JSON 不能提供事务、操作去重和恢复，因而需要这份仅本机使用的状态模型。业务仓库及 Backend 数据模型不变。目录锁与操作记录防止两个 Controller 争用同一验收 worktree。Builder 每轮使用新的独立临时 worktree；Controller 捕获并核对授权路径的代码后才导入验收目录。即使后代进程脱离进程组，它也只能写旧目录，不能改动后续验收代码。结果不明先对账，不盲目重发。

实施前 Rubrics：

1. 契约必须声明目标、架构边界、复用决策、模型与性能影响、验收要求及测试命令；字段缺失不能开工。
2. 准备独立 worktree；默认保护全部测试、Fixture、Mock、测试选择配置及 `.git`，仅显式开发测试目录可写。有效契约存在 Controller 私有目录，Builder 不能修改。
3. 关键 Contract/Independent Eval 必须有已知错误实现的负向命令：正确实现通过、负例失败才有有效证据。空报告、仅跳过、缺必需 Job、旧 CR 不能放行。
4. 每个证据绑定内容指纹、契约、命令/规则与必要环境身份；代码、依赖、规则或目标提交变化后只复用仍匹配的证据。模型手填 PASS 不生成 CI 或部署证据。
5. Builder、Reviewer、测试均走受限进程。复用真实权限探针；失败即 BLOCKED，不放宽权限。Reviewer 每次独立上下文且只读。
6. 默认最多 3 轮代码修复、30 分钟；同一失败两轮无改善即阻塞。失败的首次验证保留；同一版本不能通过原样重跑洗掉失败。
7. 独立 CR 在必要测试通过后执行；审查修复后核对最终指纹。取消、超时、预算耗尽、进程消失与外部结果不明分别记录。
8. Draft MR 与 code_ready 分开；Backend code_ready 保留单测 86%、集成 30% 和实际 MR CI 身份要求。TEST/生产批准绑定环境、提交、脚本和目标，不能互用。
9. 所有命令输出 JSON：运行、阶段、结果、阻塞、证据引用及下一步。状态、取消和恢复不要求模型参与。
10. 工具负例通过后，在隔离仓库完成真实 Codex 修复、独立 CR 和续接；该证据不替代 Backend、真实泳道或生产验收。

工具性能边界：默认单 worktree 一个写入者，只读模型并发最多 2；SQLite 短事务，不在事务内等待模型或网络。每次源码指纹扫描仅 Git 跟踪文件及未忽略文件，有文件数、单文件和总量限制；运行环境按实际文件内容核验，包含包代码、`.pth` 和字节码；总计最多 10 万文件、2 GiB、单文件 128 MiB，越界阻塞。已核对本机独立 Python 的一次内容核验约 0.6 秒；大 venv 尚未实测。Builder 临时源码每轮保留一份供诊断，最多受修复轮数与源码预算约束，不自动删除不明进程使用的目录。原始输出和子进程时长复用 M0 限额。无生产查询和后台持续观测，实际工具耗时和模型用量写入运行报告，不能据此声称线上性能改善。

## 固定约束

方案、开发和 CR 都执行 [AGENTS.md](AGENTS.md) 的五条约束：最小必要改动、优先复用、默认复用数据模型、评估线上性能、先定义架构和验收再开工。降低或删除验收要求须经用户确认。

## 本轮架构与改动范围

- `doctor.py`：M0 诊断流程，复用 `codex sandbox`、`codex exec` 和已认证的 `glab api`；不新建模型服务或 GitLab 客户端。
- `cli.py` / `__main__.py`：六个稳定命令与 JSON 入口；`harness.py` 保存状态、契约、证据并执行受限 Loop；`adapters.py` 复用 Git/`glab`、已注册流程脚本和只读观测网关。
- `tests/test_m0.py` / `tests/test_harness.py`：确定性负例，不依赖模型或生产资源。`.gitlab-ci.yml` 在 CI 独立执行，不保存模型凭据。
- `pyproject.toml`：Python 3.10 包及命令入口，无运行时第三方依赖。
- `AGENTS.md` / 本文：固定约束、架构边界和验收标准，不保存运行进度。
- `skills/quality-harness/`：薄入口及用户指定的 Ponytail 原文引用，固定上游提交并保留许可证。开发和 CR 按阶段加载，不安装 Hook 或增加模型评审轮次。

上述文件分别承担执行、安装入口、负例与固定规则。仅新增本机 Controller 状态表，没有业务数据模型、后台服务或共享平台组件。运行证据保存在用户私有目录，不提交 Git。

## M0 验收契约（版本 1）

| 检查 | 通过标准 |
|---|---|
| 非交互认证与结构化输出 | 使用现有 Codex 认证，真实回合完成并输出符合 JSON Schema 的结果 |
| Builder | 授权业务文件可写；工作区外文件、模拟凭据和 Controller 状态不可读写；契约不可改、删或通过目录重命名绕过 |
| Reviewer | 独立上下文，继承 `:read-only`，同样运行权限探针且不能修改业务文件 |
| 网络与环境 | 有监听进程的 TCP/Unix socket 连接被权限拒绝，不能将连接拒绝或文件不存在算为权限生效；工具子进程不继承模拟生产变量 |
| 工具配置 | 忽略用户配置和 exec rules，禁用 Apps、Plugins、Hooks、子 Agent、浏览器及 Web 搜索；不因失败放宽权限 |
| Backend CI 报告 | 必需 Job 名称固定为 `unit_test`、`integration_test`；读取独立 JUnit、覆盖率 XML、Artifact 和实际 Pipeline/Job 身份 |
| 不完整证据 | 缺失、手动、跳过、运行中或模糊 Job 不能通过；JSON 摘要和模型 PASS 不替代 XML 报告 |

Rubrics 使用硬门槛，不以平均分掩盖失败：所有必要能力通过，M0 才通过。离线夹具只验程序行为，真实 Codex 和 Backend 读取分别验收。M0 不是 Code ready、TEST accepted 或生产验收。

## 运行

命令默认输出 JSON：

```bash
qh prepare --repo /path/to/repo --task TASK-1 --contract /private/contract.json
qh run --run QH-... --until code_ready
qh status --run QH-...
qh gate --run QH-... --stage code_ready
qh resume --run QH-...
```

`run --publish` 才由 Controller 提交、推送并创建 Draft MR 和中文 CR 评论；默认不发布。需先配置已有 remote、目标分支和 GitLab project ID。提交前 author、committer 均须是 `严骏 <yanjun@fuzhi.ai>`，`user.useConfigOnly=true`。不修改项目审批设置，不合并，不部署。

契约最小示例（测试入口和被测代码必须已经存在）：

```json
{
  "version": 1,
  "goal": "修复整数求和，保留现有接口",
  "architecture": {
    "scope": ["calc.py"],
    "reuse": "修复现有 add 函数",
    "data_model": "无业务模型变化",
    "performance": "常数次整数运算，无网络或数据库"
  },
  "risk": "low",
  "rubric": [{"id": "sum", "expected": "正负整数求和；减法错误实现必须失败", "critical": true}],
  "tests": [{
    "id": "sum", "kind": "contract",
    "argv": ["python3.10", "tests/check.py"],
    "negative_argv": ["python3.10", "tests/check.py", "--known-bad"],
    "inputs": ["tests/check.py"]
  }],
  "budget": {"rounds": 3, "seconds": 1800},
  "base_ref": "HEAD"
}
```

`scope` 是可写业务路径；默认其余源码只读，测试、Fixture、Mock、CI、依赖及选择配置受保护。开发测试可通过 `developer_test_paths` 单独授权，但不得覆盖有效 Contract/Independent 输入。需要第三方依赖时，显式配置 `runtime_read_paths` 指向已准备的 Python 3.10 venv；不能把 HOME、凭据或 Controller 目录当运行时开放。测试输出放专用目录：argv 使用 `{output}/junit.xml`，契约 `junit` 为 `junit.xml`；覆盖率同理。`COVERAGE_FILE` 和 pytest cache 已定向到专用目录。工作树内的忽略产物不能作为可复现源码。

同一任务的契约修订保留旧版本。删除/更改原测试、业务目标或原 Rubric、降低风险要求，或修改 CI、部署/回归验收及生产策略，需要另给 `prepare --approval <file>`，错误 JSON 会返回待批准的精确 scope。增加检查可保留原要求；只有测试描述、规则和完整源码均相同的 Eval 才复用，不复用旧 CR。无法证明影响范围时保守失效，不猜依赖图。

## 外部动作、观测与归档

契约 `operations` 可注册现有仓库流程脚本。每项声明 `kind`、`environment`、`target`、已跟踪的 `script`、结构化 `argv`、只读 `verify_argv` 和 `checks`。不提供未核验平台的默认脚本，也不另建服务客户端。只在用户明确要求后执行：

```bash
qh status --run QH-...  # 读取 operation_scopes
qh run --run QH-... --operation deploy-test --approval /private/approved-scope.json
qh run --run QH-... --operation deploy-test --reconcile  # 只回读，绝不重复写入
```

批准 JSON 是 status 给出的完整 scope 加 `approved_by: "yanjun@fuzhi.ai"`、`expires_at`（未来一天内的 Unix 秒数）。它是本机授权审计记录，不是人类意图的密码学证明或组织 IAM。Agent 只有在当前用户已明确授权精确范围时才能登记；不能根据文件名、actor 字段或一条需求人的原话自行创造权限。TEST 指令可记录已授权的个人配置与部署；数据库 SQL/目标变化、生产就绪、合并与生产发布保留各自单独批准。工具不会执行生产发布或合并。

读回 JSON 需有实际 `source_sha`、`environment`、`target` 和 `captured_at`。`checks` 是有界条件数组，如 `{"path":"route_hit","op":"eq","value":true}`，支持 eq/ne/lte/gte，不执行表达式或接受模型 PASS。必要事实缺失不放行：

- `test_deployment`：code_ready 通过，核对实际镜像摘要、配置 selector、lane ID 和路由命中。检测到模型、SQL、迁移路径或声明的启动迁移时，先要求单独批准和核验数据库执行。部署须显式列出 `database_operations`，逐项核对所需数据库目标、环境、脚本及提交的成功证据；其他数据库的成功不能放行。
- `test_regression`：预先声明 `required_scenarios`，全部业务、故障、超时和恢复场景通过；保留 `screenshots` 和 `requests`。性能前后值只有 metric/unit/method/environment/target/request_digest 相同且样本数有效时才计算差值与百分比，否则明确缺基线。
- `test_report`：复用现有飞书归档脚本；必须回读 `document_url`、`text_saved` 和 `image_refs_verified`，不擅自改分享或消息同事。
- `production_observation`：仅调用本机现有 `atoms-obs-prod` 允许的只读叶子命令，要求完整、已审计的 prod 信封及指定版本字段；不接受 setup/login、任意脚本或伪装同名程序。缺生产版本映射即阻塞。
- `performance/capacity/risk/recovery`：复用现有专项验证，按事先确认的事实条件核验，不用健康检查代替故障恢复。

production_ready 另用 `run --approve-stage production_ready --approval ...` 登记具体目标及版本的批准；它只允许就绪判断，不等于发布授权。数据库写入必须是单独的 `database_write` 项，绑定所审核脚本、目标和提交。外部脚本是受信任的 Controller Adapter，必须随对应改动一起审查；不允许运行仓库里未经确认的任意脚本。`verify_argv` 必须只读，不能用它隐藏第二次写入。平台事实、脚本和权限未验证时，只能说接口已实现，不能说线上验收通过。

## 恢复与成本

`run --cancel` 请求终止子进程并保留证据；`status` 查询生命周期、验证有效性、待核对操作和按操作统计的耗时/Token。没有价格或历史交付数据时，费用与效率改善返回未知，不预设改善比例。没有持续监听。

`resume` 遇到不明操作先返回待核对；显式 `resume --reconcile` 才按现有 worktree、提交、远端分支、MR、CR 或注册的只读接口对账。活着的 PID 不会被租约到期替换；查不到 PID 的不明 Worker 不被假定为失败。只读 Worker 或使用独立临时源码的 Builder，主进程结束后可以记录“结果未知、验收源码已捕获”，随后重新验证，不伪造原回合 PASS。不具备该隔离证明的旧 Worker 保持待核对；通用 Adapter 不凭 PID 消失确认完成。任一外部操作不明时，先阻止该运行的新外部动作，完成精确只读对账。无法核对的操作继续列出，不自动反向迁移。

`status --backup /private/new.sqlite` 用 SQLite backup 创建一致快照，不覆盖旧文件。回退停用新入口，保留状态、操作记录和内容摘要产物；可切回旧提交重装工具。状态 schema 目前为 1，未知版本拒绝打开。独立隔离仓库不冒充 Backend 或真实环境验收。

使用已安装的独立 Python 3.10：

```bash
PYTHONPATH=src python3.10 -m unittest discover -s tests
PYTHONPATH=src python3.10 -m codex_quality_harness doctor \
  --repo /path/to/repo --live-codex \
  --gitlab-project 1678 --pipeline <pipeline-id>
```

`--runtime` 可指定 Python 3.10 安装根目录。默认使用当前解释器的安装根目录并解析符号链接；系统 Xcode Python 启动器不适合此白名单探针。`--live-codex` 消耗实际模型用量；省略时相应检查标为阻塞，不会误报全通过。

已启动的指定流水线可用 `--wait-for-ci-seconds` 等待，范围 0–5400 秒、默认不等待。每 30 秒只读一次状态，结束后再取报告，避免等待 CI 时重复调用模型或下载已有 Artifact；不会创建、重试或取消 Job。

所有命令输出版本化 JSON。退出码：0 通过、1 失败、2 阻塞、3 待核对（后续外部副作用恢复使用，M0 不产生此结果）。原始证据写入 `~/.local/state/codex-quality-harness/<run-id>/`，目录仅当前用户可访问。

## CI 报告导出兼容设计（实施前契约）

已核对的缺口：Backend 的 `.mise.toml` 生成 XML，统一 CI 已声明 Artifact 路径，但 AppExec 测试 Pod 的文件没有返回 Runner。现有 AppExec 只提供状态、日志和取消接口；覆盖率摘要来自日志。已有成功 Job 没有 XML，GitLab test report summary 的测试数量为零。

优先复用方案：使用 AppExec 已透传的短期 `CI_JOB_TOKEN` 和已启用的本项目 Generic Package API，将本次 Job 的两个指定 XML 文件传回 Runner，再复用现有 Artifact 收集。无需新增 AppExec API、数据库、存储服务或个人凭据。

最小兼容改动限定于 Backend 的一个报告传输脚本、对应负例和 `.gitlab-ci.yml` 的命令包装及报告声明；业务测试、覆盖率阈值及部署路径不改。若现有公共实现能够直接满足该协议，改为复用，不保留第二套实现。

实施前必须满足的验收用例：

1. 正常和失败测试都保留报告；缺报告、无效 XML、摘要代替 XML 均不通过。
2. 传输绑定项目、Pipeline、Job、源 SHA、测试类型及各文件摘要；串 Job、串提交和文件篡改均拒绝。
3. 只允许既定 XML 文件，限制单文件、总包大小和网络时间；拒绝目录穿越、重复成员和解压膨胀。
4. 凭据通过受支持的短期 Job Token 认证，不写入报告、包或命令参数，不向其他主机或重定向转发。
5. 上传响应丢失后只读取同一对象核对，不盲目重复上传；无法核对则阻塞。
6. 在专用分支跑真实已有测试并从 GitLab 回读 XML；夹具成功不能代替真实通过。

性能影响报告（实施前）：新增每 Job 两份报告的有限网络和存储开销；不增加业务查询或生产并发，不执行部署。现有报告大小、传输耗时和实际 Token 权限仍待实测。传输限额或认证失败必须阻塞验收；回退恢复原 CI 命令及报告包装，保留已产生的证据，不自动删除远端包或修改共享清理策略。包的保留与存储预算须在启用到日常 CI 前明确。

## 验收边界

本机执行、CLI、状态、门禁及 Adapter 接口可分别实现与验收；Backend 实际 CI、真实泳道和生产观测仍要求各自真实证据。Backend 的已失败单测、82.21% 覆盖率与 JUnit subTest 汇总问题不因工具开发而转为通过。原单测 86%、集成 30% 门槛保持。没有 Ultimate 等平台强制能力证据时，只能声明本机可审计门禁和独立 CI，不能宣称 MR 无法绕过。
