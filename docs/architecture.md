# FPGAFlow 架构与设计约束

## 职责边界

FPGAFlow 是 AMD Vivado/Vitis 工程通用的构建、验证和部署工具。核心只处理 profile、阶段依赖、工具版本、内容指纹、缓存、运行记录、日志和安全门。外部工程在自身仓库维护 RTL、HLS、PS 配置、板卡参数和验收规则。

provider 负责 Vivado/Vitis、XSCT、QSPI 和 UART 等通用操作。provider 不保存外部工程路径、ROM 名称、应用命令或项目协议。外部脚本通过 argv 命令执行器接入。

核心包不依赖任何具体工程的目录、应用、存储布局或板级验收语义。项目专属信息只能出现在外部 profile 和外部脚本中。

AMD Ross 文档搜索和 Vivado MCP 用于资料查询和交互调试。FPGAFlow Skill 只说明如何读取运行状态和调用 CLI，不维护第二份执行状态。

## Profile 与阶段调度

Schema version 1 的 profile 声明阶段类型、依赖、输入、产物、工具链、目标和验证规则。新字段保持可选，旧 profile 继续工作。

阶段指纹包含本阶段输入文件哈希、依赖产物哈希、argv、provider 参数、工具版本和目标身份。缓存命中前，Flow 会重新计算指纹并验证每个输出哈希。失败阶段不会覆盖上一次成功产物的引用；manifest 同时记录最新尝试和最后成功的 run_id。

DAG 默认串行。Profile 可设置 `max_parallel_stages`，并为阶段声明自定义资源锁。Flow 自动排斥相同的专用工具、目标和重叠输出。并行阶段必须先满足依赖；失败会阻止其下游阶段。已经启动的独立阶段会完成并留下记录。

同一输出目录的跨进程变更操作使用 OS 文件锁。默认遇到锁时立即报告所有者、阶段和 run_id。只有显式使用 `--wait-lock` 才会排队。只读查询不获取变更锁。

## 运行状态与 Agent 恢复

`manifest.json` 只保存阶段摘要、最近 run_id 和缓存所需的短索引。完整输入哈希、输出哈希、命令、工具版本和证据引用保存在不可变的 `runs/<run_id>/run.json`。旧版 manifest 在首次写入时原样归档，再原子迁移；历史 run 文件不会删除。

`resume` 汇总活动、失败和待运行阶段，并给出完整 argv 形式的下一步命令。它不启动工具链或操作硬件。失败运行先给出 `watch` 命令查看日志。

长命令通过流式 runner 执行。stdout/stderr 持续写入 run 目录；执行状态定期记录 PID、输出字节数和心跳。人类输出只显示阶段摘要；默认 JSON 同样只返回输入、输出和证据数量，并附带 run 记录引用。需要完整哈希映射时加 `--details`。`watch --run-id` 查看实时状态和日志尾部；长任务进度写到 stderr，JSON 错误与成功结果使用同一 stdout 格式。

## 预检、UART 与副作用

`doctor --stage` 和 `doctor --suite` 只检查相关工具链和只读 provider 预检。JTAG 与 QSPI 检查目标唯一性；UART 预检只枚举匹配设备，不打开端口、不发送命令。

UART profile 用 `expect_text` 和 `reject_text` 做字面匹配；现有 `expect`、`reject` 仍按正则解释。UART 通信或提示符错误会立即终止后续步骤。内容断言失败只有在 profile 明确启用时才继续收集诊断。

Profile 可在 `side_effects` 声明持久存储等外部副作用。Flow 在执行前要求 `--allow-side-effects`。部署阶段的 `destructive` 仍要求 `--allow-destructive`。Flow 不推断项目存档位置，也不自动备份或删除持久数据。

## 跨平台与证据

核心使用 Python 标准库，支持 Windows 和 Linux。进程按 argv 执行，不通过 shell 解释。串口使用可选的 PySerial。

主机回归、RTL 仿真、硬件状态读取、QSPI 读回和画面目视验收分别留证。工具只报告实际收集到的证据，不把某一层测试扩大成整体验收。
