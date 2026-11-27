# 排障手册（按 gate.json 的 category 或现象查）

修好之后重跑**同一条** `run` 命令即可，工具会从失败的阶段继续。不要手动删除阶段产物，需要重做时用 `--from <stage>`。

| 现象 / category | 原因 | 处理 |
|---|---|---|
| exit 20 / `Sign in to confirm you're not a bot` | 本机出口 IP（腾讯云香港）被 YouTube 判为机器人 | 让用户导出 cookies.txt（见 SKILL.md）或者提供本地视频文件。换客户端（tv/ios/android…）都没用，不必再试 |
| `--cookies-from-browser` 报 `Failed to decrypt with DPAPI` | Chrome/Edge 新版启用了**应用绑定加密**（Local State 里有 `app_bound_encrypted_key`），关掉浏览器也解不开 | 不要尝试绕过；改用扩展导出 cookies.txt |
| `youtube_js` / `n challenge solving failed` / 只剩 storyboard 格式 | 宿主的 node 带有预加载 shim，在 yt-dlp 使用的 `--permission` 沙箱里读不到 | 检查本机缓存的 Deno；`doctor` 里的 deno 项应为 OK |
| `youtube_format` / `Requested format is not available` | 多半是上面的签名求解失败，或需要登录 | 先确认 deno，再考虑 cookies |
| 下载到的音轨不是英文 | 视频带 YouTube 自动配音的多语种音轨 | 工具已锁定 `language^=en` 且排除 `-drc`；若视频没有语言标签，会退回到第一条音轨，识别阶段的语言检测会在日志里报 WARNING |
| `gpu_oom` | 显存被其他程序占用（Unity、游戏、本地大模型等） | 告诉用户关闭后重跑；不要替用户结束进程 |
| 合成变慢（RTF 从约 1.0 升到 1.5 以上） | 其他程序抢占 GPU；不是 VoxCPM 的问题 | 同上；`doctor` 的 gpu_apps 会列出占用显卡的程序 |
| 合成卡在 `Warm up VoxCPMModel...` | Windows 上 torch.compile（inductor）预热卡死 | 工具默认 `optimize=False`；不要加 `--optimize` |
| `safe_delete` / `SAFE_DELETE_BULK_CONFIRM_REQUIRED` | 宿主在一轮里删除超过 50 个文件时会拦截 | 工具已改为覆盖写和改名；手动清理时也不要批量删除，用 `clean --yes`（改名挪到 trash/） |
| 封装 MP4 时 `Invalid data found when processing input`（空字幕） | mov_text 编码器不接受空 SRT | 工具用空 ASS 作「关闭字幕」轨；自己改动时也照这个做 |
| 抽帧看字幕时多个时间点得到同一帧 | ffmpeg 把 `-ss` 放在 `-i` 前做快速定位，加上 subtitles 滤镜时定位不准 | 验收抽帧把 `-ss` 放在输出侧（`-i file -ss T`） |
| `env` / 装不上 voxcpm | VoxCPM2 要求 Python 3.10–3.12；RTX 50 系（sm_120）需要 cu128 版 PyTorch | 从技能的 dub.ps1 启动，使用已下载的 Python 3.12 和依赖 |
| `doctor` 中 torch+cuda 显示 arch 缺失 | 装成了不含 sm_120 的 torch | 确认接收方 GPU 和驱动兼容使用的 CUDA 12.8 PyTorch |
| exit 10 反复出现同一个分块 | 子代理没写出文件，或者校验没过 | 看 gate.json 里该分块的 `status`/`missing`/`over_by_too_much`，带着这些信息 resume 子代理 |
| exit 30 后续跑仍是 30 | 只有在 `qa.max_rewrite_passes` 调大时才会发生 | 检查 rewrites.json 的键是否是字符串序号、值是否非空 |
| 成片里个别句子念的是英文原声 | 设计如此：极短又反复合成失败的句子会保留原声（`metadata/qa_decisions.json`） | 想改成配音：在 rewrites.json 里给这句写可念的中文并重跑 |
| 字幕盖住画面下方的内容 | 硬字幕位置固定 | 推荐使用软字幕版（可以关闭）；也可以用 `--set subtitles.hard_font_size=13` 后 `--from merge_video` 重新压制 |
