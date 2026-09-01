# TATA 设计稿 v1.1 — 01 · Dashboard（S1，三层导航）

> 职责：三层 drill-down 工作台 —— **Global（课程一览）→ Course（课程作业一览 + 跨作业操作）→ Assignment（作业工作台，即原 S2 内容）**
> 版本变更（v1.1 多课程支持）：Dashboard 吸收原 S2 Pipeline Tab；`data/` 目录插入 course 层（`data/<course>/<assignment>`）；配置分层 global→course→assignment
> v1.1 (2026-08-30 update)：Assignment 工作台移除查重 stage（查重仅剩 Course 层 `p` 与 S4 tab），新增 `score review` 按钮（push_screen ScoreReviewScreen）；S4 重构为 course 级 4-tab（详见 04）
> v2 (2026-09-01 update)：导入作业改为两段式 **pick → quick-setup modal → config+aliases → fetch**（ImportAssignmentModal + AssignmentSetupModal，见 §6.2）；导入课程同步种子 `[course]` 别名（seed_course_alias，fill-missing，见 §6.1）
> 对应 CLI：`fetch`、`plagiarism --aggregate`、帮助、`list_courses/list_assignments`

______________________________________________________________________

## 1. 三层导航模型

| 层             | 职责                                            | 动作                                      | 数据扫描                                                                                    |
| -------------- | ----------------------------------------------- | ----------------------------------------- | ------------------------------------------------------------------------------------------- |
| **Global**     | 课程一览（每行 = 一个 course 的聚合状态）       | 导入课程、全局配置                        | 扫描 `data/*/` 中**含 `config.toml` 且其子目录亦含 `config.toml` 的目录**（即 course 目录） |
| **Course**     | 该课程所有作业一览 + 跨作业操作                 | 导入作业、fetch 全部、查重+聚合、课程配置 | `scan_assignments(course_dir)`                                                              |
| **Assignment** | 单作业工作台（信息 + pipeline 操作 + 设置入口） | 原 S2 全部 stage 操作                     | 沿用 02-pipeline.md                                                                         |

导航：**视图切换不是 Tab 切换** —— S1 Tab 内是一个栈式 drill-down（`state.dashboard_level: global|course|assignment`），顶栏面包屑显示路径，`esc`/`backspace` 上钻（Assignment→Course→Global），`enter` 下钻。切到 S4/S5 Tab 再切回时**保留当前层级**（Dashboard 状态不销毁）。

**Tab 结构（v1.1 变化）**：原 4 工作区（Dashboard/Pipeline/Plagiarism/Settings）→ 3 工作区（Dashboard/Plagiarism/Settings）+ Review push_screen。S2 Pipeline 不再有独立 Tab，其内容以「Assignment 视图」存在于 S1 第三层；02-pipeline.md 的组件/协议/增量语义全部原样复用。

## 2. 视图 A · Global（Global 层）线框（≤100 列）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ TATA · Dashboard [Global]   Canvas: OK   Courses: 1   [c]Import course      │
│                            [r]Rescan  [q]Quit   │
├────────────────────────────────────────────────────────────────────────────┤
│ # Course   Assignments  raw  proc  grad  Avg score  Flags  Last run         │
│ ── ─────── ──────────── ──── ───── ───── ────────── ────── ───────────       │
│ 1  271218        6      126  121   121    83.4       2      Today 10:12     │
├────────────────────────────────────────────────────────────────────────────┤
│ ➜ 271218    enter=Course view  c=Import course  g=Global config            │
└────────────────────────────────────────────────────────────────────────────┘
```

### 组件清单（Global）

| 标注     | 组件（Textual 8.x）                                  | 用途                                                                            |
| -------- | ---------------------------------------------------- | ------------------------------------------------------------------------------- |
| 顶栏     | `Static`                                             | Canvas 状态、课程数、快捷键提示                                                 |
| 课程表   | `DataTable`                                          | 行=课程；列：Course/Assignments/raw/proc/grad 聚合计数/Avg score/Flags/Last run |
| 状态列   | `DataTable` 单元格 `Static`（`.pill-*`）             | 课程级聚合徽章（查重疑点：跨作业 flag 总和 >0 显示 `N`）                        |
| 底部     | `Static`（`#dash-status`）                           | 选中行摘要 + 动作提示                                                           |
| 导入课程 | `Button`（顶栏，`import-course-btn`）+ `ModalScreen` | 见 §6.1 交互流                                                                  |
| 全局配置 | `Button`（顶栏，`global-config-btn`）                | 切 S5 并置 Settings 上下文=Global                                               |

