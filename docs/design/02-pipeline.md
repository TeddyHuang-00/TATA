# TATA 设计稿 v1.1 — 02 · Pipeline → Assignment 工作台（S1 第三层）

> 职责：单作业工作台 —— 顶部作业状态条 + 面包屑、中部 stage 按钮区（增量提示）+ 配置面板、底部 RichLog 实时日志 + 进度条
> 位置：S1 Dashboard 第三层（Assignment 视图）；无独立 Tab（v1.1 变更）。内容与 v1 原 S2 一致，新增面包屑与 `esc` 返回绑定
> 对应 CLI：`fetch / preprocess / plagiarism / grade / score / analyze`（全部 stage 纯函数）；长任务后台线程化
> 关键源码事实：`_STAGES` 注册 `preprocess_assignment(config_path)`、`detect_plagiarism(config, aggregate, output)`、`grade_assignment(config_path, force=)`、`score_assignment(config_path)`、`analyze_assignment(config_path)`、`_run_fetch(FetchCliOptions)`；`GradingCheckpoint.done` 清单；fetch 有 `.fetch-cache.json`；preprocess 可做 mtime 增量

---

## 1. ASCII 线框（≤100 列，文案全英文）

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Dashboard / 271218 / 1-10-my-ai-starting-point   esc=Back to course          │
│ Pipeline · 1-10-my-ai-starting-point [2979511]  ◐ Partial   Yest 21:03  [i]  │
├────────────────────────────────┬─────────────────────────────────────────────┤
│ ┌────────────┐  ┌────────────┐ │ ⚙ Config (fg toggle)                       │
│ │F fetch     │  │P preprocess│ │ rubric      rubrics/exam.toml      [edit]   │
│ │raw 18      │  │18/18 done   │ │ prompt      prompt/system.md      [edit]   │
│ └────────────┘  └────────────┘ │ provider    deepseek_chat_tool    [change]  │
│ ┌────────────┐  ┌────────────┐ │ max_parallel 10   (1..10)                  │
│ │G grade     │  │S score     │ │ reference   (unset)                        │
│ │18/18 done  │  │18/18 scored│ └───────────────────────────────────────────§
│ └────────────┘  └────────────┘ │  [s]Review  [p]Plagiarism  [e]Edit config   │
│ ┌────────────┐  ┌────────────┐ │  [a]Run aggregate  [settings]Job settings  │
│ │K plagiarism│  │A analyze   │ │              (v2)                          │
│ │2 pairs(1)  │  │stats done   │ │                                            │
│ └────────────┘  └────────────┘ │                                            │
├────────────────────────────────┴─────────────────────────────────────────────┤
│ ▶ grade (8 pending/18)  [████████░░░░░░░░░░░░░░░░] 8/18  45%   [x]Cancel     │
│ 12:01:03 [grade] 8/18: 0142 · 0147 in progress                              │
│ 12:01:05 [grade] ✓ 0142 done 2.3s   [grade] ✗ 0156 error: 429 (retry)       │
│ 12:01:11 [grade] done 17/18, 1 error ─────────────────────────────────────── │
└──────────────────────────────────────────────────────────────────────────────┘
```

> 新增：首行 = `[i]` 面包屑（`#breadcrumb`，`Global / <course> / <assignment>`，末段 bold；鼠标点击可回退）；`esc`/`backspace` = 返回 Course 视图（02 原有绑定之外新增，与 `fg`、`1-9` 无冲突）。**全部线框/按钮/日志文案英文。**

## 2. 组件清单

| 标注 | 组件（Textual 8.x） | 用途 |
|------|--------------------|------|
| 顶部状态条 | `Static` + 徽章 `Static` | 作业名/ID/状态徽章/最近运行/`[i]`增量摘要按钮 |
| Stage 按钮 | 6× `Button`（`Vertical` 内两列 `Grid`，class `stage-btn`） | F/P/G/S/K/A 六段；副标题行显示增量计数（Button 内 `\n` 两行文本） |
| 配置面板 | `VerticalScroll` + `Static`/`Input`/`Select`/`Button` | 常用配置只读展示 + 快捷编辑（保存走 S5 同款写入器） |
| 进度行 | `ProgressBar` + `Static`（统计文本） | 确定态：total=预扫描数、update(progress)；未知时 `ProgressBar(show_eta)` 不定态 |
| 日志区 | `RichLog`（`markup=True`，`wrap=True`） | 实时日志流；按前缀着色（`✓` 绿 / `✗` 红 / `[stage]` 主题色） |
| 取消 | `Button`（进度行内，运行中才出现） | 触发协作式取消（见 §4） |
| 确认 Modal | `ModalScreen`（`#confirm-modal`） | 危险/大动作确认（force 重评、删除产物、停 job） |

