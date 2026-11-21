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

每个工程在自己的仓库维护 `fpga-flow.toml`，声明阶段依赖图、工具版本、目标板、输入、产物和运行规则。所有项目路径相对于 profile 所在目录，并使用正斜线。

```sh
fpga-flow plan --config fpga-flow.toml --target zybo_z7_20
fpga-flow build --config fpga-flow.toml --stage hardware
fpga-flow build --config fpga-flow.toml --stage app --with-deps
fpga-flow validate --config fpga-flow.toml --suite host
```

Flow 按 argv 执行 profile 命令并记录工具版本、输入哈希、产物哈希、运行日志和阶段结果。stage 的具体构建规则留在工程仓库；硬件工具的 GUI 工程仍由 Vivado 或 Vitis 管理。

## 主机本地配置

工具安装路径、板卡连接地址和串口属于开发主机配置。将它们写入与 profile 并列的 `fpga-flow.local.toml`，该文件适合加入项目的本地忽略规则：

```toml
[toolchains.vivado]
command = "/tools/AMDDesignTools/2025.2.1/Vivado/bin/vivado"

[toolchains.vitis]
command = "/tools/AMDDesignTools/2025.2.1/Vitis/bin/vitis"
version_command = ["/tools/AMDDesignTools/2025.2.1/Vitis/bin/vitis", "--version"]

[targets.zybo_z7_20]
hw_server_url = "tcp:127.0.0.1:3121"

[targets.zybo_z7_20.uart]
port = "/dev/serial/by-id/replace-with-board-adapter"
```

也可以通过 `FPGA_FLOW_LOCAL_CONFIG` 指定本地主机 overlay 文件。Overlay 支持调整 toolchain 命令、目标连接参数和阶段环境变量；项目硬件身份、阶段图、输入与产物仍由版本管理的 profile 定义。

## 外部依赖与部署

外部源码依赖使用完整 Git commit 和输入文件模式锁定。阶段执行前后会校验依赖提交与工作树，依赖输入哈希进入阶段指纹。

`side_effects` 用于声明会影响工程或板卡外部状态的阶段。持久存储写入需要显式授权；破坏性部署使用 `--allow-destructive`。部署、验证和构建记录分别保存在每次运行的不可变记录中。

常用命令：

```sh
fpga-flow --version
fpga-flow doctor --config fpga-flow.toml --stage hardware
fpga-flow resume --config fpga-flow.toml --json
fpga-flow watch --config fpga-flow.toml --run-id RUN_ID --follow
fpga-flow history --config fpga-flow.toml --limit 20
fpga-flow deploy --config fpga-flow.toml --target zybo_z7_20 --stage deploy_jtag --allow-destructive
fpga-flow release --config fpga-flow.toml --target zybo_z7_20
```

## 开发与发布

```sh
python -m unittest discover -s tests -v
python -m build
```

发布工作流校验 tag 与包版本，运行单元测试，构建 wheel 和 source distribution，验证安装后的 CLI，并将安装包附加到版本 Release。FPGAFlow 仓库保持项目无关；具体 FPGA 项目的 profile 和源文件由各项目仓库管理。
