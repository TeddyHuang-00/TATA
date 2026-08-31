# TATA 设计稿 v1.1 — 03 · Review（S3）

> 职责：分数审查入口 + 复用既有的 `score_review.Viewer`（不重画其内部）；**v1.1 仅更新入口措辞**（Assignment 视图 = 原 Pipeline 工作台）
> 策略：**抽取复用**。将 `Viewer` 的 `compose / BINDINGS / action_*` 原样迁移为一个 `ScoreReviewScreen(Screen)`，`Viewer(App)` 保留为 CLI `view` 薄壳（`run()` 不变）；平台 `push_screen(ScoreReviewScreen(...))`。

---

## 1. 入口卡片线框（≤100 列，文案全英文）

Assignment 视图（S1 第三层，即原 Pipeline）右下配置面板区（02 §1 的右下块）与 Course 视图行选中 `s` 均触发。入口为：

```
┌────────────────────────────────────────────────────────────────────────────┐
│ ➜ Score review · 1-10-my-ai-starting-point [2979511]                       │
│                                                                            │
│   Graded 18/18 · Mean 82.1 · Max 98 · Min 45   [s] Open review  [esc] Back  │
│                                                                            │
│   ⚠ 3 ungraded: 0142 0157 0163 — run grade (g) first then retry            │
└────────────────────────────────────────────────────────────────────────────┘
```
（若从 Course 视图按 `s` 进入且该作业未评：`notify(warning)` 并弹出同样的提示行。）

## 2. 组件清单

| 标注 | 组件（Textual 8.x） | 用途 |
|------|--------------------|------|
| 入口卡 | `Static`（复用 Pipeline 配置面板占位） | 摘要 + 打开按钮 |
| 「打开审查」 | `Button` → `App.push_screen(ScoreReviewScreen)` | 全屏压栈 |
| （复用） | `Markdown` / `Static` / `Select` / `Button` / `ProgressBar` / `VerticalScroll` | `ScoreReviewScreen` 内部，**零改动** |

## 3. 复用改动清单（实现提示，非代码）

1. `score_review.py`：新增 `class ScoreReviewScreen(Screen)`，把 `Viewer` 的 compose/绑定/action/`_render`/预览 worker 逻辑整体搬入（`self.students`、`preview_cache` 等状态不变）；`Viewer(App)` 改为 `compose → yield ScoreReviewScreen(...)` 的单屏 App。
2. 平台侧新增绑定：`esc → pop_screen`（Review 内有效，其余屏 esc 关 Modal 语义由平台全局统一）。
3. CLI `main.py view` 与 `uv run cli view` 继续走 `Viewer`，行为不变；`--web` 路径不受影响。
4. 不做：不改 `narrow` 阈值（100）、不改数字键复制、不加平台配色覆盖——`score_review.tcss` 原样保留。

## 4. 交互流

```
Assignment 视图按 s（或 Course 视图选行按 s）
  → 检查 graded 产物: 无 → notify(warning) 停在原屏
  → 有 → push_screen(ScoreReviewScreen(score_dir))   # 全屏覆盖，Header/Footer 由 Review 自带
  → 内：←/→ 学生切换 · j raw JSON · 1-9 复制评论 · f 评分等级过滤（全部沿用既有约定）
  → 按 esc 或 r → pop_screen 返回 Assignment 视图（工作台状态原样保留，Tab 未切换）
```

## 5. 键盘映射表（复用 + 新增 1 键）

| 键 | 动作 | 来源 |
|----|------|------|
| `←/→` 或 `↑/↓` | 上/下一位学生 | 继承（score_review 既有） |
| `j` | 切换 raw JSON 视图 | 继承 |
| `1`–`9` | 复制第 N 条评分标准意见 | 继承 |
| `f` | 切换评分等级过滤 | 继承（过滤按钮也可鼠标点） |
| `q` | 退出整个平台 | **平台全局**（Review 内不拦截；先弹「关闭审查并退出?」确认） |
| `esc` / `r` | 返回 Assignment 视图 | **平台新增**（与既有键无冲突；`r` 在 Review 原无绑定） |
| `?` | 帮助 | 全域 |

## 6. 空态 / 错误态 / 加载态

| 态 | 表现 |
|----|------|
| **空态·无评分 JSON** | `ScoreReviewScreen` 既有逻辑：`criteria-list` 显示「No student data in this folder.」，Prev/Next disabled；平台在 push 前就拦截（入口卡提示先跑 grade） |
| **空态·部分学生无提交文件** | 既有：preview 区「No submission file found for this student.」 |
| **加载态·raw 预览转换** | 既有：`Converting xxx.ipynb…` + 后台 `run_worker(thread=True)` 懒转换（`_convert_ipynb_to_markdown` 等，复用 `processing.py` 转换器） |
| **错误态·转换失败** | 既有：preview 区显示 `Preview failed: <err>`；不崩溃 |
| **错误态·评分 JSON 损坏** | 既有：`_load_students` 跳过坏文件；入口卡计数与实际显示数不一致时底部提示「N 个文件解析失败」 |
