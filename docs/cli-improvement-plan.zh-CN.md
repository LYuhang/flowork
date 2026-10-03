# Flowork CLI 六项优化清单

本清单记录 CLI 优化的进度与后续讨论范围。第 1、2 项已经完成并合入 main；第 3 项已实现并通过真实服务验收；第 4～6 项按用户提供的原始建议记录，暂不实施。后续实现前应重新核对代码现状，不能把历史问题描述视为最新代码审计结论。

## 进度

| 序号 | 优化项 | 状态 |
| --- | --- | --- |
| 1 | 统一 CLI 结果输出与按需进度输出 | 已完成，已合入 main |
| 2 | 梳理 Task 和 Deployment 的资源与执行查询层级 | 已完成，已合入 main |
| 3 | Deployment 异步执行后的完整结果获取 | 已完成，已部署并通过实机验收 |
| 4 | Skill 和 Workflow 版本冲突保护 | 已记录，待讨论、未实现 |
| 5 | Workflow 多步修改的原子保存 | 已记录，待讨论、未实现 |
| 6 | 评估配置、参数命名与列表筛选的易用性 | 已记录，待讨论、未实现 |

## 第 3 项 异步执行后的完整结果获取

已新增 `flowork-cli deployment result --deployment_id ID --execution_id ID`，从持久化执行结果读取完整 EndNode 输出，适用于同步与异步调用。外部 API 和 Webhook 保持原有查询能力。

info、status、history、logs 分别负责资源配置、单次运行状态、历史列表和单次执行日志。result 是只读查询，不提交新执行、不等待执行完成；run 返回后会提示使用 result 回查。

返回约定：

- 执行中或等待审批：result_available=false、outputs=null、result_unavailable_reason=execution_pending。
- 成功且存在完整结果：result_available=true，outputs 为完整业务输出，包括合法的空对象 {}。
- 失败、超时或取消：保留终态和 execution_error，outputs=null，原因是 execution_unsuccessful。
- 成功但完整历史结果缺失：result_available=false、outputs=null，原因是 result_not_recorded；不以 result_summary 代替完整结果。
- 查询成功与业务执行成功分开：查询正常时 command_status=succeeded，调用方仍须检查 execution_status 和 result_available。

服务端校验当前用户的查看执行权限、调用所属 Deployment，以及持久化轨迹的来源。仅返回最终输出，不返回内部节点输出或完整工作流。CLI 帮助和 Agent 指引同步更新。

验证记录：96 项相关自动化检查通过；真实服务完成审批等待时查询、自动超时后以原执行 ID 回查完整嵌套输出、同步调用回查、失败结果回查，以及查询不新增执行的核对。验收资源已清理。

## 第 4 项 Skill 和 Workflow 版本冲突保护

原始问题描述：Knowledge 已增加 --expected_version，但 Skill 发布尚未让调用方指定预期版本，workflow upload 允许覆盖并发修改，没有预期版本检查。原建议使用 skill publish 表述发布动作；实施前需核对当前 Skill CLI 的实际命令名称，不在本清单中新增或改名命令。

建议流程：

1. 下载资源时获取并记录版本。
2. 提交修改时携带预期版本。
3. 如果服务端版本已经变化，拒绝覆盖，提示 Agent 重新读取并合并修改。

目标是保护用户和 Agent 同时编辑的场景，避免后提交的内容静默覆盖其他人的更新。预期版本参数是否必填、版本标识及兼容策略仍待讨论。

## 第 5 项 Workflow 多步修改的原子保存

原始问题描述：workflow operation 连续执行多个修改时，如果中途失败，会将前面成功的修改保存为新版本。例如“添加节点成功 → 修改配置成功 → 连线失败”，最终仍保存前两步。Agent 容易将命令失败理解为没有修改，重试后造成重复操作。

建议默认整组操作成功才保存；任一步失败，整组修改不写入，不留下成功的前半段，也不创建部分修改版本。

这里要求的是每个编辑操作本身成功，不要求编辑中的 Workflow 已经完整可执行。仍然允许保存尚未完成的草稿，不能将执行前的完整性校验强加到每次编辑保存。

## 第 6 项 次要易用性调整

### 评估配置

原始问题描述：task evaluation-config 必须传评估脚本，单独开关自动评估也需要重新提供脚本。

建议允许只修改 --auto-evaluate，未提供脚本时保留已有脚本。没有已有脚本时如何启用自动评估，以及空参数提交的行为，留待设计确认。

### 参数命名

原始问题描述：同类输入文件参数存在 --input-file、--input_file、--inputs_file，Agent 容易混用。

建议统一推荐写法，保留旧参数兼容，并同步调整帮助说明和示例。具体统一名称、适用命令及别名冲突处理待讨论。

### 列表筛选

原始问题描述：Skill、Knowledge、MCP 使用 --search，Task 使用 --query，Workflow 和 Deployment 的筛选能力较少。

建议统一名称、描述筛选参数及帮助说明。筛选范围是资源元信息；文件内容检索仍通过下载后的 bash 命令完成，不增加文件内容搜索命令。具体参数名称及兼容策略待讨论。

## 后续验收原则

各项方案确认后再实现。同步检查 CLI 参数、服务端处理、帮助文档与 Agent 使用说明，避免只改一个入口。按已有要求部署到真实服务验收；验证 Agent 能根据业务目标自主发现合适命令和参数，不能只以命令提交成功作为完成标准。
