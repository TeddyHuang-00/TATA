# Alias 编辑层级重排（2026-08-31）

## 目标
1. global 级（course 列表）按 `a` → 编辑**选中 course** 的单条 alias（`data/alias.toml` `[course]`，key=course_id）
2. course 级（assignment 列表）按 `a` → 编辑**选中 assignment** 的单条 alias（`<course>/alias.toml` `[assignment]`，key=assignment_id 或 dir_name）
3. 删除 workspace（AssignmentScreen）的 alias 入口：`ws-aliases` 按钮、`_open_aliases`、`_on_aliases_saved`、button handler 分支
4. `AliasEditorModal` 改为单条目编辑：key 只读 + name Input；Save 调 `set_alias`（空名=删除，沿用语义）；esc 取消。删除整表展示、add 行、`#alias-empty`、`on_mount`。

## 改动文件
- `src/tui/tata_app.py`：
  - `AliasEditorModal(alias_path, section, key, title)`；compose：key Static + name Input（当前值，无 alias 时为空）
  - `action_edit_aliases`：global 级用 `_selected()` 取 course（course_id None → notify error 返回）；course 级用 `_selected()` 取 assignment（key 规则同 `assignment_display_name`：`str(assignment_id) if not None else dir_name`）；assignment 级 return（workspace 拥有 `a`=Analyze，BINDINGS 无 priority 保持现状）
  - BINDINGS 注释更新
- `src/tui/tata_workspace.py`：删按钮 `yield Button("Aliases", id="ws-aliases")`、aliases 段落、handler 分支
- `src/tui/tata_app.tcss`：删 `#alias-empty` 块；`.alias-row` 保留供单条目用
- `tests/tata_dash_check.py`：重写 `_check_alias_editor_course`（global `a` → 单条目 modal）、`_check_alias_editor_assignment`（course 级 `a` → 单条目 modal，含空名删除断言）
- `tests/tata_workspace_check.py`：删除 `_check_aliases_button` 及其调用（`_check_analyze_key` 保留：workspace `a`=Analyze 且不弹 alias modal）

## 验证
- `uv run python tests/tata_dash_check.py`
- `uv run python tests/tata_workspace_check.py`
- `uv run pytest tests/test_aliases.py`
- 全量 `tests/tata_*_check.py`（门禁）