**课程聚合行计算规则（供实现）：**

```
课程聚合 raw/proc/grad = Σ 旗下作业计数
平均分 = Σ(作业平均分) / 作业数（有分数的作业）
查重疑点 = Σ 各作业 pair 中 $error 判定数（聚合 z 超 alpha）
最近运行 = max(旗下作业 stage_mtime)
课程识别：data/<dir>/config.toml 存在 且 <dir> 的子目录含 config.toml
        （排除 example/、无 config 的杂目录；排除 data/config.toml 本身）
```

## 3. 视图 B · Course（Course 层）线框（≤100 列）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ TATA · Dashboard [Course: 271218]  [c]Import assignment [F]Fetch all        │
│                                    [p]Plagiarism+aggregate [cfg]Config     │
│                                    [r]Rescan  [q]Quit                       │
├────────────────────────────────────────────────────────────────────────────┤
│ # Assignment           ID      raw  proc  grad  Avg  State       Last run   │
│ ── ──────────────────  ──────── ──── ───── ───── ───── ─────────  ───────── │
│ 1  1-1-begin-quest     2979509   23    23    23   82.1  ● Done     Today 10:12
│ 2  1-10-my-ai-start    2979511   18    18    18    —    ◆ Flagged  Yest 21:03 │
│ 3  0-10-first-colab    2978557   18    17    17    —    ◐ Partial  Yest 20:44 │
│ 4  1-6-first-python    2979480   31     0     0    —    ○ Not run  Never      │
│ 5  1-7-ai-studio-01    2979482    0     0     0    —    ○ Not run  Never      │
├────────────────────────────────────────────────────────────────────────────┤
│ ➜ 1-10-my-ai-start…  enter=Assignment  s=Score review  p=Plagiarism  esc=Global  │
│     Course config: plagiarism overrides global · fetch list: 6 entries      │
└────────────────────────────────────────────────────────────────────────────┘
```

### 组件清单（Course）

| 标注     | 组件                                                                                       | 用途                                                                     |
| -------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| 顶栏     | `Static`                                                                                   | 面包屑首段（课程名·course_id）+ 课程级操作按钮区                         |
| 课程操作 | `Button`×4（`course-fetch-btn`/`course-plag-btn`/`course-config-btn`/`import-assign-btn`） | 跨作业操作（见 §6.2）                                                    |
| 作业表   | `DataTable`                                                                                | 行=作业（**原 Dashboard 表格原样**：ID/四计数/平均分/状态徽章/最近运行） |
| 底部     | `Static`（`#dash-status`）                                                                 | 选中作业摘要 + 课程覆盖状态行                                            |

### Course 层跨作业操作（用户点名，全部走 job 协议）

| 动作       | 键    | 等价 CLI                                              | 行为                                                                                                                                                                                                                                                                                                                                             |
| ---------- | ----- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 导入作业   | `c`   | `fetch` 交互选择                                      | ImportAssignmentModal 选 Canvas 作业 → AssignmentSetupModal（rubric Select 自 `data/rubrics/*.toml`、prompt 多选框自 `data/prompt/*.md`、provider Select 自注册表；默认 第一个/全选/第一个；空库或无勾选 prompt 时 Import 禁用）→ 写 `data/<course>/<id>/config.toml`（`[grading]` + schema 头）+ 种子 `[assignment]` 别名 → 单作业 fetch → 重扫 |
| fetch 全部 | `F`   | `fetch -c data/<course>/config.toml`                  | 拉取 course config 清单全部条目；确认 Modal 显示「将拉取 N 项（M 份提交，缓存跳过）」                                                                                                                                                                                                                                                            |
| 查重+聚合  | `p`   | `plagiarism -c data/<course>/config.toml --aggregate` | 跑全部作业检测 + 跨作业 z-score 聚合（一条命令语义）；完成后自动切 S4 查重屏                                                                                                                                                                                                                                                                     |
| 课程配置   | `cfg` | —                                                     | 切 S5 并置 Settings 上下文=Course（编辑 course config.toml：course_id/`[[fetch.assignments]]`/[plagiarism] 覆盖）                                                                                                                                                                                                                                |

> `[maybe]` 全局聚合（跨课程查重）v1 不做 —— 用户未要求，YAGNI；将来加就是 global 视图一个按钮。

