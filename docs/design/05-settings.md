# TATA 设计稿 v1.1 — 05 · Settings（S5）

> 职责：provider、Canvas、plagiarism 权重、路径与 schema 生成的集中配置编辑；**上下文选择器：Global / Course / Assignment 三级**（右上 Select）
> v1.1 变更：写入目标由「根配置/作业配置」两层改为**三层**：global config（`data/config.toml`，可选，跨课程默认）、**course config**（`data/<course>/config.toml`：course_id/`[[fetch.assignments]]`/[plagiarism] 覆盖）、assignment config（`data/<course>/<name>/config.toml`：`[grading]`/`[plagiarism]`/`[assignment]`/`[processing]`）；.env 仍全局
> 上下文来源：`state.current_course` / `state.current_assignment`；从 S1 三层的 `cfg`/`g` 进入时预置（Global 视图 `g`→Global 上下文；Course 视图 `cfg`→Course 上下文；Assignment 视图 `e`→Assignment 上下文）
> 保存逻辑：全屏右侧显示「将写入: 文件路径」；内容经 `load_assignment_file` 校验通过才落盘（pydantic 错误逐条展示）
> v2 (2026-09-01 update)：rubric/prompt/provider 三字段全部改为本地库枚举（Select / 多选框）；Assignment 上下文显示 `(inherited)` 继承徽章；新增 RubricBuilderScreen（§7）；布局契约与 `DEFAULT_CSS` 陷阱见 §8
> v4 (2026-09-01 update)：context 下拉显示 `Alias (id)`；Canvas tab 改为可编辑（url/token 写 `.env`、遮蔽预览、Save/Reload，测试连接保留）；provider 注册表编辑迁至 **Library tab → ProvidersPane**（Settings 只读展示）；`.prompt-row` 高度修复（行高 `auto`，见 §8.3）

---

