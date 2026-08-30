# 2026-08-31 fetch 全类型自动收集 + 多文件预处理合并

## 目标（用户原话要点）
1. 所有 fetch 自动识别类型，彻底去除 mode 相关配置（FetchSection.mode / FetchAssignmentEntry.mode / FetchCliOptions.mode / 导入弹窗 RadioSet / 单文件 `_resolve_mode`）。
2. 学生多类型提交（如文本框 html + 附件 ipynb）：在 `raw/<uid>/` 新建文件夹，内容放入其中；单文件提交保持平铺（现状）。
3. 预处理自动区分单文件/文件夹；每文件按后缀自动识别类型（ipynb/html/txt/md/docx）。
4. 多文件学生：各文件分别转换 → 加 header（文件名 + 提交时间如可用）→ concat 成单个 `<uid>.md`（如 html 部分在前、ipynb 部分在后）。

## fetch 侧（src/canvas_fetch.py）
- 删 `FetchMode`/`_resolve_mode`；`fetch_assignment(canvas, course_id, assignment_id, out)` 无 mode。
- 每个 submission 收集两部分：body（非空时 `<uid>{_LATE_0}.html`，现有命名）+ attachments（`<uid>{_LATE_i|_i}.{ext}`）。
  - 命名冲突规避：attachment i=0 且该 sub 同时有 body → 强制 `_0` 后缀（否则 html 附件与 body `<uid>.html` 同名）。
  - 文件数 = 1 → 平铺 `out/<name>`；> 1 → 文件夹 `out/<uid>/<name>`（>1 即多文件，无论是否不同类型；单文件不变）。
- `.fetch-cache.json`：key = 文件名（uid 前缀保证 per-assignment 唯一），value = stamp（attachment/body 的 updated_at）；cache 仍放 `out/.fetch-cache.json`；下载跳过逻辑不变（dest 移到子目录无影响 — dest 名仍是 key）。
- rows 改为 per-student：{user_id, user_name, sortable_name, file: 第一个文件名或 ""}；print 改为 `f"auto: {n} submissions -> {out}/ ; alias -> {alias_path}"`，n = file 非空的学生数。alias upsert 不变。
- `remember_fetch` → 已存在 `remember_course_fetch(cfg, *, course_id=None, entry=(aid, mode)|None)`：把 `entry` 简化为 `assignment_id: int | None = None`（不加 mode），追加 `{id}`，去重按 id（含 legacy assignment_id），保留其它键。

## 配置侧（src/assignment_config.py / cli_options.py）
- `FetchSection`：删 `mode`（course_id / assignments）。
- `FetchAssignmentEntry`：删 `mode`（仅 `id`，仍 AliasChoices("id","assignment_id")）。
- `FetchCliOptions`：删 `mode`；`--mode` 相关 validator/警告全删；help 文本同步。

## CLI（src/cli.py）
- 删 `_entry_mode`；`_fetch_entries`/`_fetch_course` 不再有 mode 参数；`_run_fetch` 删 mode 解析（args.mode 分支、警告、`mode = ...` 块）；`_remember` 的 entry 改为 assignment_id（不传 mode，记住时永不写入 [fetch].mode — 该字段已不存在）。
- 其余（容器判定、单作业派生 `<id>/raw`、非数字目录名提示、--retry 只走清单）保持。

## 预处理（src/processing.py）
- raw 顶层条目 = 文件（单）或目录（多文件，非点目录）。自动模式（`processing.input_format` 未配置）下 **per-file 检测格式**（当前是按第一个文件检测单一格式，须改）；显式配置格式时：顶层文件按现有 glob 过滤，目录内文件按后缀 ∈ 配置格式过滤（保持向后兼容）。
- "No supported files" 门：顶层支持的扩展名或目录内含支持的扩展名，任一满足即可。
- 处理循环按 ITEM（文件/目录）：
  - 文件 → 现有单文件路径（`_process_single_file` → `<stem>.md`，无 header，不变）。
  - 目录 → 每个文件（排序：name）分别 `_process_single_file` 到临时 md（tempfile 于 processed_dir 或系统临时目录），读回文本；按序 concat：
    ```
    ---
    <!--- file: <文件名>, submitted: <stamp> -->

    <该文件 md 内容>
    ---
    <!--- file: <文件名2>, submitted: <stamp2> -->

    <内容>
    ```
    stamp 优先 `.fetch-cache.json` 中该文件的 updated_at（若存过），否则文件 mtime，无法获取则省略该行。输出 `<uid>.md`（目录名即 stem），删临时文件。
  - 单文件不套 header（保持现状）；目录中单个文件也走 concat 路径吗？——目录即多文件场景，一律 concat（含 header），目录内只有 1 个文件时同样 concat（header + 单段）也 OK（规则简单）。
  - 每个实际输入文件仍触发 before/after_preprocess_file hook（output_file 传最终 `<uid>.md`；docstring 注明多文件项是最终 concat 文件）。
