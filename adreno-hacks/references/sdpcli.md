# sdpcli 运行时性能采集

`<skill>/tools/snapdragon-profiler-cli/` 附带 Qualcomm Snapdragon Profiler CLI（版本 2026.2）：设备端可执行文件 `sdpcli`（Android arm64 ELF）、trace 用 layer APK 和官方手册 `doc/html/index.html`。手册是权威参考，本文记录在本仓库真机（SM8650 / Adreno 750，Android 16）上验证过的用法与限制。

## 准备

```bash
adb push <skill>/tools/snapdragon-profiler-cli/sdpcli /data/local/tmp/sdpcli
adb shell chmod a+x /data/local/tmp/sdpcli
adb shell /data/local/tmp/sdpcli --version
```

- Windows Git Bash 会把 `/data/...` 改写成 `C:/Program Files/Git/data/...`，导致 push 失败或 shell 找不到程序。执行前设置 `export MSYS_NO_PATHCONV=1`，或在 PowerShell / cmd 中执行。
- 只在需要 trace 时安装 layer APK（Android 11+ 用 `adb install -g -r --force-queryable <apk>`；仅 64 位设备只装 arm64-v8a）。安装前先用 `adb shell pm list packages | grep -i profilinglayer` 检查是否已有同版本，避免重复安装。

## 实时指标（`sdpcli r`）

系统级指标，按固定周期轮询，无需重启应用，开销低；GPU 空闲时数值没有意义，采样前确认目标应用在前台并持续渲染。

```bash
adb shell "su -c '/data/local/tmp/sdpcli r l'"                 # 列出指标 ID（加 -v 带说明）
adb shell "su -c '/data/local/tmp/sdpcli r c -d 10000 -p 100 -o /data/local/tmp/sdp-rt 1,3,4,30,57,59,60'"
```

- 本机以 shell 用户运行 `r l` 会因读取 `/config/platform_mpam/...` 被拒而崩溃，需要 root（`su -c`）。
- `-d` 时长（ms）或 `-c` 次数；`-p` 采样周期（ms）；`-o` 输出 CSV 目录；指标 ID 必须放在最后，支持 `1,3,14` 与 `1-5` 混用。
- 本机共 122 项：GPU（时钟、利用率、Bus Busy、各类 Stall、纹理 L1/L2 Miss、LRZ、RTU / BVH、Shader Busy/Stalled、ALU/EFU 利用率、Wave 占用、读写带宽等）、CPU、Thermal、Network、IO。指标 ID 随驱动/版本可能变化，每次以 `r l` 输出为准并记录。

## Rendering stages trace（`sdpcli t`）

逐 surface / 渲染阶段的耗时与指标，输出 JSON 可用 Chrome tracing 或 Perfetto 打开。

```bash
adb shell "cd /data/local/tmp && ./sdpcli t s vk <package>/<activity>"   # 由 sdpcli 启动应用；-s 挂起到 trace 开始，可从第 0 帧采集
adb shell "cd /data/local/tmp && ./sdpcli t c -d 3000 -o /data/local/tmp/sdp-trace"   # 标准模式，约 10–15% GPU 开销
adb shell "cd /data/local/tmp && ./sdpcli t c -d 3000 -m lo -o /data/local/tmp/sdp-trace"   # 低开销模式，<1%，数据较少
```

前置条件：应用必须由 `sdpcli t s` 启动；manifest 包含 `INTERNET` 与 `ACCESS_NETWORK_STATE`；应用必须 **debuggable**（设备 `ro.debuggable=0` 时无法绕过，不要为此改系统属性）。用 `adb shell dumpsys package <package> | grep pkgFlags` 确认含 `DEBUGGABLE`；Release 包会报 `Failed to connect to the application`。

Rayloom 用 `python android.py --debug --output dist/rayloom-debug.apk` 构建可调试包。签名与手机上已装版本不一致时安装会失败、脚本不会自动卸载；卸载旧版会清空应用数据，须先征得用户同意。

实测（2026-11-12，Debug 包，默认 Cornell Box + Inline Path Tracer）：

- `t s vk com.rayloom.app/.MainActivity` 后台运行，等 logcat 出现 `RAYLOOM_ANDROID_FRAME_OK` 再 `t c`；`t c` 之前进程未由 sdpcli 启动会报 `No processes are available to profile`。
- 标准与 `-m lo` 各 3 秒均成功，约 130 帧；JSON 为 `{"traceEvents": [...]}`，事件有 `Surface`（`args` 含尺寸、MRT、像素格式、bins、renderMode、`newFrame`、`ComputeOnly`）、`Bin N`、`Render` / `Workload`、`Dispatch`、`Binning`、`Blit`、`Queue Submit`、`Preempt` 及 `New Frame`。按 `Surface.newFrame` 划分帧。
- 低开销模式 stage 只给出 `Workload`，不区分 Render / Binning；两种模式的每帧 GPU 时间一致（约 22.3 ms，其中 compute 21.8 ms），未观测到标准模式的额外开销。
- Debug 构建的数值不代表 Release 性能，用于对比时双方须同为 Debug 包。

## 结果与报告

- 采集结束后 `adb pull` 到 `report/<本次目录>/`，并生成可直接打开的 `index.html` 总报告（链接原始 JSON；trace 可在 ui.perfetto.dev 打开），记录 sdpcli 版本、模式、指标 ID 与名称、采样周期、时长、目标包名与设备身份；清理设备上的输出目录。
- 运行时数值受温控、频率策略、后台负载和采集本身开销影响；对比实验保持相同设备状态、场景与参数，必要时重复多次。
- 不要在 `capture.py` 静态采集运行期间测性能（其 shaderlog/binlog 会显著拖慢驱动）；先确认 `restoration.json` 已恢复。
- 不要自动修改 GPU 频率、温控或应用数据。