## 1. ASCII 线框（≤100 列）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Settings · Context: [Course: 271218 ▾]                                      │
├──┬─────────────────────────────────────────────────────────────────────────┤
│  │ ⚙ Grading (assignment config)                                          │
│  │ provider      [deepseek_chat_tool        ▼]   max_parallel [ 10 ]      │
│  │ registry      [deepseek_chat_tool · base_url · model …] (read-only)    │
│  │               → 编辑在 Library tab → Providers pane                     │
│  │ ────────────────────────────────────────────                            │
│  │ rubric        [Select: data/rubrics/*.toml] (inherited)                 │
│  │ system_prompt [☑ prompt/system.md  ☑ prompt/grade.md …]                 │
│  │ ────────────────────────────────────────────                            │
│  │ Will write: data/271218/1-10-my-ai-start/…/config.toml [grading] │
├──┴─────────────────────────────────────────────────────────────────────────┤
│ [ctrl+s Save]  [Validation: OK]  [r Reset]                                 │
└────────────────────────────────────────────────────────────────────────────┘
```

> Tab 页：① Grading ② Canvas ③ Plagiarism ④ 路径与高级。除 Canvas 外都针对 `state.current_assignment`；未选课程时只显示 Global/.env 项（见 §2.5）。

## 2. 组件清单

| 标注 | 组件（Textual 8.x） | 用途 |
|------|--------------------|------|
| 上下文选择器 | `Select`（右上 `#ctx-select`） | （v4）选项显示 `Alias (id)`（`course_display_name`/`assignment_display_name`；无别名回退 `dir_name`）；Global / Course / Assignment；切换即重新载入对应层 config |
| Tab 容器 | `TabbedContent` + `TabPane`×4 | 分区编辑 |
| 字段 | `Input`（api_key 用普通 `Input`——注册表已迁 Library；Canvas token 用 `_SecretInput`：聚焦明文、失焦遮蔽预览 head 4 + 8 星 + tail 4，≤8 字符全 8 星） | 文本/密钥 |
| 枚举 | `Select`（provider/model/mode 项） | 有限选项（provider 注册表动态加载，**只读**） |
| 布尔 | `Checkbox`（remove_base64_images 等 `[processing]` 开关，④ 页批量） | 处理选项 |
| 单选 | `RadioSet`+`RadioButton`（chat/reasoning mode） | 互斥模式 |
| 状态行 | `Static`（`#settings-status`） | 校验结果（通过/错误摘要）+ 目标文件路径 |
| 操作 | `Button`（保存/重置/生成 schema/测试连接/打开目录） | 动作 |
| 错误 | `notify` + `ModalScreen`（校验失败详情列表） | pydantic 逐条错误 |

## 2.5 上下文规则（三层写入目标）

| 上下文 | 可编辑内容 | 写入目标 |
|--------|-----------|---------|
| **Global** | 环境 `.env`（`CANVAS_BASE_URL`/`CANVAS_ACCESS_TOKEN`，Canvas tab 编辑 + Save .env）；global config 可选的跨课程默认（[plagiarism] 权重/阈值）。（v4：provider 注册表编辑迁至 **Library tab → ProvidersPane**——此处仅 `#grading-registry` 只读展示） | `.env` / `data/config.toml` |
| **Course** | `[fetch]` course_id、`[[fetch.assignments]]` 只读摘要、`[plagiarism]` course 级覆盖 | `data/<course>/config.toml` |
| **Assignment** | `[grading]`（provider/rubric/prompt/parallel）、`[assignment]` 目录、`[processing]`、`[plagiarism]` | `data/<course>/<name>/config.toml` |

> 未选作业时 Assignment 上下文禁用（只显示 Global/Course）——与 v1 行为一致但扩展为「未选课程」亦然。Course 上下文字段与 v1 根配置页相同（Canvas 页从「根配置」改名为「课程配置」）。

## 3. 各页字段与写入目标

### ① Grading（作业级 `[grading]`）
| 字段 | 控件 | 写入键 |
|------|------|--------|
| provider | `Select` | `grading.provider` |
| max_parallel_tasks | `Input`(数字) | `grading.max_parallel_tasks`（1..10） |
| rubric | `Select`（枚举 `data/rubrics/*.toml`，值 `"rubrics/<file>"`） | `grading.rubric` |
| system_prompt | `_PromptCheckList`（检查框列表，枚举 `data/prompt/*.md`，值 `"prompt/<file>"`，多选） | `grading.system_prompt`（str 或 list） |

> **Assignment 上下文继承徽章（v2）**：键未在**本地 assignment config.toml 原始文件**（`read_config(assignment.config_path)`）中显式设置时，字段标签追加 `(inherited)` 徽章。local 键集来自原始文件而非 global/course 分层合并视图——合并值照常作为字段值显示，但徽章指明「此值来自上层继承」；本地已设置则无徽章。

### ② Canvas（.env + course config）
| 字段 | 控件 | 写入 |
|------|------|------|
| `#canvas-env` | `Static` | 显示 `.env` 状态 + masked token（`mask_secret` 预览：head 4 + 8 星 + tail 4，≤8 字符全 8 星） |
| CANVAS_BASE_URL | `Input`（`#canvas-url`） | `<root>/.env`（Save .env 键） |
| CANVAS_ACCESS_TOKEN | `_SecretInput`（`#canvas-token`，聚焦明文、失焦遮蔽） | `<root>/.env`（Save .env 键） |
| Save .env / Reload .env | `Button`（`#btn-save-env` / `#btn-reload-env`） | dotenv `set_key` 写这两个键（保留其它键与注释；缺失时创建；**只覆盖 CANVAS 两键**） |
| Test Canvas connection | `Button` + `t` 键 | 后台线程 `list_courses`（保留，行为不变） |
| course_id | `Input`(数字) | course config `[fetch]` |
| `[[fetch.assignments]]` 列表 | 只读摘要（`Static`，含 id） | 该清单的增删挪到 S1·Course「导入作业」——本页只展示，避免双入口编辑 |

### ③ Plagiarism（作业级 `[plagiarism]`）
| 字段 | 控件 | 说明 |
|------|------|------|
| copydetect_weight / embedding_weight | `Input`(0..1) | **保存时校验和为 1**（不符合 → 行红 + 阻止保存） |
| embedding_model | `Input` | 默认 `jinaai/jina-embeddings-v5-omni-small…` |
| pairwise_alpha / individual_alpha | `Input` | 聚合显著性阈值（默认 0.01） |
| score_floor / score_cap | `Input` | logit 变换上下限 |
| display_threshold | `Input` | 单作业显示阈值（默认 0.8） |
| extensions | `Input`(逗号分隔) | `.py` 等 |

### ④ 路径与高级
| 字段 | 控件 | 说明 |
|------|------|------|
| raw/processed/graded/logs 目录 | `Input`×4 | `[assignment]` 段 |
| reference_file / template_file | `Input` | 可空 |
| `[processing]` 开关组 | `Checkbox`×12 | strip_canvas_suffix、remove_base64_images 等（**仅展示常用 6 项，其余折叠**） |
| 生成 schema | `Button` | 复用 `generate_all_schemas`；完成后 status 行列出生成文件（等同 CLI `schema`） |

## 4. 交互流

```
进入 S5（Tab 切到 settings）
  → 无 current_course: 显示 「未选中课程——仅 Global 页可编辑；前往 Dashboard 进入课程后重进。」
     Global 上下文可编辑 .env（Canvas tab，Save .env）；Course/Assignment 字段禁用
  → 有 current_course 无 assignment: Course 上下文可编辑；Assignment 字段禁用
  → 载入当前值（对应层 config 分层合并结果）+ 「将写入: <path>」 说明
编辑 → ctrl+s 保存
  → 组装 TOML → 用 load_assignment_file 校验（含分层合并后再校验）
  → 通过: 写盘(仅改动的段) + notify(success) + status 行「已保存」
  → 失败: Modal 列出全部 pydantic 错误(字段: 原因) + 对应 Input 红边框，不写盘
测试连接(Canvas 页) → 后台线程 list_courses → 成功: 「连接正常, 课程 N 个」; 失败: 红字原因
生成 schema → 后台线程 → 成功: 列出路径; 失败: notify(error)
```

## 5. 键盘映射表

| 键 | 动作 | 说明 |
|----|------|------|
| `tab` / `shift+tab` | Switch tab / move between fields | 原生 |
| `1`–`4` | Jump to tab ①–④ | |
| `ctrl+s` | Save current tab | 校验语义见 §4 |
| `r` | Reset to disk values | 弹确认（覆盖未保存改动） |
| `b` | Open Rubric builder | Grading Tab 按钮「Rubric builder…」等价 |
| `e` | Open config file in `$EDITOR` | 深度编辑放行（v1 不做 TUI 全量编辑） |
| `t` | Test Canvas connection | 仅 Canvas Tab |
| `esc` | Back — confirm if unsaved changes | |
| `?` | Help | 全域 |

## 6. 空态 / 错误态 / 加载态

| 态 | 表现 |
|----|------|
| **空态·未选课程** | 面板顶部 Static：「未选中课程——Grading/Plagiarism/路径页禁用；前往 Dashboard 的 Global 视图进入课程后重进。」 |
| **空态·无 .env** | Canvas Tab 顶部黄色 `Static`：「.env 不存在。填入 BASE_URL/TOKEN 后保存将创建 .env（gitignored）。」 |
| **空态·provider 注册表为空** | provider `Select` 显示「(无可用 provider)」+ 红色提示；保存禁用 |
| **加载态·读配置** | 首帧 `Static`「读取 data/xxx/config.toml …」 |
| **加载态·测试连接** | 「正在连接 Canvas…」+ 按钮 disabled + 不定态旋转符 |
| **错误态·校验失败** | Modal 错误列表（见 §4）；例：「grading.max_parallel_tasks: Input should be less than or equal to 10」「plagiarism weights: sum 1.20 ≠ 1.00」 |
| **错误态·写盘失败(权限)** | `notify(error, "写入失败: <err>")`；不丢输入（field 内容保留，可重试） |
| **错误态·schema 生成失败** | notify(error) + 日志区（本屏右下固定 1 条 RichLog 摘要）记录堆栈首行 |

## 7. Rubric builder（v2 新增，2026-09-01）

`RubricBuilderScreen`（`src/tata_rubric.py`）—— 与 Settings **平级的独立 pushed Screen**（同 ScoreReviewScreen 模式），入口：Grading Tab 按钮「Rubric builder…」（`btn-rubric-builder`）或 `b` 绑定（`action_rubric_builder`）。Esc 关闭**不保存**。

```
┌────────────────────────────────────────────────────────────────────────────┐
│ [b]Rubric builder                                            esc=Back      │
├────────────────────────────────────────────────────────────────────────────┤
│ File: [existing.toml ▾]  [new rubric filename___]   (New rubric… 时显示)   │
│ (error line, red, optional)                                              │
│ DataTable: name | rating | grading | pts   # 现有准则                     │
│ ── form（一次一条）──                                                      │
│ name / desc(TextArea,h5) / rating Select / grading Select / pts Input /   │
│ custom_scale(comma-separated, 仅 grading=custom 可用)                      │
│ [Edit] [Remove] [Add] [Update] [Save rubric]                              │
│ Esc closes without saving.                                                │
└────────────────────────────────────────────────────────────────────────────┘
```

- **文件**：Select = 现有 `data/rubrics/*.toml` + 「New rubric…」（显示 filename Input）；载入选中的 rubric（`get_rubric_definition`；失败 → 红色错误行，屏幕存活）
- **准则编辑**：一次一条 —— Edit（选中行载入表单）/ Remove / Add / Update；表单校验 name 必填、pts 数值、custom_scale 仅 grading=custom（否则 Input 禁用 dim）
- **Save rubric**：`RubricDefinition.model_validate` 全量校验（pydantic 错误逐条展示 + notify(error)）→ 写 `data/rubrics/<name>.toml`（tomlkit `[[criterion]]` AoT；首行 `# schema: ../../config/rubric.schema.json`；无 criteria 时报错「Add at least one criterion」）→ notify(success) + pop
- **返回刷新**：`_on_rubric_builder_closed` → `_load_context()` 重新枚举 rubric/prompt 列表，新建/修改的 rubric 立即出现在 Grading Tab Select
- UI 文案全英文

## 8. v2 布局契约与 CSS 陷阱（2026-09-01）

### 8.1 布局契约（c9272e81 修正）

| 容器 | 契约 | 备注 |
|------|------|------|
| `#settings-top` | `height: 3` | 顶栏（标题 + 上下文 Select），与字段行同高 |
| `#ctx-select` | `height: 3; width: 52` | **v1.1 曾无高度 → Select 渲染高度为 0，「Context: 」显示空白**；修复后固定 |
| TabPane 内容 | 每个 TabPane 包一层 `ScrollableContainer`，链式 `height: 1fr`（TabPane 1fr → ContentSwitcher 1fr → ScrollableContainer 1fr） | 整页不滚，内容区滚 |
| provider 注册表 | `#grading-registry { height: 8; overflow-y: auto }` | 超 8 行内部滚动，不再撑破布局 |
| 字段 | Input/Select/Checkbox `height: 3`、Label `height: 1`（`.settings-field`） | 统一 |
| Save/Reset + `#settings-status` | 底部固定，**在滚动区之外**（`#settings-actions`/`#settings-status` 不嵌套在 TabPane/Scrollable 内） | 动作与校验结果永远可见 |

### 8.2 Textual 8.2.8 CSS gotcha（新增样式必读）

> **Textual 8.2.8 对非 Screen 的 widget 忽略 `CSS` classvar —— 必须用 `DEFAULT_CSS`。**
> `SettingsScreen` 是 `Vertical`（非 Screen），所以 c9272e81 之前其类内 `CSS = """…"""` **从未生效**（顶栏/字段/滚动布局全丢）——这就是「Context: 」空白与布局塌陷的根因。改用 `DEFAULT_CSS` 后恢复。
> 真正的 Screen（`RubricBuilderScreen`、各 ModalScreen）用 `CSS` 则正常。**规则：目标类继承自 `Screen` → `CSS`；其他 widget → `DEFAULT_CSS`。**

### 8.3 v4：prompt 列表高度修复（zutorusvmynx，2026-09-01）

`settings.tcss` 中 `.prompt-row` 行高被强制为 `1`，与行内 Checkbox（`height: 3`）/▲▼ Button（`height: 3`）固有高度不匹配 → 行内容被裁剪为 0（空白行、无法交互/排序）。修复 = `.prompt-row`（及其 Checkbox/Button）改 `height: auto`，行随内容展开。多选+排序能力保留——用户提出的 fallback 两段式未启用，因为根因是 CSS 行高而非组件能力。
