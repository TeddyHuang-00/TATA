# TATA Feedback v3 修复计划（2026-08-31）

反馈来源：feedback.md（9 项）。现状基线：TUI v2（4 TabPane、RubricBuilder push_screen、native HelpPanel `?` 只开不关、settings v2：prompt 多选/inherit 徽标/Reset=重载）。

## 依赖与批次

```
A1 (tata_settings.py 主战场)        F4 prompt 选择+排序+动态高度
                                    F5 每项独立 reset → 回 inherit
                                    F6 inherit 显示实际值
A2 (新 tab)                         F2 rubric/prompt 编辑入新 tab（Settings 前）
A3 (别名入口)                       F1 课程/作业别名编辑入口
A4 (小改)                           F3 ? toggle + F9 --web + score viewer 修复
A5 (重构, 最后)                     F8 src → shared/cli/tui
```

顺序理由：A1/A2 同改 tata_settings.py（A2 会删 Settings 的 Rubric builder 入口），串行；A3/A4 同改 tata_app.py，串行；A5 全部推翻 import，置于最后，功能在新旧结构上开发，最后统一迁移。

## 设计决策（已定，实现者不必再问）

1. **新 tab 名**：`Library`（资源库：Rubrics + Prompts 两个子 TabPane），放在 Settings 之前。Rubrics 迁移 RubricBuilderScreen 内容（Screen → 非 Screen 组件，**必须用 DEFAULT_CSS，CSS 类属性在非 Screen 上不生效**——教训 c9272e81）；Prompts 用 TextArea 编辑 .md + 文件管理。
2. **F4 排序交互**：_PromptCheckList 每行 = Checkbox + 上移/下移小按钮（`^`/`v`），行高 1（废除 `.settings-field Checkbox { height: 3 }` 全局规则，改为 height: 1 或仅限该列表）；value 顺序 = 行序（system_prompt 顺序有语义）。
3. **F5 reset**：每个设置项加独立 reset 按钮 → 删除该键在本层 config（TOML patch 删除），assignment 层即回 inherit。course/global 层同语义（回到 schema 默认）。全局 `r` 保留（reload from disk）。
4. **F1 别名编辑**：Dashboard course 层 / assignment 层各加 `a` 键 → AliasEditorModal（当前层 [course]/[assignment] 别名：id → name 列表，Input 编辑 + Save）。写入走 aliases.py 新函数（必须 cache_clear，否则 lru_cache 失效），不直接写文件。student 别名不进 UI（fetch 自动 upsert）。
5. **F9 TUI --web**：`run()` 支持 argv（`--web` → Server("uv run tui")），与 score_review._serve_web 同模式；**score_review.py:544 `uv run score-view` → `uv run cli view {score_dir}` 是 bug 根因**（score-view 脚本已被删），连带更新 :2/:524/:549 注释和 docs/design/03-review.md:35。
6. **F6**：以实测为准——字段若已显示 merge 值，则只需让 badge 明确呈现"继承自 X: 值"；若显示的是本地值，则改为显示继承值。实现者读 `_load_context`/_apply_badges 后定。

## 每批验收标准（实现者必须同批更新 headless check）

- A1: `uv run python tests/tata_settings_check.py` + pytest（tata_settings 相关）+ 新增排序/reset/inherit 断言
- A2: `tata_rubric_check.py` / `tata_settings_check.py`（Rubric builder 入口移除）重写为新 tab 断言 + `tata_app_check.py`（tab 数量 3→4）
- A3: `tata_dash_check.py` 加 alias 编辑断言
- A4: `tata_modal_check.py` + `tata_workspace_check.py` 的 `?` 断言改 toggle（按两下验证关闭）；`tata_app_check.py` --web 冒烟
- A5: 全量 pytest + 全 9 个 headless check（just test-e2e）

**门禁（每批必跑）**：`jj diff` 自查、`uv run ruff check .`、全量 `pytest -q`、受影响 headless check。遵循教训 P1：涉及流程变更时同批更新所有相关 headless 检查，不得留到 P2。

## Project conventions（每个实现者 context 都要带）

- VCS: jj；uv（`uv run python ...`）；ruff 格式/检查；Python 3.13；`from __future__ import annotations`
- Textual 8.2.8：非 Screen 组件用 DEFAULT_CSS；BINDINGS 不支持多键 chord
- 注释：无装饰分隔符，English copy（TUI 全英文）
- 修改文件前先 `jj new`（实现者子代理不提交，只改）；jj 提交由编排者做
