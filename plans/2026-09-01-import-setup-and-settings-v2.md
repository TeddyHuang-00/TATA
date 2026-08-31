# 2026-09-01 import 快速配置 + Settings v2（动态列举/继承徽章/rubric 构建器）+ 布局修复

## 用户决策（2026-09-01 确认）
1. 导入作业：选完 assignment → 快速设置面板（rubric/prompt/provider 下拉，动态列举+默认值）→ 确认后写 config+别名 → 再 fetch。
2. Rubric 构建器：**平行于 Settings 的独立 Screen**（push_screen，仿 Review 先例）；交互 = 逐条添加 criterion（name/desc/rating/grading/pts/custom_scale）。
3. 继承表达：assignment 上下文显示有效值 + 未本地设置时标 "(inherited)" 徽章；手动改即写本地；Reset 恢复。
4. 节奏：P1 → P2 → P3 一次做完，三个逻辑 commit。

## 背景事实（调研结论）
- 导入流：`tata_app.py` `ImportAssignmentModal._do_import` → dismiss(aid) → `_on_assignment_imported` → `_fetch_one`（只写 raw/ + course config [[fetch.assignments]]）→ `_rescan_course`。**从不写 `<course>/<aid>/config.toml`** → `scan_assignments`/`scan_courses`（tata_scan.py）只认带 config.toml 的子目录 → 导入后 Dashboard 不可见。
- 别名：`aliases.py` 三层 merge（global `data/alias.toml` [course] < course `data/<course>/alias.toml` [assignment] < assignment alias.toml [student]）；`_seed_section`（fill-missing）已存在，加公开 wrapper。
- grading 路径解析：`grading.py` `config_path.parents[2] / rubric` → **文件库 = `data/rubrics/*.toml`、`data/prompt/*.md`**，config 里写法是 "rubrics/x.toml"、"prompt/system.md"。
- rubric TOML 形状：`[[criterion]]` name/desc/pts/rating/grading/custom_scale（`rubric.py` `RubricDefinition`/`Criterion`/`Rating`/`Grading`）。
- Settings 现状：`tata_settings.py`（816 行）`_FIELD_SPECS`（grading.rubric/system_prompt/max_parallel_tasks/…）+ provider Select + checkboxes；`_layer_view()` assignment 上下文 = `load_assignment_file` 全量 merged；保存 = delta（edit_config 只写编辑过的 key）；验证 = `config_edit.validate_config_edits`。
- 布局 bug（截图确诊）：`#settings-top` 里 `#ctx-select` 渲染不可见（“Context:” 后空白）；provider 注册表 Static 巨大（7 行）；TabPane 内容不可滚动，44 行下 Save/Reset 被挤出；输入框行高不受控。

## P1 — import 快速配置（feat）
**文件**：`src/aliases.py`（新公开 helper + 测试 `tests/test_aliases.py`）、`src/tata_app.py`（新 `AssignmentSetupModal`；`ImportCourseModal`/`ImportAssignmentModal`/`_on_assignment_imported`）、`tests/tata_modal_check.py`。
- `aliases.py`：
  - `seed_course_alias(assignments_dir: Path, course_id: int, name: str) -> None`：global alias.toml `[course]` fill-missing。
  - `seed_assignment_alias(course_dir: Path, assignment_id: int, name: str) -> None`：course alias.toml `[assignment]` fill-missing。