- processed_count 按时：文件 item 计 1；目录 item 计 1（每学生 1 个 md）——总计数与 raw 学生数对齐。

## 扫描（src/tata_scan.py）
- `count_files` 保持（processed/graded 用）；raw 计数改为顶层 items（文件+目录，排除点项）：新增/内联 `raw=sum(1 for p in entry.iterdir() if not p.name.startswith(".") and (p.is_file() or p.is_dir()))`；避免把 `.fetch-cache.json`、plagiarism 等误计（raw 目录内只有这些 + 学生目录）。`_is_fetched` 不变。

## TUI（src/tata_app.py / tata_workspace.py / tata_settings.py）
- ImportAssignmentModal：删 `#modal-mode` RadioSet + `_do_import` mode 解析；dismiss(aid)；`_on_assignment_imported` 收 aid（int 而非 tuple）；`_fetch_one(course, aid)`：`FetchCliOptions(course=..., assignment=aid, config=course.config_path)`（无 mode/out）；M3 append `{"id": aid}`。
- `_fetch_all_section`/`_on_fetch_all_confirmed`：`mode=entry.mode or cfg.mode` 全删；FetchCliOptions(course=course_id, assignment=entry.id, config=config_path)。
- `tata_workspace`：无 mode 引用（检查 `_run_fetch_job`）。
- `tata_settings`：fetch list summary 只显示 id（entry.get('id') or entry.get('assignment_id')）。
- 头部/状态文案检查 "attach/text/auto" 出现处。

## 测试
- tests/test_canvas_fetch.py：mode 参数删除；新增多类型场景（body+attachment → 文件夹；双附件 → 文件夹；单附件/单 body → 平铺；html 附件 + body 命名不冲突；cache key 不因文件夹改变）。
- tests/test_fetch_cli.py / test_layered_config.py / test_multicourse.py / test_aliases.py：所有 mode 引用删除；FetchSection/FetchAssignmentEntry 新形状；remember_course_fetch 新签名。
- tests/e2e_common.py：entries 只 {id}；assignment config 无 [fetch]（已无）。
- tests/tata_*_check（fetchall/dash/modal/workspace/settings）：无 mode 断言；import modal 交互（无线电组 → 只剩 assign 选择）；fetch-all 的 FetchCliOptions 无 mode；settings 列表 id-only。
- tests/test_processing.py：新增目录多文件 concat 测试（html+ipynb → 一个 md，含 file/submitted 标注，顺序按文件名）；单文件不变回归；显式 input_format 兼容回归。
- tests/test_tata_scan.py：raw 计数含文件夹。

## 数据迁移（gitignored）
- `data/271218/config.toml`：`[fetch]` 只留 `course_id`；entries 只有 `id`（删 mode 行；mode="text" 的 3 个 entry 去 mode）。
- 现有 raw 布局不动（当前 2978557/2979480/2979482 是单文件学生为主；重建仅需在有 >1 文件的学生时重 fetch；不批量重抓）。
- 备份 /tmp/tata-alltypes-backup-20260830。

## 文档
- README.md（--mode 移除、多文件目录、concat 说明）、docs/config/assignment.md、docs/design/01-dashboard.md/05-settings.md、data/example/config.toml 注释、HERMES.md（gitignored，一并改）。

## 验收
- pytest 全绿（基线 125；预期 ≥125）；ruff format/check（仅既有 3 错误）；8 check 脚本 OK。
- 构造临时 assignment（fixture）验证：raw 下 1 文件平铺 + 1 学生双文件（html+ipynb）→ processed 2 个 md，双文件 md 含两段 header/内容。
- 真数据冒烟：`fetch -c data/271218/config.toml`（重建 raw，检查目录出现与否合理，计数 ~56/55/55/56/53/54）+ 2979482（多文件学生 → 目录）。
