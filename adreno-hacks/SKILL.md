---
name: adreno-hacks
description: Android Adreno GPU 分析。静态数据：采集真机 shader ISA、编译统计并解码 QGL binlog，解码不兼容时用 REA 逆向对应驱动并生成临时解码器。运行时性能：用附带的 Snapdragon Profiler CLI（sdpcli）采集实时 GPU 指标与 rendering stages trace。
---

# Adreno Hacks

依赖 Python 3.11+ 和 ADB。以下 `<skill>` 为本文件所在目录；从目标项目根目录运行，输出位于 `report/`。

数据分两类，按需求选择，不能互相替代：

| 需求 | 途径 |
| --- | --- |
| GPU 静态数据：ISA、寄存器、scratch、max waves、指令统计、驱动 API 事件（binlog）、驱动身份 | 下文「静态数据」，`<skill>/scripts/` 已有操作 |
| GPU 运行时性能：GPU 利用率、频率、带宽、per-surface / per-stage 耗时、渲染阶段指标 | 下文「运行时性能」，`<skill>/tools/snapdragon-profiler-cli/` 的 sdpcli |

## 静态数据

附带 Android arm64-v8a Vulkan 探针。需已有 root 权限和 Permissive SELinux。

1. 执行 `python <skill>/scripts/capture.py --probe`，自动识别 ADB、设备和前台应用；仅在有歧义时指定参数。
2. 完整采集执行 `python <skill>/scripts/capture.py --seconds <采集秒数> --binlog`。时长优先遵从用户，否则按目标选择并记录。复现时保持相同的 `--launch-args` JSON 字符串数组，每次使用新输出目录；缓存命中或窗口过短可能没有编译产物。
3. 阅读 `index.html`、`summary.json` 和 `parse-audit.json`，核对采集状态、解析遗漏及文件哈希。用 `shaders[].origin` 区分应用与独立探针，探针 API 统计不能代表应用 pipeline；缺失值 `null` 不能按零解释。source_crc 是驱动 CRC，源文件 SHA-256 见 `collection.sources.files`；证据不足的 Pass 对应保持未知。
4. 确认 `restoration.json` 恢复成功后再测性能。中断或断连后，重连并执行 `python <skill>/scripts/capture.py --recover <output>/recovery.json`；恢复未确认前不继续采集。
5. 有 binlog 时执行 `python <skill>/scripts/decode_binlog.py <output>/raw --summary <output>/summary.json --out <output>/binlog`。阅读解码 HTML 和 JSON；失败或部分解析时，按 [binlog 解码与 REA 适配](references/binlog.md) 用原采集驱动调查，格式不兼容时编写临时解码器。

编译统计和探针单次 timestamp 不是运行时性能测量；scratch 不足以证明寄存器溢出。binlog 的跨驱动兼容性和时间戳单位未保证，不需要时可省略 `--binlog`；原始文件始终保留。

其他参数查看 `--help`；`--source-root <设备源码目录>` 指定源码快照范围，`--parse <file-or-directory>` 离线解析日志。采集能力、数据及缺项以 JSON 和原始证据为准。

## 运行时性能

运行时 GPU 性能只通过 sdpcli 获取，用法、模式与注意事项见 [sdpcli 运行时性能采集](references/sdpcli.md)，官方手册在 `<skill>/tools/snapdragon-profiler-cli/doc/html/index.html`。

- 实时指标（`sdpcli r`）：系统级，无需重启应用，开销低。
- Rendering stages trace（`sdpcli t`）：需由 sdpcli 启动目标应用、安装 layer APK，且应用必须 debuggable；标准模式约 10–15% GPU 开销，`-m lo` 低开销但数据较少。
- 性能测量前先确认静态采集已恢复驱动配置（`restoration.json`），且不在静态采集运行期间测量；记录采样周期、时长、模式和指标 ID，结果写入 `report/`。

## 通用约束

不要自动修改 GPU 频率、温控或应用数据。
