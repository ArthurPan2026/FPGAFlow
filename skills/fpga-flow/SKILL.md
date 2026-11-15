---
name: fpga-flow
description: 使用 fpga-flow 在 AMD Vivado/Vitis 项目中执行可重复的构建、验证、部署，或在智能体会话中断后恢复工作。
---

# FPGA Flow 操作指南

对已配置的项目，始终以 fpga-flow CLI 作为正式入口。读取项目自己的 fpga-flow.toml；涉及 AMD 工具细节或需要交互式检查 Vivado 时，查询 AMD Ross 文档或使用相应 MCP 能力。

## 上下文变化后恢复

1. 运行 fpga-flow status --json 和 fpga-flow resume --json。
2. 阅读中断阶段的最新运行记录及日志路径。
3. 运行 fpga-flow doctor 和 fpga-flow plan，再决定下一步构建操作。
4. 每次只继续一个阶段，并使用配置文件中声明的输入和输出。

manifest、运行记录和产物保存实际操作状态；不要依靠对话历史或 shell 历史重建步骤。

## 构建与验证

- 保持 plan 为只读操作。它不得启动 Vivado/Vitis、探测板卡或写入产物。
- 使用 build --stage <name> 执行单个阶段。只有用户要求同时运行前置阶段时才使用 --with-deps。
- 严格按 profile 中声明的内容运行主机测试和 RTL 套件。保留套件名称、随机种子、工具版本、日志和通过标记。
- 分别报告主机、RTL、UART、读回和画面证据。

## 板卡操作

- 部署前检查所选目标与 profile 一致，并确认所需产物哈希有效。
- 只有用户明确要求写入板卡时才使用 deploy --target <profile-target>。有破坏性的阶段，仅在用户已授权时传入所需的显式授权标记。
- 不终止占用 UART 或编程器会话的进程。报告占用情况并选择安全方案。
- 不得根据 UART 或仿真数据声称画面验收通过。

## 输出

自动续作时优先使用 CLI 的 JSON 输出。总结 run_id、阶段状态、产物哈希和下一步有效操作，使其他智能体能够继续工作，而不必重新探索工具链。
