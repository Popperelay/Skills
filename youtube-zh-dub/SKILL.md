---
name: youtube-zh-dub
description: 仅在用户明确要求为 YouTube 视频或本地英文视频制作或继续制作中文配音时使用，例如“给这个视频配中文”。单独发送链接、请求总结、翻译文字或制作字幕均不触发。
---

# youtube-zh-dub

工具负责全部确定性工作（下载、分离、识别、断句、切片、克隆合成、质检重试、对齐、混音、压制、软字幕封装），
**不消耗对话 token**；本 skill 只处理工具返回的关口。和 `video-localization-dubbing`（另一套代码库的知识型专家团）不同：要**实际产出成片**时用本 skill。

## 启动与分享

适用于 Windows x64，需要兼容 CUDA 12.8 的 NVIDIA 显卡和驱动。分享时复制整个技能文件夹到接收方的 `%USERPROFILE%\.codex\skills\` 下。配音程序在 `tool/`。运行 `doctor` 或 `run` 前，启动器会检查本机运行环境和四组默认模型。已就绪的组件直接复用，缺失时从上游下载到 `%LOCALAPPDATA%\YouTubeZhDub`。已有离线版的解包目录也可直接复用。首次下载需要联网，后续本地文件配音可复用缓存。

从本技能目录启动预检。以下命令中 `$CMD` 代表同一启动方式，替换最后的子命令即可。传给 `run` 的本地视频和 Cookie 文件使用绝对路径。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\dub.ps1 doctor
```

工作目录默认为 `%LOCALAPPDATA%\YouTubeZhDub\work`，可通过环境变量 `YOUTUBE_ZH_DUB_WORKDIR` 指定其他绝对路径。继续旧任务时，将它指向原来包含 `sessions` 的目录。会话位于工作目录的 `sessions/<id>/`，成片位于 `output/`。每次 `run` 结束后读会话的 `gate.json` 和 stdout 的最后一行 `GATE {...}`。

## 流程

1. **预检**（本会话首次使用时）：`$CMD doctor`，看最后一行 `DOCTOR {...}`。`ready=false` → 查 `references/troubleshooting.md`。
   `vram_free` 只是警告：其他程序占显卡会拖慢合成，**不要替用户关程序**，告诉他即可。
2. **运行**：`$CMD run "<URL 或本地文件路径>"`（续跑：`$CMD run --id <id>`），用 exec_command 启动并保留会话 ID，通过 write_stdin 等待退出，
   等待期间无需反复读取日志。常用参数：
   - `--formats soft` / `hard` / `hard,soft`（默认两者都要）
   - `--set qa.gate_ratio=0.05` 这类临时覆盖；`--from <stage>` 强制从某阶段重跑
   - `--asr-prompt "..."` 手动给识别提示词（默认用标题/标签/简介自动生成）
3. **按退出码处理**（同时读 gate.json）：

| 退出码 | gate | 处理 |
|---|---|---|
| 0 | done | 进入第 4 步 |
| 20 | cookies | YouTube 要求登录。见下方「cookies」 |
| 10 | translate | AI 翻译。见下方「翻译」 |
| 30 | rewrite | AI 改写少数难念句。见下方「改写」 |
| 1 | error | 看 `category/hint/tail`，查 `references/troubleshooting.md`，修好后重跑**同一条命令**（自动从失败阶段续跑） |

4. **交付**：从完成时的 `gate.json` 读取 `export_dir` 和 `outputs`，用绝对路径链接给出成片文件夹和推荐的中文配音 MKV。MP4、硬字幕版和三份 SRT 也使用实际导出路径。用 `$CMD status --id <id>` 取 QA 摘要。
   导出目录和文件名使用 YouTube 的视频标题或本地原视频的文件名（去扩展名），例如 `output/原视频名/原视频名.中文配音.mkv`。遇到已完成但尚未按标题导出的旧会话，可用工具目录下的 `dub/s10_export.py --id <id>` 单独导出已有成片，并从 `metadata/export.json` 取交付路径。
   回复里说明：字幕轨可选「关闭字幕 / 中英双语 / 中文 / English」，默认关闭；音轨 1 中文配音、音轨 2 英文原声。
   中间产物占用大时可提示 `$CMD clean --id <id>`（只列清单；`--yes` 也只是挪进 `工作目录/trash/`，删除前必须让用户确认）。


### cookies（exit 20）
- 本机出口 IP 常被 YouTube 判为机器人；Chrome/Edge 新版启用了**应用绑定加密**，工具读不出浏览器 Cookie，**不要尝试绕过**。
- 请用户二选一：①浏览器装「Get cookies.txt LOCALLY」，登录后在 youtube.com 导出到 `工作目录/cookies.txt`（建议小号）；②自己下载好视频，给本地路径（走本地文件流程）。
- 拿到后：`$CMD run "<URL>" --cookies "工作目录/cookies.txt"`（后台）。下载成功后提醒用户清理其导出的 cookies.txt。

### 翻译（exit 10）
gate.json 给出：`guide`、`context`、`glossary`、`validator`（含 `<NN>` 占位）、`todo`（每个未通过分块的 `chunk/in/out/first_i/status`）。
1. 术语表（可选，长视频或术语多时做）：读 `glossary.json` 前 30 条，只给**非代码**且有歧义的词补 `zh`（代码标识符保持空），写回。小于 10 分钟的视频跳过。
2. **并行派发子代理**（用 spawn_agent，每块一个，按可用槽位分批），提示词用 `references/translate_subagent_prompt.md`，把 gate.json 的字段原样填进去。
   子代理只回报校验摘要，**不要**把译文带回主对话。
3. 有分块不是 PASS → 将失败摘要通过 followup_task 交给对应子代理再改一次。
4. 全部 PASS 后后台 `$CMD run --id <id>` 续跑（翻译之后的合成最耗时：约 0.8× 视频时长）。

### 改写（exit 30）
重试三轮、择优、短句回退原声之后，仍有超过 3% 的句子回听错字多才会停在这里。
1. 读 `metadata/qa_report.json` 的 `remaining`（每项 `i, src, dst, hyp, cjk_cer, slot_s, dur_s`），只看这些行。
2. 对每行写更好念的中文：代码标识符拆成能念的形式，没有元音的缩写拼读或写全称，`dur_s` 明显大于 `slot_s` 就缩短；
   纯代码念白或极短的句子可以写 `"__ORIGINAL__"`（保留讲者原声，字幕照常）。
3. 写到 `metadata/rewrites.json`：`{"123": "新的中文", "456": "__ORIGINAL__"}`，然后后台 `$CMD run --id <id>`。
   工具只重做这些句子；默认最多改写一轮，之后剩余问题句以 `accepted_with_warnings` 放行。

## 省 token 的纪律
- 不读完整日志、译稿、QA 明细；只读 gate.json、`status` 和日志尾部。
- 长任务保留执行会话并等待退出；不要中途打印进度。
- 不手动拼阶段脚本命令；需要单独重跑时用 `--from <stage>`。

## 耗时参考（RTX 5080，2 小时视频实测）
下载约 2 分钟，人声分离约 8 分钟，识别约 3.5 分钟，翻译（11 个子代理并行）约 5 分钟，合成约 90 分钟
（其他程序占用显卡时会慢 1.5 倍左右），质检和重试约 30 分钟，对齐约 8 分钟，硬字幕压制约 7 分钟，软字幕封装不到 1 分钟。
