# 跨项目 workspace

workspace 清单把多个项目的 FPGAFlow 阶段连成一张依赖图。项目仍各自维护 fpga-project.toml、脚本、输入和产物。workspace 只声明项目路径、阶段顺序和产物导入规则。

## 命令

    fpga-flow --workspace fpga-workspace.toml plan --json
    fpga-flow --workspace fpga-workspace.toml run --json
    fpga-flow --workspace fpga-workspace.toml status --json
    fpga-flow --workspace fpga-workspace.toml history --limit 20 --json

plan 只解析配置并检查 DAG，不启动工具。run 按依赖顺序执行。status 显示最近一次运行，history 列出运行记录。

如果阶段会访问开发板或修改外部状态，节点必须标记 side_effects = true。运行时还要传入 --allow-side-effects。破坏性节点用 destructive = true 标记，并要求 --allow-destructive。

回归节点可以标记 optional = true。默认运行会把这类节点记为 skipped，并继续运行其下游节点。需要完整验收时传入 --include-optional。这样快速硬件迭代可以跳过耗时的 host 与 RTL 回归。

## 清单结构

workspace 文件使用 TOML。项目路径相对于 workspace 文件所在目录。每个项目条目指向它自己的 fpga-project.toml。

    schema_version = 1

    [workspace]
    id = "video-product-release"
    output_dir = "build/fpga-flow/workspace"

    [projects.canonical_ip]
    manifest = "../EmbeddedVideoIP/fpga-project.toml"

    [projects.consumer]
    manifest = "fpga-project.toml"

    [[nodes]]
    name = "canonical_release"
    kind = "flow"
    project = "canonical_ip"
    action = "release"
    resources = ["vivado", "python"]

    [[nodes]]
    name = "import_ip"
    kind = "artifact_import"
    source_project = "canonical_ip"
    source_node = "canonical_release"
    target_project = "consumer"
    release_stage = "release"
    expected_version = "0.3.0"
    expected_vlnv = "vendor.example:video:core:1.0"
    expected_core_revision = 5
    ip_asset = "VideoIP-0.3.0-vivado-2025.2.1.zip"
    ps_asset = "VideoIP-PS-0.3.0.zip"
    ip_prefix = "video-ip"
    ps_prefix = "video-ip-ps/Video/Lib"
    ip_target = "ip-repo/video-ip"
    ps_target = "src/ps/Video/Lib"
    dependency_name = "video_ip"
    dependency_lock_file = "ip-repo/video-ip-DEPENDENCY-LOCK.json"
    needs = ["canonical_release"]
    resources = ["ip-package-import"]

    [[nodes]]
    name = "consumer_build"
    kind = "flow"
    project = "consumer"
    action = "build"
    stage = "hardware"
    needs = ["import_ip"]
    resources = ["vivado"]

kind = flow 节点调用项目清单中已配置的 validate、build、release 或 deploy 操作。needs 必须指向其他节点。FPGAFlow 会拒绝有环依赖。

kind = artifact_import 节点从前置项目的成功运行记录中找到 release_stage 产物。导入前会核对 SHA256SUMS、ZIP 内文件路径、版本、VLNV、core revision，以及 IP 与 PS 产物的来源提交。导入器拒绝绝对路径、目录穿越、重复成员和符号链接。

导入器先解包到目标目录旁的暂存目录，并保存现有目录快照。所有目标都准备好后才替换。任一替换失败时，导入器恢复旧目录。运行记录保存每个目标导入前后的树哈希和快照位置。

## 运行记录和资源锁

workspace 输出目录内会生成 runs/<run-id>/run.json 和各节点日志。记录包含清单与项目配置哈希、依赖边、项目子运行 ID、产物哈希、导入前后树哈希和节点状态。节点失败后，它的下游节点会标记为 blocked。

节点的 resources 声明共享的工具或硬件资源。workspace 会在主机状态目录中锁定这些资源，避免两个 workspace 同时使用同一台 Vivado、Vitis 或板卡。默认状态目录与单项目 Flow 相同，也可用 FPGA_FLOW_STATE_ROOT 或 workspace.resource_lock_dir 指定。

Consumer 项目应把生成的 dependency lock 文件加入依赖的 metadata 列表。这样后续 Flow 指纹会追踪导入的版本和哈希。具体项目的阶段、板卡规则和端口仍放在 consumer 项目仓库中；FPGAFlow 核心只负责通用 DAG 与安全导入。