## 4. 视图 C · Assignment（Assignment 层）线框

第三层 = **02-pipeline.md §1 完整线框**，仅头部增加面包屑与返回绑定：

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Global / 271218 / 1-10-my-ai-starting-point [2979511]   esc=Back to course  │
├────────────────────────────────────────────────────────────────────────────┤
│ (full wireframe of 02-pipeline.md §1: status bar + 6 stage buttons +        │
│  config panel + progress row + RichLog; reused verbatim, all-English copy)  │
└────────────────────────────────────────────────────────────────────────────┘
```

Assignment 层新增职责（相对 v1 的 S2）：

- **作业信息条**（复用 02 顶部状态条 + 新增第二行）：目录、course、ID、rubric、provider、计数摘要
- **作业设置入口**（未来，v1 占位）：`[settings]` 按钮 → 标注 `TODO v2: model/hooks/processing form`. v1 用 `e`（`$EDITOR` 打开 config.toml）满足修改需求，设计稿 02 §8 已预留字段
- `esc`/`backspace` → 返回 Course 视图（新绑定；02 原有 `fg` 等键不受影响）

## 5. 键盘映射表（三层合并视图，文案全英文）

| 键                  | 层                | 动作                                 | 说明                                                                                                                                                                         |
| ------------------- | ----------------- | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `↑/↓` 或 `j/k`      | S1 全层           | Move selection                       | DataTable 原生                                                                                                                                                               |
| `enter`             | Global/Course     | Drill down one level                 | Global→Course→Assignment; inside Assignment 'enter' unused (02 has no enter binding)                                                                                         |
| `esc` / `backspace` | Course/Assignment | Drill up one level                   | Assignment→Course→Global; at Global top esc closes Modal or is ignored                                                                                                       |
| `c`                 | Global            | Import course                        | Modal: pick Canvas course → create `data/<dir>/config.toml` + seed `[course]` alias (fill-missing) → enter Course view                                                       |
| `c`                 | Course            | Import assignment                    | Modal: pick assignment → AssignmentSetupModal quick setup (rubric/prompt/provider) → write `data/<course>/<id>/config.toml` + seed `[assignment]` alias → fetch job → rescan |
| `F`                 | Course            | Fetch all                            | Per-assignment cache skip (`.fetch-cache.json`)                                                                                                                              |
| `p`                 | Course            | Plagiarism + aggregate (this course) | `--aggregate` full run; on finish switch to S4                                                                                                                               |
| `cfg`               | Course            | Course config                        | Switch to S5 (context=Course)                                                                                                                                                |
| `g`                 | Global            | Global config                        | Switch to S5 (context=Global)                                                                                                                                                |
| `s`                 | Course            | Score review (selected assignment)   | `push_screen(ScoreReviewScreen)`                                                                                                                                             |
| `r`                 | All layers        | Rescan                               | Global rescans courses; Course/Assignment rescans assignments                                                                                                                |
| `1..9`              | Course            | Filter by state                      | 1=All 2=Done 3=Partial 4=Not run 5=Flagged                                                                                                                                   |
| `q`                 | Global            | Quit                                 | Confirm if a job is running                                                                                                                                                  |

## 6. 交互流

### 6.1 导入课程（Global 层入口，新）

```
[c] → Modal「Import course from Canvas」: Select(list_courses)     # 后台线程预载
    → 确认: 创建 data/<dir>/config.toml（[fetch].course_id=…）
       + seed_course_alias（fill-missing：data/alias.toml 的 [course] 表写入 Canvas 课程名，已有别名不覆盖）
       <dir> 默认 = course_id（如 "271218"），Modal 内 Input 可自定义（唯一性校验：重名报错）
    → 进入 Course 视图（若该课程已有作业目录则扫描显示；否则空态提示 [c] 导入作业）
