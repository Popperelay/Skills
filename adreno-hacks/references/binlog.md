# binlog 解码与 REA 适配

## 内置解码器

`scripts/decode_binlog.py` 接受单个 binlog 或包含 `qgl_bin_log_*.bin` 的目录；`--out` 必须是当前项目 `report/` 内新的或空的目录。已有采集无需连接手机，使用其原始 `summary.json`；省略 `--summary` 时从输入的祖先目录查找。

解码器按日志自带 schema 读取动态 marker、事件 ID、字段和枚举，不写死每个事件的编号。已验证 PKG110／SM8650／Adreno 750 的驱动 build `f6b5df5188`、编译器 `E031.45.02.26`；只证明这个版本的样本可用，其他驱动的布局、字段类型或编码可能改变。同一版本不同采集的 ID 也会变化，这不等于格式不兼容。

输出 `index.html`、`binlog-summary.json` 和每文件 JSON，包含输入 SHA-256、手机／驱动元数据、schema、完整事件计数、数值范围及每类最多两个样本（`--sample-limit` 可调）。所有完整事件均解析并逐对象重编码核对，默认 JSON 不导出全部事件。整数保留无符号位模式，枚举与业务语义需另行核对。

- `complete`：完整解析，重新编码 SHA-256 与输入一致，退出码 0。
- `partial`：保留完整可验证前缀；错误记录及其嵌套对象不进入统计。不能称为完整成功。
- `failed`：没有完整事件。结构不支持、损坏或重编码不符时退出码 1；仅遇到截断且有完整事件时退出码 2。

以 `validated_reencoded_bytes` 和 `unvalidated_tail_bytes` 判断范围，不能用 `consumed_bytes` 冒充已验证字节数。`capture_hash_match=false` 时不能使用该 summary 的驱动身份，`null` 表示缺少关联证据。身份必须来自产生该日志的原采集，不能以升级后的驱动或另一台手机替代。

## 失败后必须调查

遇到 `rea_required=true`（包括 partial），AI 应继续使用 [morluto/rea](https://github.com/morluto/rea) 调查失败原因；确定格式不兼容时，为本次驱动编写临时解码器。不要只报告失败便结束，也不要放宽校验以制造成功。

1. 保留错误 code、字节偏移、当前 schema、验证前缀和原始尾部。先区分截断、损坏与格式差异。末尾截断本身不证明换了格式，缺失字节不能由新解码器补造。
2. 从原采集 `summary.json` 及 `collection.driver_libraries` 找到实际 `vulkan.adreno.so` 等库，核对库 SHA-256、手机、GPU、驱动 build 与编译器版本。缺少原驱动时尝试从原手机只读取得并核对身份；不匹配或无法取得时明确记录阻塞和恢复所需输入，不能凭别的驱动硬猜。
3. 优先复用可用的 REA MCP 或 CLI。读取官方当前用法，选择支持 Android ARM64 ELF 的 provider。已验证的路线是 REA 6.5.0 的 `execute-rizin-command` 加 Rizin 0.9.1；Windows Ghidra 的 PE 支持不代表能处理这个 ELF。缺少工具时可在临时工具目录安装官方 CLI/provider，用 CLI 即可完成此任务，不要求修改全局 MCP 配置或重启 Codex。
4. 从 `qgl_bin_log` 文件名模板及失败字段／事件名定位驱动写入代码，追踪 schema、marker、ID 分配、标量宽度、数组和结束符。可用 `rea execute-rizin-command <driver> '<command>' --json` 保存 Evidence；用 `REA_RIZIN_COMMAND` 指定 Rizin。反汇编器的函数标签和 PLT 注释也需核对，不能仅凭字符串猜格式。[REA Rizin 文档](https://github.com/morluto/rea/blob/main/docs/reverse-engineering-tools.md)。
5. 将适配器写到 `report/<本次目录>/adapters/<驱动build或哈希>/decode_binlog.py`；保留内置脚本和原始 binlog。记录适配器 SHA-256、适用驱动库 SHA-256、REA/provider 版本、Evidence、命令与输出，新结果使用独立目录。
6. 先用同驱动的小型 probe，再用失败的应用日志验证。必须检查完整输入或明确前缀的逐字节重新编码、未知记录／未解析尾部，并用已知 Dispatch 参数、Shader hash 与 shaderlog 等独立证据核对。无法解释的字段保持原始值或未知，不得跳过坏记录后宣称全部解码。

把诊断、临时适配器结果及限制写入本次 `decoder-adaptation.json`，并在总 HTML 报告链接 REA Evidence、解码 JSON 和脚本。仅验证单一新版本的临时适配器不自动替换 skill 的通用脚本；永久更新需保留原版本回归验证。

binlog 时间戳的单位／时钟域未验证前不能计算 GPU 耗时，驱动累计值不能直接称为硬件 counter；schema 中存在某个枚举项也不表示实际触发过该选项。
