# TATA 设计稿 v1.2 — 04 · Plagiarism（S4）

> 职责：course 级查重屏 —— 4-tab（Aggregate / Assignments / Students / Pairs）排名表（DataTable）+ Pairs 内嵌对比面板 `#cmp-pane`；`[p]` 检测 / `[a]` 聚合 job 按钮；z 分数按 per-assignment 与聚合两套呈现
> v1.2 (2026-08-30 update)：S4 重构为 course 级 4-tab（Aggregate 默认 / Assignments / Students / Pairs）；对比改为 Pairs 面板内嵌 `#cmp-pane`（行高亮即时更新，no push_screen，CompareModal 已删除）；检测/聚合 quiet 运行（不打印纯文本报告，TUI 只读 JSON）；display 阈值唯一来源 = course config `[plagiarism].display_threshold`（默认 0.8，容错读取；z 聚合仍按 alpha）；「embedding 列/二次 Tab」不存在，未实现
> v1.1 变更：顶部标题显示课程上下文；`[a]` 聚合键驱动 **course config**（`data/<course>/config.toml` 的 `[[fetch.assignments]]`），不再是 data/ 根配置；聚合只在本 course 内（跨课程聚合 v1 不做，YAGNI）
> 数据来源：各作业 `plagiarism/all_pairs.json`（copydetect：`{test_file, reference_file, test_similarity_pct, reference_similarity_pct, max_similarity_pct, token_overlap}`；`all_pairs.embedding.json` 属检测端混合输入，不在 pane 展示）+ 课程 `plagiarism/aggregate.json`（`run_aggregate_job` 写入；内容来自 `plagiarism_aggregate`：`MatchRecord{student_a, student_b, raw_similarity_pct, logit_similarity, z_score, one_sided_p_value}` + 个体方法 gumbel 统计 + 合并 z）；copydetect `autoopen=False`（不弹浏览器）

---

## 1. ASCII 线框（≤100 列，文案全英文）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Plagiarism · course 271218  ·  pairs 42 (top 20)  ·  display threshold 80% │
│ [p]Run detection  [a]Run aggregate                                         │
├────────────────────────────────────────────────────────────────────────────┤
│ [Aggregate] [Assignments] [Students] [Pairs]   (TabbedContent, agg first)  │
│ ┌─ Aggregate (default): course-level z/p table ───────────────────────────┐│
│ │ #  Student A   Student B   raw sim   z     p(α)      Flag               ││
│ │  1  0142      0156        88.4      5.21   0.0002   ◆ FLAG              ││
│ │  ...(20 rows/page)                                                      ││
│ └─────────────────────────────────────────────────────────────────────────┘│
│ Assignments / Students / Pairs: same DataTable + empty-static shape;       │
│ Pairs rows use <assignment>/plagiarism/all_pairs.json (Assignment/Student  │
│ A/Student B/sim %/overlap/Flag columns); row selection renders #cmp-pane:  │
│ ┌─ #cmp-pane (embedded, no modal)  0142.py ↔ 0156.py  sim 91.2%  ov 340 ─┐ │
│ │ 0142.py (processed)    │ 0156.py (processed)    │                      │ │
│ │ 1  def score(data):    │ 1  def score(data):    │ ...similar lines      │ │
│ │ 2      agg = [...];    │ 2      agg = [...];    │ highlighted[reverse]  │ │
│ └─────────────────────────────────────────────────────────────────────────┘ │
├────────────────────────────────────────────────────────────────────────────┤
│ ↳ 1 pair over α=0.01 threshold (flag) · 2 over display threshold 80%        │
└────────────────────────────────────────────────────────────────────────────┘
```

## 2. 组件清单

| 标注 | 组件（Textual 8.x） | 用途 |
|------|--------------------|------|
| Tab 容器 | `TabbedContent` + `TabPane`×4（`#pane-aggregate`（初始）/`#pane-assignments`/`#pane-students`/`#pane-pairs`） | course 级：聚合 z/p 表 / 按作业 / 按学生 / 按 pair |
| 排名表 | `DataTable`×4（`cursor_type="row"`，`zebra_stripes`） | Aggregate：Student A/B、raw sim、z、p、Flag；Assignments/Students：Assignment/Student、Pairs、Flagged、Max sim %；Pairs：Assignment、Student A/B、sim %、overlap、Flag |
| 判定列 | 单元格 `Static`（`flag`/`flag-warn` class） | display 判定用 `display_threshold`（80%）；Aggregate 的 z/p 判定用聚合 `alpha` |
| 顶部按钮 | `Button`×2（`plag-run` / `plag-aggregate`） | `[p]` 运行检测（单作业 copydetect，quiet）；`[a]` 运行聚合（`run_aggregate_job`：detect_plagiarism aggregate=True, quiet + `_write_aggregate_json`） |
| 状态行 | `Static`（`#plag-status`） | 汇总：对总数 / display 疑点数 / 阈值（`display threshold N%`） |
| 对比面板 | `#cmp-pane`（`Horizontal` 内 2×列，行高亮即时更新；**非 ModalScreen、no push_screen**） | 并排文本 + 相似片段高亮；复用 `preview_content`/`find_raw_file` |
| （复用） | `RichLog` + `JobHost`（src/tata_jobs.py 共享协议） | 检测/聚合运行日志；进度走 JobHost 协议 |

