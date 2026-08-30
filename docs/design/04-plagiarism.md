# TATA 设计稿 v1.1 — 04 · Plagiarism（S4）

> 职责：相似度对排名表（DataTable）、行选中→对比弹窗（Modal 并排文本）、顶部聚合报告入口；**z 分数按 per-assignment 与聚合两套呈现**；上下文=当前 course（跨作业聚合）或当前作业（单作业对）
> v1.1 变更：顶部标题显示课程上下文；`[a]` 聚合键驱动 **course config**（`data/<course>/config.toml` 的 `[[fetch.assignments]]`），不再是 data/ 根配置；聚合只在本 course 内（跨课程聚合 v1 不做，YAGNI）
> 数据来源：`plagiarism/all_pairs.json`（copydetect：`{test_file, reference_file, test_similarity_pct, reference_similarity_pct, max_similarity_pct, token_overlap}`）、`all_pairs.embedding.json`（嵌入向量来源），聚合报告来自 `plagiarism_aggregate`（`MatchRecord{student_a, student_b, raw_similarity_pct, logit_similarity, z_score, one_sided_p_value}` + 个体方法 gumbel 统计 + 合并 z）

---

## 1. ASCII 线框（≤100 列，文案全英文）

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Plagiarism · 271218 / 1-10-…   pairs 42 · threshold 80%  [p]Run  [a]Aggregate│
├────────────────────────────────────────────────────────────────────────────┤
│ ┌─Assignments─────────────┐ ┌─Cross-assignment aggregate─┐                    │
│ │ #  File A     File B    │ │ #  Student A   Student B   │                    │
│ │  1  0142.py  0156.py    │ │  1  0142      0156        │                    │
│ │     sim 91.2% ov 340    │ │     sim 88.4%  z 5.21     │                    │
│ │  2  0142.py  0163.py    │ │  2  0157      0163        │                    │
│ │     sim 84.6% ov 210    │ │     sim 81.0%  z 4.88     │                    │
│ │  3  0157.py  0142.py    │ │  3  ...(more)             │                    │
│ │  ...(20 rows/page)      │ │                           │                    │
│ └────────────────────────┘ └───────────────────────────┘                    │
├────────────────────────────────────────────────────────────────────────────┤
│ ↳ 1 pair over α=0.01 threshold (flag) · 2 over display_threshold            │
└────────────────────────────────────────────────────────────────────────────┘
```

对比弹窗（`enter` 或 `c` 进入，`ModalScreen`，最大 96×30）：

```
┌────────────────────────────────────────────────────────────────────────────┐
│ ⚖ Compare: 0142.py ↔ 0156.py   max_sim 91.2%  token_overlap 340   z 5.21 FLAG
├──────────────────────────────────┬─────────────────────────────────────────┤
│ 0142.py (processed)               │ 0156.py (processed)                    │
│ 1  def score(data):               │ 1  def score(data):                    │
│ 2      agg = [x for x in data]    │ 2      agg = [x for x in data]         │
│ 3      if not agg: return 0.0     │ 3      if not agg: return 0.0          │
│ 4      return sum(agg)/len(agg)   │ 4      return sum(agg)/len(agg)        │
│ 5  ...similar lines highlighted[reverse]...                               │
├──────────────────────────────────┴─────────────────────────────────────────┤
│ [c]Copy line  [o]Open file ($EDITOR)  [esc]Close                             │
└────────────────────────────────────────────────────────────────────────────┘
```

## 2. 组件清单

| 标注 | 组件（Textual 8.x） | 用途 |
|------|--------------------|------|
| Tab 容器 | `TabbedContent` + `TabPane`×3（`#pane-pairs` / `#pane-aggregate` / `#pane-cross-course`） | 单作业对 vs 课程内聚合 vs **跨课程聚合**（跨课程 Tab 仅当有 cross-course 报告时出现） |
| 排名表 | `DataTable`×2 | 视图 A：文件对（sim/重叠）；视图 B：学生对（raw sim/z/p） |
| 判定列 | 单元格 `Static`（`flag`/`flag-warn` class） | 视图 A 用 `display_threshold`(80%)；视图 B 用聚合 `alpha` 阈值 |
| 顶部按钮 | `Button`×2 | `[p]` 运行检测（复用 S2 job 协议）；`[a]` 运行聚合（`--aggregate`） |
| 状态行 | `Static` | 汇总：对总数/疑点数/阈值说明 |
| 对比弹窗 | `ModalScreen` + 2×`VerticalScroll`（并排） | 并排文本 + 相似片段高亮；用 `Grid` 两列 |
| （复用） | `RichLog`（弹窗内不重复） | 检测运行时隐藏于底部，进度走 S2 协议 |

## 3. z 分数双呈现设计（验收关键）

| 视图 | 列 | 含义 | 置信来源 |
|------|----|------|---------|
| A 单作业对 | `max_similarity_pct` / `token_overlap` / 判定 | 该作业内部排名的原始相似度 | `all_pairs.json` 原样 |
| A·嵌入列（可选 tab 内二次 Tab） | `max_similarity_pct`（embedding） | 嵌入余弦相似度（top-N 对） | `all_pairs.embedding.json` |
| B 聚合报告 | `raw_similarity_pct` / `z_score` / `one_sided_p_value` / 判定 | logit 变换→per-assignment z 标准化→跨作业 Stouffer 合并；**主体是聚合 z，不是原始分** | `plagiarism_aggregate` MatchRecord + combined stat |

