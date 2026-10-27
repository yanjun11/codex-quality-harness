# Codex Quality Harness

当前处于 **M0 能力验收**。完整目标为本机 Codex 开发与独立 CR、确定性 CI、TEST 和生产只读证据闭环；未通过 M0 前不实现自动 Loop，也不发布为完整工具。TEST、数据库写入、合并与生产发布保留各自批准边界。

## 固定约束

方案、开发和 CR 都执行 [AGENTS.md](AGENTS.md) 的五条约束：最小必要改动、优先复用、默认复用数据模型、评估线上性能、先定义架构和验收再开工。降低或删除验收要求须经用户确认。

## 本轮架构与改动范围

- `doctor.py`：M0 诊断流程，复用 `codex sandbox`、`codex exec` 和已认证的 `glab api`；不新建模型服务或 GitLab 客户端。
- `cli.py` / `__main__.py`：提供 `qh doctor` 和模块入口；其余计划命令尚未实现，不提供空壳命令。
- `tests/test_m0.py`：确定性负例，不依赖模型或生产资源。
- `pyproject.toml`：Python 3.10 包及命令入口，无运行时第三方依赖。
- `AGENTS.md` / 本文：固定约束、架构边界和验收标准，不保存运行进度。
- `skills/quality-harness/`：薄入口及用户指定的 Ponytail 原文引用，固定上游提交并保留许可证。开发和 CR 按阶段加载，不安装 Hook 或增加模型评审轮次。

上述文件分别承担可执行诊断、安装入口、负例与固定规则。没有新增数据库模型、后台服务或共享平台组件。运行证据保存在用户私有目录，不提交 Git。

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

## 后续阶段

M0 通过后按已确认的完整计划推进 M1–M4：持久化及预算恢复、版本化契约与独立 CR、Backend CI 正反例、专项性能与 TEST、生产只读反馈和成本统计。每阶段报告分别标明已实现、真实验收、待批准及未验证，不用 M0 脚手架冒充最终交付。
