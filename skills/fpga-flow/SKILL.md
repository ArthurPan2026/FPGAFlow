---
name: fpga-flow
description: 使用 fpga-flow 在 AMD Vivado/Vitis 工程中执行可重复的构建、验证、部署，或在智能体会话中断后恢复工作。
---

# FPGAFlow Agent 操作指南

对已配置的工程，使用消费项目自己的 `fpga-project.toml` 和 FPGAFlow CLI。项目路径、板卡规则和命令由消费项目维护。

## 恢复上下文

1. 先运行 `fpga-flow resume --project fpga-project.toml --json`。
2. 如果有活动 run，按返回的命令运行 `watch`；如果有失败 run，先查看日志尾部。
3. 按 resume 返回的下一步命令继续。resume 只给建议，不会执行命令。
4. 需要重新核对环境时，按目标阶段运行 `doctor --stage <name>` 或 `doctor --suite <name>`。

manifest 和不可变 run 记录保存实际状态，恢复时以对应记录中的产物与命令为依据。

## 构建与验证

- 使用 `plan` 检查缓存、依赖、工具链、输出和所需授权。plan 是只读命令。
- 使用 `build --stage <name>` 执行一个阶段。只有明确需要运行前置阶段时才加 `--with-deps`。
- Profile 默认串行。Profile 提高并行上限时，先声明共享 Vivado/Vitis、目标板和输出资源的锁。
- UART 期望文本使用 `expect_text`；需要正则时使用 `expect`。通信超时会停止后续步骤。只有 profile 明确允许时，断言失败才继续采集诊断。
- 分项汇报主机、RTL、UART、QSPI 读回和画面检查的实际结果。

## 板卡与副作用

- 部署前查看目标和产物状态。目标有多种部署方式时，明确选择 `--stage`。
- 只有得到用户授权后才运行 `deploy`。配置了外部副作用的阶段还需要 `--allow-side-effects`；破坏性部署需要 `--allow-destructive`。
- 设备占用时，读取锁持有者和诊断日志，确定本任务对应的资源。
- 画面验收引用采集或人工观察结果，UART 和仿真结果归入各自的测试项。

## 运行报告

自动续作时优先使用 JSON 输出。默认结果包含项目、target、run_id、阶段状态、缓存原因和输入/输出/证据数量，需要核对具体哈希时加 `--details`。完整记录保存在 run 目录，长任务进度使用 `watch` 查询。
