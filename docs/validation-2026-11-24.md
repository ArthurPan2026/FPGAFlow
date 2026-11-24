# 2026-11-24 变更与验证

## 版本与变更

本轮版本为 `0.3.0`。workspace DAG 连接多个项目的阶段，支持可选回归、共享资源锁和发布产物校验导入。重复导入相同产物时，依赖锁保持稳定。

UART 增加 `require_command_echo`，在收到当前命令回显后匹配响应。QSPI provider 统一解析硬件服务器地址，Program Flash 的版本探测使用 `vitis --version`。

## 主机验证

2026-11-24 在 Linux、Python 3.15 上执行 `PYTHONPATH=src python -m unittest discover -s tests -v`，140 项测试中 138 项通过，2 项 Windows 原生路径与批处理测试跳过。测试涵盖阶段调度、缓存、锁、workspace 导入与回滚、部署 provider 和 UART。wheel 和源码包构建成功，独立虚拟环境安装 wheel 后 `fpga-flow --version` 返回 `0.3.0`。

## 消费工程证据

GBA 通过 workspace 使用 EmbeddedVideoIP `0.3.0-dev.6 / coreRevision 7`。GBA hardware run `20261123T215815Z-8d4e4fd8d5` 的最终 setup WNS 为 `+0.003 ns`，hold WHS 为 `+0.050 ns`，TNS/THS 为 0，实际 DRC Error 为 0。

JTAG run `20261124T013830Z-b30557490c` 和 QSPI run `20261124T014336Z-d05b0aa33c` 分别保存对应部署记录。消费项目的测试、硬件和人工结果收录于 GBA 验证文档。

## 公开交付

源码以 Apache-2.0 公开。README 提供源码安装、项目清单、workspace 和运行记录入口。图源与矢量图一同维护。