```

> 目录创建只写一份最小 course config（course_id）；作业清单 `[[fetch.assignments]]` 由后续「导入作业」累积。course config 是 gitignored（`data/*`），与 v1 根配置同语义。**目录名约定：默认 course_id（用户 2026-08-29 确认），可自定义；迁移后课程显示名 = 目录名（或在目录名后附 course_id）。**

### 6.2 导入作业 / fetch 全部 / 查重+聚合（course 内）

```
Course 视图 [c] → ImportAssignmentModal(Canvas 作业 Select)      # 后台线程预载；目录已存在(重复导入)报错
    → (aid, name) → AssignmentSetupModal「Assignment quick setup」（v2 新）
         rubric:   Select 枚举 data/rubrics/*.toml（值 "rubrics/<file>"），默认第一个
         prompt(s): Checkbox 多选 data/prompt/*.md（值 "prompt/<file>"），默认全选
         provider: Select 枚举 config/provider.toml 注册表，默认第一个
         Import 禁用条件：任一库为空（rubrics/prompt/provider）或未勾选任何 prompt
    → 确认 → 创建 data/<course>/<dir>/config.toml，首行 "# schema: ../../config/assignment.schema.json"
             + [grading] = { rubric, system_prompt[], provider }
             # 写入目标：assignment 级 config（非 course config 的 [[fetch.assignments]]）
    → seed_assignment_alias（course 级 data/<course>/alias.toml 的 [assignment] 表，fill-missing）
    → job: _run_fetch(FetchCliOptions(course, assignment, config=course_config))
    → fetch 完成后 M3 追加：course config 的 [[fetch.assignments]] 补 { id: <aid> }（dedup，供 F 拉取）
    → 完成 notify + 重扫（_rescan_course）
Course 视图 [F] → Confirm Modal「Fetch course 6 assignments (126 submissions, cached skipped)」
    → job: _run_fetch(FetchCliOptions(config=course_config))   # 等价 CLI course config fetch
    → 完成 notify(成功 N/失败 M) + 行徽章更新
Course 视图 [p] → Confirm Modal「Run plagiarism for all assignments + cross-assignment aggregate」
    → job: detect_plagiarism(course_config, aggregate=True, quiet=True)
           # quiet：不打印纯文本报告；S4 只读 JSON（各作业 all_pairs.json + 课程聚合 aggregate.json）
    → 完成 notify + S4 视图刷新（若用户停留在 Dashboard，徽章更新）
```

### 6.3 下钻/上钻状态保持

```
Global enter → state.dashboard_level=course, state.current_course=选中 → 渲染 Course 视图
Course enter → state.dashboard_level=assignment, state.current_assignment=选中 → 渲染 Assignment 视图
Assignment esc → 回 Course（工作台 state 销毁，重进时预扫描增量）
Course esc → 回 Global（current_course 保留，current_assignment 置 None）
```

### 6.4 跨课程查重（Global 层）— NOT IN SCOPE：已按用户决定移除，以下为历史设计，不实现

```
Global 视图 [p] → Confirm Modal「Run plagiarism across ALL courses (N courses, schedule-able)」
    → job (exclusive): 
        ① 对每个 course config 跑 detect_plagiarism(course_config, aggregate=True)（含检测+本课程聚合，复用 CLI 语义）
        ② 调用新入口 cross_course_aggregate(course_configs) —— 读各课程 all_pairs.json，
           用现有 src/plagiarism_aggregate.py 的 logit/z/Stouffer 逻辑做全局合并
           （复用 BuildConfig.pair_data_files 的构造方式，只是 pair 文件跨课程路径）
        ③ 写 data/plagiarism-cross-course.json + 报告（暂定存 Global root）
    → 完成后主动切 S4 显示跨课程聚合视图；其余时间 S4 顶部提供「跨课程」Tab（见 04)
```

> 跨课程输出位置与 CLI 形式：新增 `main.py plagiarism --cross-course`（无 `-c`，扫描全部课程）；TUI 按钮等价该命令。实现上在 `src/plagiarism_aggregate.py` 增加一个接受多课程 pair 列表的入口（现有核心统计函数复用，仅输入装配不同）。

## 7. 空态 / 错误态 / 加载态

| 态                                    | 表现（**文案全英文**）                                                                                                                   |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **Empty·no courses**                  | Global 表格区居中 Static：「No courses yet. Press `c` to import one from Canvas, or drop an `data/<course>/config.toml` and press `r`.」 |
| **Empty·course without assignments**  | Course 表格区 Static：「No assignments in this course yet. Press `c` to import.」                                                        |
| **Empty·no .env**                     | Global 顶栏 `Canvas: ✗ (.env missing)`，`c` 禁用；提示去 S5·Global 配置。                                                                |
| **Loading·scanning**                  | 各层首帧 `Static`「Scanning…」，\<100ms 通常一闪而过                                                                                     |
| **Error·fetch partial failure**       | notify(warning, "Fetched N/M"), 行徽章照实                                                                                               |
| **Error·aggregate data inconsistent** | 状态行注明跳过缺失的作业（Skip: <assignment> (missing pairs)）                                                                           |

## 8. 迁移路径（现有单课程数据 → 多课程形态；**拷贝迁移，验证通过后再删除原目录**）

```
# 步骤 1: 拷贝（不破坏原数据）
mkdir data/271218
cp -r data/0-10-* data/1-1-* data/1-6-* \
      data/1-7-* data/1-10-* data/1-11-*  data/271218/
cp data/config.toml data/271218/config.toml
# 步骤 2: 验证（确认运行无误）
#   - 每个作业 config: uv run python -c "from src.assignment_config import load_assignment_file; load_assignment_file('data/271218/<name>/config.toml')" 全部通过
#   - 课程枚举: 新 scan_courses() 正确识别 271218、各作业计数一致
#   - CLI 冒烟: fetch -c data/271218/config.toml --retry 空跑/缓存命中无错误；plagiarism --aggregate 能读到作业
# 步骤 3: 确认后删除原位置（仅在验证全过后；用户 2026-08-29 明确：确认运行无误后再删）
rm -r data/0-10-* data/1-1-* data/1-6-* \
      data/1-7-* data/1-10-* data/1-11-* data/config.toml
```

- `[[fetch.assignments]]` 条目存 `id`（无 `out`）：fetch 输出目录恒为 `<course dir>/<id>/raw`（派生于 id，不存储）
- 迁移后 `data/config.toml` 若保留 = **global config**（可选，全局默认；无它时三层退化为两层）
- course 目录名：默认 course_id（`271218`），任意唯一目录名 + config 内 course_id 亦可
- 现有 `find_root_config`（`parent.parent/config.toml`）在迁移后**自动**指向 course config，无需修改；`load_assignment_file` 需增加第三层合并（global，可选）

## 9. 配置分层契约（v1.1 核心）

```
data/
├── config.toml              # GLOBAL（可选）：全局默认值（[plagiarism] 默认）
├── ITCS5153/                # COURSE 目录（识别：含 config.toml 且子目录=作业）
│   ├── config.toml          # COURSE 配置：course_id、[[fetch.assignments]]、[plagiarism] 覆盖
│   └── 1-6-first-python/…   # ASSIGNMENT：grading/processing/hooks/scoring（覆盖 course）
└── example/                 # 模板（保持 gitignored 例外）
```

**合并顺序：GLOBAL < COURSE < ASSIGNMENT（per-key 覆盖，逐 section 浅合并，沿用 `_merge_configs`）。**

- `load_assignment_file`：先找 course root（现状 `find_root_config` 逻辑），再向上找 global root（`course.parent/config.toml`，可选）；两层合并后同现状
- `is_root_config`：语义更新 = 「course root」（其子目录是作业）；global root 不参与 CLI（`fetch/plagiarism -c` 接受 course config；global config 仅 TUI 全局默认 + S5·Global）
- 全部路径仍相对作业目录解析（`resolve_*` 不变）

## 10. 集成影响（实现清单，供派活）

| 文件                           | 变更                                                                                                                             |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `src/assignment_config.py`     | `find_root_config` 保持；新增 `find_global_config` + 三层 `_merge_configs`；`is_root_config` 文档语义更新                        |
| `main.py` 或新 `src/scan.py`   | `scan_assignments()` 升级：输入 course_dir，汇总按 course 分组；global 视图聚合计算                                              |
| TUI `tata_app.py`              | `AppState` 加 `courses`、`current_course`、`dashboard_level`；S1 三层视图类（`DashboardScreen` 三态切换 + 面包屑 `#breadcrumb`） |
| `docs/design/00-ia.md`         | 屏幕清单、导航图、状态模型（本次同步更新）                                                                                       |
| `docs/design/02-pipeline.md`   | 头部面包屑 + `esc` 返回绑定 + 「作业设置 v2 占位」                                                                               |
| `docs/design/05-settings.md`   | 写入目标三层化；上下文选择器（Global/Course/Assignment）                                                                         |
| `docs/design/04-plagiarism.md` | 顶部课程上下文；`a` 键语义改为 course config；空态文案                                                                           |
| 迁移脚本                       | `scripts/migrate_courses.sh`（一次性；或手动 mv + 验证，见 §8）                                                                  |
