# FPGAFlow

FPGAFlow 是一套面向 AMD Vivado/Vitis 工程的项目无关 CLI。核心处理阶段依赖、内容哈希、缓存、运行记录、工具预检和部署保护。外部工程在自己的仓库维护 profile、构建脚本、板卡身份和验收规则。

目前支持 Linux 和 Windows 主机，提供 argv 命令执行器、Vivado/Vitis 工具检查、Zynq JTAG、QSPI 和 UART provider。工具核心不包含任何外部工程的源码路径、ROM 路径、应用协议或应用数据。

FPGAFlow 的仓库内容只定义通用 schema、调度器、provider、缓存和示例 profile。具体工程名称、应用参数、存储路径、板级验收命令和部署策略必须留在外部工程 profile 中。

## 安装

```sh
python -m pip install .
fpga-flow --help
```

核心只依赖 Python 标准库，要求 Python 3.11 或更新版本。使用 UART provider 时安装可选依赖：

```sh
python -m pip install ".[uart]"
```

Vivado、Vitis、XSCT、Bootgen 和板卡驱动仍需单独安装和授权。

## 项目配置

每个外部工程在项目根目录维护 `fpga-flow.toml`。配置声明输入、产物、阶段依赖图、工具版本、目标、资源锁和测试规则。所有项目路径都相对于 profile 所在目录，并使用正斜线。

`flow.max_parallel_stages` 默认值为 1。项目可提高并行上限，并用阶段的 `resources` 声明共享资源。Flow 还会锁住同一工具链、同一板卡身份和相互覆盖的输出路径。

`side_effects` 用来声明会影响项目外部状态的阶段，例如持久存储写入。运行这类阶段必须显式传入 `--allow-side-effects`。破坏性部署仍需 `--allow-destructive`。

示例配置：

- `examples/zynq7000-rtl-ps/flow.toml` 展示 RTL + PS 流程。
- `examples/zynq7000-hls-ps/flow.toml` 展示 HLS、PL、PS、启动镜像和 QSPI 阶段。

阶段命令按 argv 数组执行，不经过 shell。只有被 profile 声明的输入和依赖产物会参与阶段指纹。工具专属的增量构建逻辑留在外部工程脚本中。

## 常用命令

```sh
fpga-flow doctor --config fpga-flow.toml --stage hardware
fpga-flow plan --config fpga-flow.toml --target zybo_z7_20
fpga-flow resume --config fpga-flow.toml --json
fpga-flow watch --config fpga-flow.toml --run-id RUN_ID --follow
fpga-flow history --config fpga-flow.toml --limit 20
fpga-flow build --config fpga-flow.toml --stage hardware
fpga-flow build --config fpga-flow.toml --stage app --with-deps
fpga-flow validate --config fpga-flow.toml --suite host
fpga-flow validate --config fpga-flow.toml --suite board --allow-side-effects
fpga-flow deploy --config fpga-flow.toml --target zybo_z7_20 --stage deploy_jtag --allow-destructive
fpga-flow release --config fpga-flow.toml --target zybo_z7_20
```

`plan`、`status`、`resume` 和 `watch` 都不会启动工具链或写板。JSON 模式的成功和失败结果都写到 stdout；阶段摘要默认只返回输入、输出、证据数量，完整哈希映射仍保存在 run 记录中。需要展开哈希时加 `--details`；长任务进度写到 stderr，失败仍返回非零退出码。

`resume` 汇总当前阶段、缓存和失败记录，并给出完整的下一步命令。它只提供建议，不会自动执行。长任务在运行时持续写入 stdout/stderr 日志；`watch` 可显示心跳和日志尾部。运行 ID 会在阶段开始时报告。

`doctor --stage` 或 `doctor --suite` 只检查所选阶段需要的工具链和只读硬件预检。JTAG 预检只查询目标，QSPI 预检只查询 Flash 目标，UART 预检只枚举设备，不打开串口或发送数据。未指定范围时检查所选目标的完整 profile。

同一输出目录只允许一个变更操作同时运行。Flow 默认立即报告锁持有者；需要等待时显式使用 `--wait-lock`。阶段 DAG 默认串行；profile 可提高并行上限。部署、验证与工具共享资源时，Flow 会按锁配置串行执行。

## 记录与迁移

`out/fpga-flow/manifest.json` 是紧凑的当前状态索引。每次操作的输入哈希、产物哈希、命令和日志引用保存在不可变的 `out/fpga-flow/runs/<run_id>/run.json` 中。

首次写入时，Flow 会把旧版 manifest 原样归档，再迁移到紧凑格式。旧 run 记录不会删除。可用 `history` 按阶段查看记录。

## Agent 与 AMD Ross

AMD Ross 文档搜索和 Vivado MCP 适合查询官方资料、检查打开的 Vivado 工程和交互调试。FPGAFlow Skill 指导智能体调用统一 CLI。智能体在上下文中断后先运行 `resume --json`，再用 `watch` 查看正在运行的任务或失败日志。聊天记录不是构建事实来源。

参考：[AMD Ross 产品页](https://www.amd.com/en/products/software/ross-agentic-ai.html)、[AMD Ross 官方仓库](https://github.com/Xilinx/ross-ai-assistant)。

## 验证边界

```sh
python -m unittest discover -s tests -v
```

CI 在 Ubuntu 和 Windows 运行同一套测试。主机回归、RTL 仿真、UART 状态、QSPI 读回和画面目视验收分别记录。UART 或仿真通过不能代替画面验收。
