# TATA 设计稿 v1.1 — 99 · 设计系统（Design System）

> 从 `src/score_review.tcss` 提炼主题约定 + 平台通用模式（长任务、日志流、Modal、Toast、增量词汇、**面包屑三层导航**）+ 全平台 TCSS 草案
> v1.1 变更：新增面包屑组件（S1 三层 drill-down 共享）、Tab 工作区数由 4 → 3

______________________________________________________________________

## 1. 主题提炼（源自 score_review.tcss）

| 既有约定                                                               | 提炼为平台规范                                                        |
| ---------------------------------------------------------------------- | --------------------------------------------------------------------- |
| `$primary` 边框 `round`，`border-title-color: $primary`                | 工作面板统一 `round solid $primary` + `border-title`                  |
| `$error/$warning/$success` 三色语义                                    | 徽章/日志/评论等级三色体系（见 §2）                                   |
| `.rating-correct/.rating-partial/.rating-incorrect` + `bold`           | 保留给 Review；平台徽章用同色系但独立 class                           |
| `#content-horizontal` 用 `layout: grid` 2 列、`.narrow` 切 1 列        | **平台窄屏规则沿用 NARROW_WIDTH=100**：所有双栏在宽度\<100 时纵向堆叠 |
| `#filters > Button.off { opacity: 0.3 }`                               | “关闭态”统一 `opacity: 0.3`                                           |
| 布局惯用 `margin: 0 1`、`padding: 1 2`                                 | 面板内边距规范：外层 1，列表 1 2                                      |
| Worker 模式：`run_worker(thread=True, exclusive)` + `call_from_thread` | 平台所有长任务协议（§3）                                              |

## 2. 颜色语义与状态徽章词汇（增量状态呈现代词，**全部英文**）

### 徽章状态词（全平台统一，出现在 Dashboard/状态条/按钮副标题）

| 词汇        | 意境         | class           | 颜色                  | 判定                                   |
| ----------- | ------------ | --------------- | --------------------- | -------------------------------------- |
| `○ Not run` | 无产物       | `.pill-empty`   | `$text-muted`         | 计数=0 且无 mtime                      |
| `◐ Partial` | 计数断链     | `.pill-partial` | `$warning`            | raw>0 且 proc\<raw（或 grad\<proc）    |
| `● Done`    | 全链路齐     | `.pill-done`    | `$success`            | proc==raw 且 grad==proc 且 scores 存在 |
| `◆ Flagged` | 查重出现     | `.pill-flag`    | `$error`              | 聚合/单作业判定命中（只能由查重产生）  |
| `✘ Error`   | 上次运行失败 | `.pill-error`   | `$error`（加 `bold`） | summary errors>0 或 config 解析失败    |
| `? Unknown` | 无法判定     | `.pill-unknown` | `$text-muted`         | config 损坏/目录不可读                 |

### 按钮副标题语法

```
[已就绪词] N/M 或 [Will run N · Skip M]
示例: grade 按钮两行: "grade" / "8 pending · 10 done"
```

## 3. 通用模式

### 3.1 长任务协议（所有 stage / 连接测试 / schema 生成共用）

```
JobHandle = { stage, worker, cancel_event, log_queue: Queue, progress: (done,total) }
生命周期: 预扫描 total → run_worker(thread=True, exclusive, group="jobs")
          → stdout/stderr 重定向到 log_queue → set_interval(0.1) 排空写 RichLog
          → progress 事件更新 ProgressBar → "__done__" → notify + 重扫增量
取消: cancel_event.set() → 协作式；UI 三态: 运行中(可取消) → 停止中(disabled) → 已取消
进度呈现: total>0 → 确定态; total 未知 → indeterminate
```

**规则：** 同一时刻仅 1 个 job（exclusive）；线程内绝不碰 UI；`call_from_thread` 只传递数据不执行重活。

### 3.2 日志流（RichLog）

- `max_lines=2000`（超出滚动丢弃最早），`auto_scroll=True`，`wrap=True`
- 行种类着色：`[stage]` 前缀→`$primary`；`✓`→`$success`；`✗`/`error`→`$error`；`warning`→`$warning`；其余 `$text`
- 日志区固定在工作屏底部（S2/S4），标题 `border_title = "实时日志 · <stage>"`

### 3.3 Modal（ModalScreen）规范

- 布局：居中 `Grid`/`Vertical`，`width: 92`，`max-height: 32`；`border: heavy $primary`
- 标题行 = `Static`（bold）— 始终有 `esc/enter` 语义；危险操作再确认必须双按钮（取消/确认）
- 三类用途：确认（危险/大动作）｜选择（导入 course/assignment）｜展示（校验错误、对比）

### 3.4 Toast（Notification）

- 成功：notify(success)；错误：notify(error)；完成有 data 的：消息含计数（「17/18 完成，1 失败」）
- 不做：自定义 toast 组件（Textual 内置 notify 足够，YAGNI）