## 3. 键盘映射表

| 键 | 动作 | 说明 |
|----|------|------|
| `f` | Run fetch | 等价 `_run_fetch`（当前作业维度） |
| `p` | Run preprocess | |
| `g` | Run grade | 弹确认 Modal（显示增量：待评 N / 已有 M） |
| `s` | Run score | |
| `k` 或 `;` | Run plagiarism | 含聚合（aggregate=True） |
| `a` | Run analyze | |
| `x` | Cancel current job | 仅运行中可用；见 §4 语义 |
| `e` | Open config.toml in `$EDITOR` | 读后自动重扫刷新面板 |
| `i` | Toggle incremental summary | `[i]` 顶部状态条切换 |
| `fg` | Collapse config panel | 给日志区让出空间 |
| `esc` / `backspace` | Back to Course view | v1.1 新增（上钻；Review 弹窗内的 esc 优先归 Modal） |
| `r` | Re-rescan incremental | 清空"将执行/将跳过"预估并重算 |
| `ctrl+v`/`ctrl+t` | Switch to Review / Dashboard | 平台全局 |
| `?` | Help | 全域 |

## 4. 长任务模型（本屏核心，硬性要求 4/5）

**统一后台执行协议（所有 stage 共用，`JobHandle`）：**
```
启动：  screen.run_worker(worker_fn, thread=True, group="stage", exclusive=True)
          worker_fn = 后台线程将 现有同步函数 送入:
            ① stdout/stderr 重定向 → queue.Queue（LogBridge，逐行入队）
            ② 进度事件（done/total）→ 同一队列（带类型前缀）
        主线程: set_interval(0.1) 排空队列 → RichLog.write(行) / ProgressBar 更新
            （关键点：绝不从 worker 线程直接碰 UI，全部经 call_from_thread 或队列+timer）
结束：  队列收到 "__done__" 或 summary 行 → notify + 删除该 job → 重扫增量 → 徽章更新
```
**进度来源：** 启动前预扫描给出 `total`（preprocess/grade=raw 文件数；fetch=条目数；score=graded 数；plagiarism/analyze=未知→不定态）。worker 内按每完成一个文件发一事件。

**取消（协作式，诚实语义）：** 同步函数无法被强杀。`cancel_event.set()` 后：当前正在处理的文件**跑完**，之后不再启动新任务；`worker.cancel()` 仅作兜底。UI 立即显示「正在停止…」，保证：
- grade：`GradingCheckpoint.done` 已落盘 → 下次续跑（增量提示正确反映）
- fetch/preprocess：下轮启动时 `.fetch-cache.json`/mtime 判断跳过
- 真被卡死（LLM 挂起）：确认框提供「强制丢弃 job」= 直接 `worker.cancel()`，进程内线程残留由平台退出时回收（风险见报告）

**并发上限：** `max_parallel_tasks` 由现有函数内部多线程实现（10 线程并行），平台只跑 **1 个 stage job**（exclusive group）——防止同时跑两个 stage 写坏同一产物目录。

## 5. 增量语义表（UI 显式呈现，acceptance 5）