- `ImportCourseModal._do_import` 成功后：`seed_course_alias(state.assignments_dir, course_id, name)`（name 来自 `_items`，use `_items` tuple）。
- `ImportAssignmentModal._do_import`：dismiss 改为携带 (aid, name)。
- 新 `AssignmentSetupModal(_ImportBase)`（push_screen，回调带 dict）：
  - rubric `Select`：data/rubrics/*.toml 的 basename；默认第一个。
  - prompt `Checkbox` 列表：data/prompt/*.md（文件名）；默认全选？→ 默认勾选全部（当前实际都是全量）；至少 1 个，否则禁用 Import。
  - provider `Select`：`get_providers()` registry names；默认第一个（sorted）；空 → 禁用 Import + 提示去 config/provider.toml。
  - 空库情况给清晰错误（"No rubrics found in data/rubrics — build one in Settings (Rubric builder)"）。
- `_on_assignment_imported`：值变为 (aid, name) 或 setup dict → 保存结果后：
  1. mkdir `<course_dir>/<aid>`（存在即 Already imported 已在 modal 前置拦）
  2. 写 `<course_dir>/<aid>/config.toml`：`[grading]` rubric="rubrics/<file>" system_prompt=["prompt/<file>",...] provider=…（tomlkit；加 header 注释 # schema: ../../config/assignment.schema.json）
  3. `seed_assignment_alias(course_dir, aid, name)`
  4. `_start_job("fetch", _fetch_one, after=_rescan_course)`（不变）
- 约束：UI copy 全英文；`from __future__ import annotations`；无 CJK；ruff 干净；不改其它逻辑。

## P2 — Settings v2（feat）
**文件**：`src/tata_settings.py`、新 `src/tata_rubric.py`（`RubricBuilderScreen(Screen)`）、`src/config_edit.py`（如需要 remove-key 就加 `remove_config_keys(path, {"section": ["key"]})` tomlkit 删除，None 值不写的不动）、`tests/tata_settings_check.py`、新 `tests/tata_rubric_check.py`。
- 动态列举：
  - `grading.rubric`：Input → Select，选项 = data/rubrics/*.toml（值 "rubrics/<file>"）；当前有效值不在列表 → append "… (not in list)"（复用 provider 现模式）。
  - `grading.system_prompt`：Input → Checkbox 列表（data/prompt/*.md，值 "prompt/<file>"）；`_parse`→ list[str]；至少 1 个才合法（GradingSection validator 已有）。
  - prompt 文本样式：label 加 “(multi-select)” 提示。
- 继承徽章：assignment 上下文，对每个 spec：key 不在本地 assignment config（raw `<course>/<aid>/config.toml` 的 section 里无此 key）→ label 追加 `[dim](inherited)[/dim]`；本地有 → 无。仅 assignment 上下文显示。Save 后重新计算。Reset（action_reset 现有“从磁盘重载”）语义不变。
- RubricBuilderScreen（平行 Screen，push_screen）：
  - 顶部：rubric 文件 Select（data/rubrics/*.toml + "New rubric…"）；选 New → 文件名 Input（.toml 自动补）。
  - 中部：criterion 列表（每条：name · rating · grading · pts，可 Remove）。
  - 下部：Add/Edit form：name Input、desc TextArea（固定高度）、rating Select（binary/ternary/likert）、grading Select（standard/strict/round up/custom）、pts Input、custom_scale Input（comma-separated；grading=custom 时启用）；[+ Add] / [Update]。
  - Save：`RubricDefinition.model_validate(criteria)`（校验失败 notify + 定位行）→ tomlkit dumps（`[[criterion]]` 列表）→ `data/rubrics/<name>.toml` → pop 返回。
  - 入口：Settings · Grading tab 按钮 "Rubric builder…"（push_screen）；返回后 `_load_context()` 刷新 rubric/prompt 列表。
- 空 data/rubrics：Settings rubric Select 显示 "No rubrics found…"，rubric 输入改为可打字？→ 简单：Select 仍允许空值（允许 blank）+ 提示去 Rubric builder。
- 测试：fixture 增加 data/rubrics/data/prompt 小样例；断言徽章、Select 选项、save 写单 key；rubric check：添加条目 → save → `get_rubric_definition` 读回一致；custom scale 长度不符 → 校验失败。

## P3 — 布局修复（fix）
**文件**：`src/tata_settings.py`（CSS + compose 结构）、`tests/tata_settings_check.py` 增 layout 断言。
- `#settings-top`：`height: 3`；`#ctx-select` `height: 3; width: 52`（**修复 Context: 后空白 —— 根因：Horizontal 高度塌到 1 行，Select 被 tabbar 覆盖/裁掉**）；title `height: 3` line1 flex。
- `#grading-registry`：固定高度 + 滚动（如 `height: 8; overflow-y: auto;`），避免 7 行顶飞内容。
- 四个 TabPane 内容包 `ScrollableContainer`（允许滚动）。
- 固定行高：`.settings-field Label { height: 1 }`；`Input/Select/Checkbox { height: 3 }`（bordered 可见）；desc TextArea `height: 5`。
- `#settings-actions` 保持 tabs 之后（tabs 1fr），`#settings-status` 最后一行；滚动时按钮/状态常驻。
- 测试断言：`#ctx-select` 的 value 可见（region 非空 + str(value) 非空）；每个 TabPane 内有 `ScrollableContainer`；Save/Reset 按钮 region 在视口内；Input height == 3。

## 验收（每 P）
- `uv run pytest`（+ 改动相关已有测试不红）
- `uv run tests/tata_settings_check.py` / `tests/tata_modal_check.py` / `tests/tata_rubric_check.py`（新）
- ruff：`uv run ruff check src/tata_settings.py src/tata_rubric.py src/aliases.py src/tata_app.py tests/`（+ ruff format）
- headless 截图（120x44）复核布局
- 每 P 一个 jj 逻辑 commit；dev 前移（`jj bookmark set dev -r @`），不推远端。

## 边界（YAGNI 记录）
- 不做 rubric criterion 排序（添加序即序）；不做 prompt 文件内容编辑（$EDITOR 仍可用）；不做 per-field "clear override" 按钮（Reset 即恢复继承）；不做 hooks 表单。