## 3. z 分数双呈现设计（验收关键）

| 视图（tab） | 列 | 含义 | 置信来源 |
|------|----|------|---------|
| Pairs | `sim %` / `overlap`（`max_similarity_pct` / `token_overlap`） | 该作业内部排名的原始相似度 | 该作业 `all_pairs.json` 原样 |
| Assignments / Students | Assignment / Student、Pairs、Flagged、Max sim % | 按作业、按学生聚合的 display 级计数 | 各作业 `all_pairs.json` 汇总 |
| Aggregate（默认） | `raw sim` / `z` / `p` / `Flag` | logit 变换→per-assignment z 标准化→跨作业 Stouffer 合并；**主体是聚合 z，不是原始分** | `plagiarism/aggregate.json`（`plagiarism_aggregate` 统计） |

明确标注（表头与状态行）：“⚠ 聚合 z 为跨作业归一化信号，`raw sim` 为原始值”——避免把 88% 误当绝对证据（呼应 aggregate 源码注释“triage signal, not proof”）。

**判定标记规则：**
- Pairs：`sim ≥ display_threshold(80)` → `◆ FLAG`（红色 `flag` class）
- Aggregate：`p < alpha(0.01)` → `◆ FLAG`；仅预览 `z ≥ 3` 且未超 alpha → `? watch`（黄色）；否则 `—`

## 4. 交互流

```
进入 S4（Tab 切到 plagiarism）→ 读 state.current_course（course 级 scope，不看 current_assignment）
  ├─ 无 course → 空态提示：「请先在 Dashboard 进入某课程再使用查重屏幕」
  ├─ 逐作业读 <assignment>/plagiarism/all_pairs.json（tolerant 解析，单文件失败记入 course_errors 不中断）
  ├─ aggregate.json 存在 → Aggregate 填充；不存在 → 空态 + [a] 按钮
  ├─ 全部无 pairs → 各空态 + [p] 按钮
Pairs 行高亮（↓/j）→ on_data_table_row_highlighted → #cmp-pane 即时渲染
   （侧文件解析：优先 processed/*.md，回退 raw，复用 preview_content/find_raw_file；
     token_overlap 行集合 → 相似行高亮；无 push_screen、无 Modal）
[a] 运行聚合 → job（run_aggregate_job：detect_plagiarism(aggregate=True, quiet=True)
     + _write_aggregate_json 写课程 plagiarism/aggregate.json）→ 完成后各 pane 刷新 + notify
[p] 运行检测 → job（detect_plagiarism(aggregate=False, quiet=True)，单作业 copydetect，需
     state.current_assignment（无 → notify 提示先进入课程/作业）；autoopen=False 不弹浏览器）
     → 完成后 Pairs/Assignments/Students 刷新
```

## 5. 键盘映射表

| 键 | 动作 | 说明 |
|----|------|------|
| `tab` | Switch pane | TabbedContent 原生（4 pane 循环） |
| `↑/↓` 或 `j/k` | Move in table | DataTable 原生 |
| `p` | Run detection（当前作业） | 单作业 copydetect；`run_aggregate_job` 之外的另一 job 入口 |
| `a` | Run aggregation（当前 course） | 需当前 course config 的 `[[fetch.assignments]]` 存在，否则 job 失败/提示缺失（引导去 Course 视图导入作业） |
| `r` | Reload data files | 外部改动后刷新 |
| `esc` | Back to Dashboard | 离开 S4 |

> 对比不再有独立 Modal/键位：Pairs 行高亮即渲染 `#cmp-pane`（无 `enter`/`c`/`o` 弹窗绑定；`esc` 只负责回 Dashboard）

## 6. 空态 / 错误态 / 加载态

| 态 | 表现 |
|----|------|
| **空态·无 course** | 顶栏隐藏，各 pane 显示「No course selected. Open Dashboard and enter a course first.」 |
| **空态·未运行检测** | Pairs/Assignments/Students pane 居中 Static：「No pairs yet. Run detection (p) or aggregation (a).」 |
| **空态·无对** | `pair_count == 0`（模板单文件/无参考匹配）；聚合已跑无对：「Aggregation done, 0 tested pairs.」 |
| **空态·聚合缺失** | Aggregate pane：Static「No aggregate report yet. Run (a).」 |
| **加载态·检测运行中** | RichLog 实时日志 + 进度行（JobHost 协议）；表格加载完成后替换 |
| **错误态·JSON 损坏** | 该 pane 显示「Load failed: <err>」+ `notify(error)`；建议 `p` 重跑 |
| **错误态·单作业 pair 文件缺失** | 该作业行从列表跳过，空闲额外尾部注明「Load failed: <assignment> (missing all_pairs.json)」；聚合跳过该作业（状态行注明 Skipped） |
| **错误态·嵌入模型下载失败** | 检测端的嵌入混合失败（embedding 仅检测端输入）；日志红字 + warning，copydetect partial |