| stage | 已有产物 | 新鲜性依据 | 按钮副标题（英文） | 确认 Modal 文案（英文） |
|-------|---------|-----------|-----------|----------------|
| fetch | `raw/` 文件数 + `.fetch-cache.json` mtime | cache mtime vs 运行时刻 | `raw 18` 或 `not fetched` | "Will fetch 18 submissions; cached files skipped" |
| preprocess | `processed/*.md` 数 | 逐文件 mtime vs 对应 raw mtime | `18/18 done` | "Will convert 0 / skip 18 (fresh)" |
| grade | `graded/*.json` + `GradingCheckpoint.done` | done 清单 vs processed 清单 | `18/18 done` | "Will grade 8 (resume) / --force regrades 18" |
| score | 分数 JSON（graded 派生） | graded mtime vs score 产物 mtime | `18/18 scored` | "Will score 0/18" |
| plagiarism | `plagiarism/all_pairs.json(+embedding)` | pair 文件 mtime vs processed mtime | `2 pairs (1 flag)` | "Will check 18 texts (copydetect+embedding)" |
| analyze | `logs/` 下汇总文件 | 存在性 | `stats done` | "Will regenerate stats" |

**增量摘要 `[i]` 展开后（顶部状态条下方一行，8 列内）：**
```
▶ To run: fetch 0 · pre 0 · grade 8 · score 0 · plag 2  |  Skip: 21  |  No change: 3
```

## 6. 交互流

```
进入(Assignment 视图) → on_mount 读 state.current_assignment → 预扫描增量 → 渲染按钮副标题
用户按 g → Confirm Modal:
    "Will grade 12 (checkpoint 6/18 done, 12 pending)   [Normal] [--force regrade all]"
    → 确认 → ProgressBar + RichLog 就绪 → job 启动
运行中:  所有 stage 按钮 disabled（除 x 取消）；配置面板只读
摘要行:  [grade] 17 success, 1 error(s), 94.4% success rate (对齐 _format_job_summary)
完成后:  notify(success/warning) → 自动重扫 → 按钮副标题/徽章刷新；如 grade 完成则 Review 入口可用
```

**错误/重试路径：** `[grade] ✗ 0156 429` 行在日志流中红色显示；作业级失败由现有函数内部重试；最终 summary 有 error>0 → `notify(warning)`。用户可对**失败名单**按 `r` 重试（从 checkpoint 续跑天然只补没完成的）。

## 7. 空态 / 错误态 / 加载态（文案全英文）

| 态 | 表现 |
|----|------|
| **Empty·no config** | 进入时检测 `data/<course>/<name>/config.toml` 缺失或 `[grading]` 段校验失败 → 全屏 Static 提示 + 「Open Settings」按钮（切 S5），stage 按钮全禁用 |
| **Empty·not fetched** | raw 计数 0：preprocess/grade/score 按钮副标题显示「needs fetch first」，按钮**可点但弹「建议先运行 fetch」确认** |
| **Empty·no scores** | `s` 分数审查入口按钮 disabled + 副标题「No scores yet」 |
| **Loading·config parse** | 面板区 `Static`「Parsing config…」 |
| **Loading·job start** | 进度行 `Static`「Initializing (model load up to 30s)…」+ 不定态 ProgressBar |
| **Error·LLM failure** | 日志红字 + 每失败 1 条计数；不打断其余并发；结束 summary 精确报错数 |
| **Error·fetch network** | `[fetch] ✗ cannot connect 40001` 红字；`notify(error)`；job 立即终止；重试前建议检查 Settings·Canvas |
| **Error·canceling** | 进度行「Stopping… (waiting for current file)」；完成后 notify("Cancelled, progress saved") |

## 8. 配置面板字段（右下，常用项；完整项在 S5）

| 字段 | 控件 | 绑定 config 键 | 备注 |
|------|------|---------------|------|
| rubric 路径 | `Input` | `[grading].rubric` | 校验 rubric 解析（`rubric.py`）失败即红色边框 |
| system_prompt 路径 | `Input` | `[grading].system_prompt` | 支持逗号分隔多文件 |
| provider | `Select` | `[grading].provider` | 选项来自 provider 注册表；S5 里配 base_url/api_key/model/temperature |
| max_parallel_tasks | `Input`(数字) | `[grading].max_parallel_tasks` | 限制 1..10（config 校验 ge=1 le=10） |
| reference_file | `Input` | `[assignment].reference_file` | 留空=toml 无此键（rubric 模式） |
| 保存 | `Button` | — | 只写**作业层** config.toml（course 层默认仍生效，per-key 覆盖）；校验失败见 S5 §6 |
