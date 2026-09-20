# Diagram

`/diagram` 使用运行在 Chat 沙盒内的 draw.io 官方 MCP 创建和调整图表。最终产物
是原生 draw.io 文件，而不是 Flowork 私有图表格式，因此同一套能力可以覆盖流程图、
UML、ER 图、BPMN、架构图、网络图、思维导图、时间线、线框图、工程图以及自由画布。

## 工作方式

1. Agent 使用官方 MCP 生成 draw.io XML、搜索官方图形库、管理页面并应用 draw.io
   连线路由。
2. Flowork 只增加一层轻量文件适配：检查内容，并将结果原子写入
   `/data/diagrams/<名称>.drawio`。
3. Agent 使用沙盒内的 draw.io Desktop 官方 CLI 将当前源文件导出为 PNG，查看真实
   像素，并在必要时修正明显的视觉问题。Flowork 的轻量启动器只负责在沙盒内提供
   一次性的无头显示环境，实际渲染仍由 draw.io 完成。
4. 验收后的文件复用平台原有的 Sandbox→VFS 持久化流程，并作为预览卡片发布到对话中。
5. Preview 将同一份文件交给 diagrams.net 官方引擎渲染，再把返回的 SVG 展示在
   Flowork 原生平移缩放画布中；Agent 和用户可以检查实际画面，并在需要时继续调整
   源文件。

`.drawio` 是唯一可编辑的事实来源。Flowork 不再维护第二套语义模型、Diagram
Revision 表、操作日志、自研渲染器或按图类型划分的编译器。

## 预览

对话卡片会展示适应容器后的整体视图，并支持拖拽平移、滚轮缩放和 Fit View。需要
查看细节时，可以打开完整的只读 Preview 画布；需要调整时，Agent 会在沙盒中修改
原生文件。多页面文件仍然保持原生格式，Agent 可以通过官方 `list_pages`、
`get_page` 和 `set_page` 工具读取或修改页面。

发布前，Flowork 会执行有边界的 XML 安全与结构检查，包括 XML 格式、危险声明、重复
Cell ID 和悬空连接端点。图表是否美观、清晰，仍以真实渲染结果为准，而不是仅凭 XML
有效性判断。Agent 的反馈图在沙盒内部生成，不要求用户先打开 Preview。

## 导出

Preview 会直接下载原生源文件；SVG 和可编辑 PNG 通过 diagrams.net 官方 embed 协议
渲染。PDF 和 JPG 则在浏览器内基于这份官方 PNG 编码，无需额外部署 draw.io export
server，同时保持一致的视觉结果。

| 格式 | 适用场景 |
| --- | --- |
| `.drawio` | 在 diagrams.net 或 draw.io Desktop 中无损编辑 |
| SVG | 可缩放文档和设计工具交接 |
| PNG | 对话、演示文稿和通用图片场景 |
| PDF | 文档、打印和正式交付 |
| JPG | 不需要透明背景时的紧凑位图交付 |

在官方格式支持的情况下，SVG 和 PNG 会保留 draw.io 源数据，可重新在 draw.io 中
打开。后续 Agent 修改始终以 `.drawio` 文件为准。
