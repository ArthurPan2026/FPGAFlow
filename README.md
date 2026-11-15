# FPGAFlow

FPGAFlow 是面向不同 AMD Vivado/Vitis 项目的统一构建、验证和板级部署工具。工具核心负责项目 profile、阶段依赖、缓存、运行日志、产物哈希和安全门；RTL 目录、HLS 组件、PS 应用、板卡身份和验收规则由消费项目自己的配置提供。

v0.1 可在 Linux 和 Windows 主机运行，提供 AMD 工具链检查、命令阶段执行、Vivado/XSim 阶段编排，以及 Zynq JTAG、QSPI 和 UART provider。项目可按需使用 HLS、PL、PS、仿真或板测阶段。当前版本不宣称支持其他 FPGA 厂商。

## 架构图

![FPGAFlow 跨 AMD 项目架构图](docs/fpga-flow-architecture.png)

可编辑源图：[fpga-flow-architecture.drawio](docs/fpga-flow-architecture.drawio)。

## 安装

```sh
python -m pip install .
fpga-flow --help
```

核心只依赖 Python 标准库，要求 Python 3.11 或更新版本。需要使用 UART provider 时安装可选依赖：

```sh
python -m pip install ".[uart]"
```

Vivado、Vitis、XSCT、Bootgen 和板卡驱动仍需在本机单独安装和授权。

## 项目配置

每个消费项目在项目根目录维护 `fpga-flow.toml`。项目配置保存本项目的输入与产物、阶段依赖图、AMD 工具版本、板卡身份、部署方式和测试验收条件。通用引擎不包含 GBA ROM 路径、特定 RTL 源目录或某块板子的串口号。

- `examples/zynq7000-rtl-ps/flow.toml` 展示不含 HLS 的 RTL + PS 项目。
- `examples/zynq7000-hls-ps/flow.toml` 展示含 HLS、Vivado、PS、BOOT.BIN 和 QSPI 阶段的项目。
- GBA 消费 profile 位于 GBA 仓库的 `fpga-flow.toml`，为首个真实工程适配。

阶段使用 argv 数组执行命令，不经过 shell。配置中的路径相对项目根目录，统一使用正斜杠格式，并由 `pathlib` 在 Windows 和 Linux 上转换。输出路径可使用 glob 表达动态生成的 XPFM/ELF 目录，执行后会展开并记录具体文件哈希。

## 常用命令

```sh
fpga-flow doctor --config fpga-flow.toml
fpga-flow plan --config fpga-flow.toml --target zybo_z7_20
fpga-flow status --config fpga-flow.toml --json
fpga-flow resume --config fpga-flow.toml --json
fpga-flow build --config fpga-flow.toml --stage hardware
fpga-flow validate --config fpga-flow.toml --suite board-smoke --target zybo_z7_20
fpga-flow deploy --config fpga-flow.toml --target zybo_z7_20 --stage deploy_jtag --allow-destructive
fpga-flow release --config fpga-flow.toml --target zybo_z7_20 --deploy --deploy-stage deploy_jtag --allow-destructive
```

`plan` 只读取 profile、输入和缓存记录，不启动工具链、不连接或写入板卡。`build` 一次运行一个阶段；需要同时运行前置阶段时，必须显式使用 `--with-deps`。`validate` 按配置中的测试套件执行。`deploy` 必须给出目标；同一板卡配置了 JTAG 与 QSPI 时还必须用 `--stage` 指定一种传输。带破坏性的部署阶段还需要 `--allow-destructive`。`release` 默认不烧录，只有显式提供 `--deploy` 才会执行部署。

## 智能体与 AMD Ross 的协作方式

AMD Ross 知识库与 Vivado MCP 适合查询 AMD 官方资料、检查已打开的 Vivado 工程并进行交互式调试。FPGAFlow 技能指导 Ross、Codex 或其他智能体调用同一套 CLI，执行可重复的构建、恢复、部署和板级测试。AMD 官方将 Ross 组织为 MCP、AMD 知识库、智能体技能和设计示例；官方仓库也将技能与 MCP Server 分开安装：[AMD Ross 产品页](https://www.amd.com/en/products/software/ross-agentic-ai.html)、[AMD Ross 官方仓库](https://github.com/Xilinx/ross-ai-assistant)。

聊天记录不是构建数据库。上下文压缩或更换智能体后，先运行 `status --json` 和 `resume --json`，检查最新 `run_id`、日志和 `plan`，再继续下一步。manifest 是当前阶段状态索引；每次执行另有不可变运行记录。

## 测试

```sh
python -m unittest discover -s tests -v
```

GitHub Actions 在 Ubuntu 和 Windows 运行同一套测试。测试覆盖 profile/schema、不同阶段图、HLS 可选阶段、输入和产物哈希、缓存命中/失效、失败恢复、命令行执行、JTAG/QSPI provider、UART provider、双平台路径与进程行为。实际 HIL 验收仍由每个项目 profile 定义；主机、RTL 仿真、UART/计数器、QSPI 读回和画面目视证据分别记录。
