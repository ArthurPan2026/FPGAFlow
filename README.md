# FPGAFlow

FPGAFlow 是可独立安装、与具体 FPGA 工程解耦的 Python CLI，用于执行工程 profile 中声明的构建、验证和部署阶段。项目仓库维护自己的 Vivado/Vitis 输入、脚本、配置和产物；FPGAFlow 作为外部工具安装在开发主机。

## 安装与版本

当前版本为 0.2.0，Python 要求为 3.15 或更新版本。构建 wheel 与 source distribution 的发布流程由版本标签触发，产物发布在 GitHub Releases。

下载适用于目标版本的 wheel 后安装：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install ./fpga_flow-0.2.0-py3-none-any.whl
fpga-flow --version
```

Windows PowerShell 激活命令为 `.venv\Scripts\Activate.ps1`。使用 UART provider 时安装 pySerial：

```sh
python -m pip install "pyserial>=3.5"
```

Vivado、Vitis、XSCT、Bootgen 和板卡驱动独立安装，并按工程要求锁定版本。

## 工程 profile

消费项目只维护 `fpga-project.toml`，声明项目身份、内置 profile、XPR 或 IP 打包入口、工具版本、板卡和测试台。FPGAFlow 解析显式引用，生成源码与测试 inventory、输入锁和运行记录。XPR 仍是 Vivado GUI 工程真源。兼容入口 `--config` 供旧的完整 profile 使用。

```sh
fpga-flow inventory --project fpga-project.toml
fpga-flow plan --project fpga-project.toml --target zybo_z7_20
fpga-flow build --project fpga-project.toml --stage hardware
fpga-flow build --project fpga-project.toml --stage app --with-deps
fpga-flow validate --project fpga-project.toml --suite host
```

Flow 按 argv 执行 profile 命令并记录工具版本、输入哈希、产物哈希、运行日志和阶段结果。stage 的具体构建规则留在工程仓库；硬件工具的 GUI 工程仍由 Vivado 或 Vitis 管理。

## 主机本地配置

工具安装路径、板卡连接地址和串口属于开发主机配置。项目描述模式从仓库外的状态目录读取 `fpga-flow.local.toml`，也可用 `FPGA_FLOW_LOCAL_CONFIG` 指定路径。Linux 默认状态根目录为 `$XDG_STATE_HOME/fpga-flow`，未设置时使用 `~/.local/state/fpga-flow`；Windows 使用 `%LOCALAPPDATA%/FPGAFlow`。各 checkout 按项目 ID 和路径哈希隔离。

```toml
[toolchains.vivado]
command = ["/tools/AMDDesignTools/2025.2.1/Vivado/bin/vivado"]

[toolchains.vitis]
command = ["/tools/AMDDesignTools/2025.2.1/Vitis/bin/vitis"]
version_command = ["/tools/AMDDesignTools/2025.2.1/Vitis/bin/vitis", "--version"]

[targets.zybo_z7_20]
hw_server_url = "tcp:127.0.0.1:3121"

[targets.zybo_z7_20.uart]
port = "/dev/serial/by-id/replace-with-board-adapter"
```

也可以通过 `FPGA_FLOW_LOCAL_CONFIG` 指定本地主机 overlay 文件。Overlay 支持调整 toolchain 命令、目标连接参数和阶段环境变量；项目硬件身份、阶段图、输入与产物仍由版本管理的 profile 定义。

## 外部依赖与部署

构建与部署日志、工具工作区和产物写入 `build/fpga-flow/runs/<run-id>/`。外部状态目录保存输入锁和短索引。下游消费通过哈希核验的生产 run 产物；硬件输入变化时使用干净综合与实现，默认关闭增量实现。

可选的 `[exports]` 用 `directory` 和 `artifacts` 指定交付目录与 bundle 产物名。完整 bundle 成功后自动导出到本次生产 run 的独立子目录。未配置时不导出；已有交付目录不会被覆盖。

外部源码依赖使用完整 Git commit 和输入文件模式锁定。阶段执行前后会校验依赖提交与工作树，依赖输入哈希进入阶段指纹。

`side_effects` 用于声明会影响工程或板卡外部状态的阶段。持久存储写入需要显式授权；破坏性部署使用 `--allow-destructive`。部署、验证和构建记录分别保存在每次运行的不可变记录中。

常用命令：

```sh
fpga-flow --version
fpga-flow doctor --project fpga-project.toml --stage hardware
fpga-flow resume --project fpga-project.toml --json
fpga-flow watch --project fpga-project.toml --run-id RUN_ID --follow
fpga-flow history --project fpga-project.toml --limit 20
fpga-flow deploy --project fpga-project.toml --target zybo_z7_20 --stage deploy_jtag --allow-destructive
fpga-flow release --project fpga-project.toml --target zybo_z7_20
```

## 开发与发布

```sh
python -m unittest discover -s tests -v
python -m build
```

发布工作流校验 tag 与包版本，运行单元测试，构建 wheel 和 source distribution，验证安装后的 CLI，并将安装包附加到版本 Release。FPGAFlow 仓库保持项目无关；具体 FPGA 项目的 profile 和源文件由各项目仓库管理。
