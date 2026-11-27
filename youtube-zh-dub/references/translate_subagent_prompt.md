# 翻译子代理提示词模板

把 `{...}` 换成 gate.json 里的值（`{NN}` 用两位分块号，`{validator}` 里的 `<NN>` 换成分块号本身，例如 `3`）。
一条消息里并行派发所有 `todo` 分块；`description` 写成「翻译分块 NN」。

---

You are translating one chunk of an English video transcript into a Chinese DUBBING SCRIPT (spoken Chinese that a cloned-voice TTS will read, aligned to the original timeline). This is for the user's personal-use Chinese dub.

Files (absolute paths):
- Style guide (read fully first, follow strictly): {guide}
- Video context and glossary (read before translating; glossary entries are mandatory): {context}
- Input chunk: {in}
- Output you must create: {out}

Steps:
1. Read the guide and the context file, then the chunk (items have i, slot_s, gap_after_s, budget, en; context_before/context_after are context only, do not translate them).
2. Write the output file: one JSON object whose keys are the item numbers as strings and values are the Chinese lines, e.g. {"{first_i}": "……"}. Every item i must be present exactly once. Valid UTF-8 JSON; no ASCII double quotes inside values (use 「」 if needed).
3. Validate by running exactly:
   {validator}
4. If it prints FAIL: add missing keys / shorten the listed over-budget lines (keep the meaning and every technical detail), save, and re-run the validator until it prints PASS.
5. Final reply: only the last validator summary (lines / over_budget / PASS). Do NOT include the translations. Do not modify any other files.
