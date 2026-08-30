# TATA 设计稿 v1.1 — 05 · Settings（S5）

> 职责：provider、Canvas、plagiarism 权重、路径与 schema 生成的集中配置编辑；**上下文选择器：Global / Course / Assignment 三级**（右上 Select）
> v1.1 变更：写入目标由「根配置/作业配置」两层改为**三层**：global config（`data/config.toml`，可选，跨课程默认）、**course config**（`data/<course>/config.toml`：course_id/`[[fetch.assignments]]`/[plagiarism] 覆盖）、assignment config（`data/<course>/<name>/config.toml`：`[grading]`/`[plagiarism]`/`[assignment]`/`[processing]`）；.env 仍全局
> 上下文来源：`state.current_course` / `state.current_assignment`；从 S1 三层的 `cfg`/`g` 进入时预置（Global 视图 `g`→Global 上下文；Course 视图 `cfg`→Course 上下文；Assignment 视图 `e`→Assignment 上下文）
> 保存逻辑：全屏右侧显示「将写入: 文件路径」；内容经 `load_assignment_file` 校验通过才落盘（pydantic 错误逐条展示）

---

## 1. ASCII 线框（≤100 列）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Settings · Context: [Course: 271218 ▾]                                      │
├──┬─────────────────────────────────────────────────────────────────────────┤
│  │ ⚙ Grading (assignment config)                                          │
│  │ provider      [deepseek_chat_tool        ▼]   [Refresh registry]        │
│  │ base_url      [https://api.deepseek.com    ]                            │
│  │ api_key       [sk-••••••••••••••••       ]  (set)                       │
│  │ model         [deepseek-chat               ]   temperature [ 0.20 ]     │
│  │ mode          (•) chat   ( ) reasoning    │   max_parallel [ 10 ]      │
│  │ ────────────────────────────────────────────                            │
│  │ rubric        [rubrics/example_rubric.toml ]                            │
│  │ system_prompt [prompt/system.md            ]  (comma-separated)         │
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
| 上下文选择器 | `Select`（右上 `#ctx-select`） | Global / Course: <name> / Assignment: <name>；切换即重新载入对应层 config |
| Tab 容器 | `TabbedContent` + `TabPane`×4 | 分区编辑 |
| 字段 | `Input`（`password=True` 用于 api_key/TOKEN） | 文本/密钥 |
| 枚举 | `Select`（provider/model/mode 项） | 有限选项（provider 注册表动态加载） |
| 布尔 | `Checkbox`（remove_base64_images 等 `[processing]` 开关，④ 页批量） | 处理选项 |
| 单选 | `RadioSet`+`RadioButton`（chat/reasoning mode） | 互斥模式 |
| 状态行 | `Static`（`#settings-status`） | 校验结果（通过/错误摘要）+ 目标文件路径 |
| 操作 | `Button`（保存/重置/生成 schema/测试连接/打开目录） | 动作 |
| 错误 | `notify` + `ModalScreen`（校验失败详情列表） | pydantic 逐条错误 |

## 2.5 上下文规则（三层写入目标）

| 上下文 | 可编辑内容 | 写入目标 |
|--------|-----------|---------|
| **Global** | 环境 `.env`（BASE_URL/TOKEN）；provider 注册表（`config/provider.toml`）；global config 可选的跨课程默认（[plagiarism] 权重/阈值） | `.env` / `config/provider.toml` / `data/config.toml` |
| **Course** | `[fetch]` course_id、`[[fetch.assignments]]` 只读摘要、`[plagiarism]` course 级覆盖 | `data/<course>/config.toml` |
| **Assignment** | `[grading]`（provider/rubric/prompt/parallel）、`[assignment]` 目录、`[processing]`、`[plagiarism]` | `data/<course>/<name>/config.toml` |

> 未选作业时 Assignment 上下文禁用（只显示 Global/Course）——与 v1 行为一致但扩展为「未选课程」亦然。Course 上下文字段与 v1 根配置页相同（Canvas 页从「根配置」改名为「课程配置」）。

## 3. 各页字段与写入目标

### ① Grading（作业级 `[grading]`）
| 字段 | 控件 | 写入键 |
|------|------|--------|
| provider | `Select` | `grading.provider` |
| base_url / api_key / model / mode / temperature | `Input`/`Select`/`RadioSet`/`Input` | **已确认（2026-08-29）：`config/provider.toml` 的 `[providers.<name>]` 注册表**（`src/provider.py` `get_providers()` 读取；api_key 写 `${ENV}` 占位符而非明文）。编辑语义：全局生效，提示「保存将更新所有作业的 provider 选项」；刷新注册表按钮对应重新 `get_providers()` |
| max_parallel_tasks | `Input`(数字) | `grading.max_parallel_tasks`（1..10） |
| rubric / system_prompt | `Input` | `grading.rubric` 键（`rubric` / `system_prompt`） |

### ② Canvas（course config + .env）
| 字段 | 控件 | 写入 |
|------|------|------|
| BASE_URL | `Input` | `.env` |
| TOKEN | `Input`(password) | `.env` |
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
     Global 上下文可编辑 .env/provider；Course/Assignment 字段禁用
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