### 3.5 帮助

`?` → `ModalScreen` 列出当前屏全部绑定（动态从 `BINDINGS` 生成，避免双份维护）。

### 3.6 面包屑（S1 三层导航共享组件）

- 位置：S1 各层视图顶部（Global 层无面包屑，仅标题带 `[Global]` 标记；Course/Assignment 层显示）
- 格式：`Global / <course slug> / <assignment dir>`，末段 bold；`/` 分隔符 `$text-muted`
- 交互：鼠标点击任一前段 → 回退到该层（等价 `esc` 上钻的带参版）；键盘始终 `esc`/`backspace` 逐级上钻
- 组件：`#breadcrumb` `Static`（`markup=True`，段间 `[link]`/`bold`）；不引入自定义子类（遵循 §5 诚实声明）
- 宽度：超出 70 列时中间段截断为 `…`（防止长 slug 撑爆标题行）

## 4. 全平台 TCSS 草案（继承 score_review 风格）

```css
/* ===== TATA 平台全局 ===== */
Screen { background: $surface; }
#app-shell { height: 1fr; }

/* 面板：统一 round + 标题 */
.panel {
  border: round $primary;
  border-title-color: $primary;
  background: $surface;
  padding: 0 1;
}
.panel > #inner { width: 1fr; height: 1fr; }

/* 徽章 */
.pill { padding: 0 1; text-style: bold; }
.pill-empty   { color: $text-muted; }
.pill-partial { color: $warning; }
.pill-done    { color: $success; }
.pill-flag    { color: $error; }
.pill-error   { color: $error; text-style: bold; }
.pill-unknown { color: $text-muted; }

/* stage 按钮（S2） */
.stage-btn { width: 1fr; height: 3; border: round $panel; }
.stage-btn.-running { border: round $warning; }
.stage-btn.-done    { border: round $success; }
.stage-btn > .secondary { color: $text-muted; }

/* 日志 */
#richlog { border: round $panel; border-title-color: $text-muted; height: 1fr; }

/* 进度行 */
#progress-row { height: auto; padding: 0 1; }
#progress-row > ProgressBar { width: 1fr; }

/* DataTable 状态列单元格 */
.datatable .cell-flag { color: $error; text-style: bold; }

/* 配置面板（S2/S5） */
#config-panel { width: 34; border-left: solid $primary; }
#config-panel Input, #config-panel Select { width: 1fr; }
#config-panel Input.-invalid { border: round $error; }

/* 顶部状态条 */
#screen-header { height: auto; padding: 0 1; border-bottom: solid $primary; }
#screen-header .header-title { text-style: bold; }

/* 窄屏（<100 列）沿用 score_review 模式 */
#content-horizontal { layout: grid; grid-size: 2 1; grid-columns: 1fr 1fr; grid-rows: 1fr; }
#content-horizontal.narrow { grid-size: 1 2; grid-rows: 1fr 1fr; }

/* 弹窗 */
ModalScreen { background: $background 60%; }
.confirm-modal { width: 92; max-height: 36; border: heavy $primary; background: $surface; padding: 1 2; }
.confirm-modal Static { width: 1fr; }

/* 空态 */
.empty-state { width: 1fr; height: 1fr; content-align: center middle; color: $text-muted; }

/* Review 复用不覆盖：score_review.tcss 原样加载（CSS_PATH 指向原文件） */
```

## 5. 组件可用性自检（Textual 8.x 真实 API）

| 组件                                     | 使用屏          | 备注                                                            |
| ---------------------------------------- | --------------- | --------------------------------------------------------------- |
| `DataTable` `<` `row_cursor`             | S1/S4           | 排序自行维护（render 前 sort）                                  |
| `RichLog` `<` `write`/`markup`           | S2/S4 日志      | 不支持富文本块，只行级                                          |
| `TabbedContent`/`Tabs`/`TabPane`         | S4/S5           | v1 不用 `TabbedContent` 做全局导航（S1-S4 用 `App` 级 Tabs 壳） |
| `Input`/`Select`/`Checkbox`/`RadioSet`   | S2/S5           | `Input(password=True)` 掩码                                     |
| `ProgressBar`                            | S2/S3           | `show_percentage/eta` 可关                                      |
| `ModalScreen`                            | S1/S2/S4/S5     | `dismiss` 返回                                                  |
| `Static`/`Header`/`Footer`               | 全域            | 平台 Footer 自定义绑定提示                                      |
| `Notification`（`App.notify`）           | 全域            | severity 三档                                                   |
| `run_worker`/`Worker`/`call_from_thread` | S2/S4/S5 长任务 | `thread=True` + `exclusive`                                     |
| `Markdown`                               | S3（复用）      | 不动                                                            |

**注意（诚实声明）：** 平台不新增任何自定义组件子类作为“组件库”——需要特殊视觉的行/徽章用 `Static` + class 实现，保持 Textual 8.x 原生面。
