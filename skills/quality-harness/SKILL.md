---
name: quality-harness
description: 在已采用 Codex Quality Harness 的任务中定义架构与验收、执行当前阶段检查并做代码审查。用户点名 Quality Harness 或要求按其门禁推进时使用；普通独立开发保持原有工作流。
---

# Quality Harness

## 固定约束

贯穿方案、开发和同一轮 CR：

1. 先列改动范围；新增文件、模块或依赖说明必要性，检查无关改动和过度抽象。
2. 搜索已有实现、调用方及公共能力；优先复用或小幅扩展，新实现说明具体缺口。
3. 默认复用表、字段和关系；确需改模型时说明复用不足、兼容、迁移和恢复限制。
4. 查询、并发、队列、缓存、外部调用或资源消耗变化，报告变化、证据、验证缺口、风险和恢复限制；高风险缺关键证据不能无条件通过。
5. 开工前明确架构边界、行为、验收用例和 Rubrics；按同一版本验证，降低或删除要求须用户确认。

保留用户已要求的完整交付范围和权限边界。

## Ponytail 接入

已按固定版本保存官方指引，按阶段读取，不启动额外模型评审：

- 开发前读 [ponytail](references/ponytail/SKILL.md)，使用 **full**：查调用链、复用现成能力、选择最小且完整的改动。每个任务读一次即可。
- CR 时读 [ponytail-review](references/ponytail-review/SKILL.md)，并入既有独立 CR：正确性、安全、实际负载、测试、性能和必要性一起核对。

本任务的固定约束、验收契约和权限优先。上游的 session 范围限于当前 Harness 工作；其输出模板适配现有中文 CR，不另写第二份报告。完整能力不因“最小改动”缩水；验证、数据保护、错误处理和必要的性能证据继续保留。Ponytail 的宣传指标不作为本任务实测结果。

只使用已审阅的文本指引，不启用生命周期 Hook、自动更新、额外权限或全局模式。子任务由 Controller 传入当前阶段需要的指引，继续使用受限配置；不得为加载 Skill 恢复未筛选的 Plugins、MCP 或 Hooks。

来源：[DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail/tree/09c434ab73ca43761c5d8b0c5d341926c2fba710)，固定提交 `09c434ab73ca43761c5d8b0c5d341926c2fba710`。保留 [MIT 许可证](references/PONYTAIL-LICENSE)，上游原文保持不变。

## 执行与如实交付

先解析本 Skill 的实际目录；上两级是工具仓库。读取该仓库 README 的当前架构、验收版本和实现状态。按实际命令帮助执行，不为未实现能力拼装临时执行器。

现有六个稳定命令：`doctor`、`prepare`、`run`、`gate`、`status`、`resume`，参数以 `qh <command> --help` 为准。命令未安装时，在工具仓库使用 `PYTHONPATH=src python3.10 -m codex_quality_harness ...`。真实 Codex Loop 消耗模型用量；CI 只跑确定性验证，不保存个人模型凭据。

先按 README 的版本化契约声明架构边界、可写业务路径、验收与关键负例，再用 `prepare` 创建 worktree。`run` 使用受限 Builder、独立只读 Reviewer 和现有测试；`gate` 核对当前证据。`status` 查询、成本统计与备份不唤醒模型。不得编辑状态库或手填证据来放行。

默认不发布；用户已授权交付时用 `run --publish` 提交、推送并创建 Draft MR 和中文 CR，随后在当前 Codex 任务 attach 该 MR。提交前核对 author、committer 为 `严骏 <yanjun@fuzhi.ai>` 且 `user.useConfigOnly=true`。不从交付授权推断合并、部署或数据库写入。

`run --operation` 只执行契约中已有、经过审阅的 Adapter。批准记录必须来自当前用户对具体范围的明确授权；不能用 actor 字段、需求人的原话或模型自己的建议伪造批准。已授权的 TEST 指令可登记相同范围而不重复询问；SQL/目标变更及生产动作保留各自批准边界。缺现成 Adapter 时先复用专项 Skill 和已有 CLI，不能临时拼脚本后自行批准。

结果不明时先 `status`，显式 `resume --reconcile` 做只读对账，不盲目重发或反向迁移。已有活跃进程不能被接管；无法确认的项保持待核对。降低或更改既有验收要求须用户确认后登记精确版本的批准。

用户已明确将工具开发与 Backend 集成验收分开；Backend 原失败单测、覆盖率不足和报告问题继续阻断其门禁，不能靠放宽条件绕过。进展按“已实现、真实链路已验收、待批准、未验证”报告，引用具体证据。隔离仓库成功不代表 Backend、泳道或生产验收通过。

Jira 修复、数据库检查和泳道回归复用现有对应 Skill，在已授权范围内串接；不得重复建客户端或用本 Skill 扩大数据库写入、TEST、合并、生产发布及对外发信权限。