明确标注（表头与状态行）：“⚠ 聚合 z 为跨作业归一化信号，`raw_similarity_pct` 为原始值”——避免把 88% 误当绝对证据（呼应 aggregate 源码注释“triage signal, not proof”）。

**判定标记规则：**
- 视图 A：`max_similarity_pct ≥ display_threshold(80)` → `◆ 显示级`；`≥ 90` → 追加 `↑`
- 视图 B：`one_sided_p_value < alpha(0.01)` → `◆ 疑点`（红色 `flag` class）；仅预览 `z_score ≥ 3` 且未超 alpha → `? 关注`

## 4. 交互流

```
进入 S4（Tab 切到 plagiarism）→ 读 state.current_course / state.current_assignment
  ├─ 有 current_assignment → 上下文绑定该作业（视图 A 用该作业的 all_pairs.json）
  ├─ 只有 current_course → 上下文绑定该 course（视图 A/B 显示 course 内聚合：所有作业 pairs）
  ├─ 无 course → 显示 Global 提示：「请先在 Dashboard 进入某课程再使用查重屏幕」
        （例外：若存在 data/plagiarism-cross-course.json，顶部出现跨课程 Tab 并可查看）
  ├─ all_pairs.json 存在 → 解析 → 视图 A 填充（按 max_similarity_pct 降序，默认 20 行）
  ├─ 聚合 JSON 存在 → 视图 B 填充；不存在 → 视图 B 空态 + [a] 按钮高亮
  ├─ 作业未运行检测 → 视图 A 空态 + [p] 按钮高亮
行选中 + enter/c → 解析两个文件（优先 processed/*.md，回退 raw 转换，复用 _preview_content 逻辑）
   → 计算相似片段（token_overlap 行集合 → 高亮首 200 行）→ push_screen(CompareModal)
   → o 发起 $EDITOR 打开原文件；esc 关闭返回表格
[a] 运行聚合 → job（后台：读本 course 各作业 all_pairs*.json → 合并统计 → 写聚合 JSON/MD）→ 完成后视图 B 刷新 + notify
[p] 运行检测 → job（与 S2 的 K 按钮同一协议）→ 完成后视图 A 刷新
[跨课程 Tab]（来自 Global 层 p 或 CLI --cross-course）→ 读 data/plagiarism-cross-course.json → 视图 C 填充
```

## 5. 键盘映射表

| 键 | 动作 | 说明 |
|----|------|------|
| `tab` | Switch (pairs / aggregate / cross-course) | TabbedContent 原生 |
| `↑/↓` 或 `j/k` | Move in table | |
| `enter` 或 `c` | Open compare modal | 仅视图 A 且行有文件对 |
| `o` | Open selected file in `$EDITOR` | 弹窗焦点内有效 |
| `·`/`e` | Copy row info to clipboard | `copy_to_clipboard`（通知确认） |
| `p` | Run / re-run detection | 与 S2 `k` 同协议 |
| `a` | Run aggregation | 需当前 course config 的 `[[fetch.assignments]]` 存在，否则 notify 提示缺失（引导去 Course 视图导入作业） |
| `r` | Reload data files | 外部改动后刷新 |
| `esc` | Close modal / back to previous tab | |
| `?` | Help | 全域 |

## 6. 空态 / 错误态 / 加载态

| 态 | 表现 |
|----|------|
| **空态·未运行检测** | 视图 A 居中 Static：「No pairs yet. Run plagiarism (`p`) on the assignment or `[a]` for course aggregation.」 |
| **空态·无对** | `pair_count == 0`（模板单文件/无参考匹配）：「Detection done, 0 pairs (submissions <2 or all below threshold).」 |
| **空态·聚合缺失** | 视图 B：Static「No aggregate report yet. Run `a` for cross-assignment z-scores (needs `[[fetch.assignments]]` in the course config).」 |
| **空态·course config 缺作业清单** | `[a]` 点击时 `notify(error, "data/<course>/config.toml missing [[fetch.assignments]]")`，引导去 S1·Course 导入作业 |
| **空态·无跨课程报告** | 视图 C：Static「No cross-course report yet. Run from the Global view (`p`) or `main.py plagiarism --cross-course`.」 |
| **加载态·检测运行中** | 视图 A 顶部行 `Static`「Detecting: 12/18 …」+ 不定态 ProgressBar；表数据加载完成后替换 |
| **错误态·JSON 损坏** | 该视图显示「Load failed: <err>」+ `notify(error)`；建议 `p` 重跑 |
| **错误态·嵌入模型下载失败** | 日志红字（模型首次下载可达数 GB）→ 明确提示「Embedding model not available locally; copydetect partial only」；`notify(warning)` |
| **错误态·聚合数据不一致** | 某作业 pair 文件缺失 → 聚合跳过该作业并在状态行注明「Skipped: <assignment> (missing all_pairs.json)」 |
