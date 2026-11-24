# Test Pulse — 大仓 CI 智能选词与不稳定测试治理

用 Python 标准库 + SQLite + 原生 JavaScript 实现的可运行原型。它会模拟一个类似真实大仓的环境：

- **20,400 个测试**、306 个源码/平台模块、模块依赖图和历史覆盖关系；
- **半年 / 约 500 次提交**的代码变更、CI 运行、失败、自动重试与人工重跑记录；
- 埋入真实“提交弄坏测试”、真 flaky、以及“先 flaky、后来被提交弄坏”的 ground truth；
- 回放基于改动文件、依赖反向闭包、历史覆盖和共同失败概率的测试选择；
- 估计漏检比例，并用每日夜间全量作为安全兜底；
- 识别同 SHA 既过又不过的不稳定测试，给出 flake 分数、原因分类和通过率曲线；
- 对 flaky 测试隔离、继续执行记录、持续确定性失败升级、连续绿色后自动放回；
- 提供每次提交选词原因、不稳定测试列表、隔离事件和开发者报告页面。

无第三方 Python 依赖；不需要 React、npm 或外部图表服务。

## 快速开始

```bash
# 1. 生成模拟仓库、半年提交和 CI 历史（约 15 秒）
python3 -m ci_optimizer generate

# 2. 回放选词/隔离策略并生成 data/report.md（约 3 分钟）
python3 -m ci_optimizer replay

# 或一步完成并打开仪表盘
python3 -m ci_optimizer demo --serve --port 8765
```

然后访问 <http://127.0.0.1:8765/>。

也可以用小规模数据快速自检：

```bash
python3 -m ci_optimizer demo \
  --tests 1200 --days 45 \
  --db /tmp/test-pulse-small.sqlite3 \
  --no-materialize
```

## 页面说明

- **总览**：平均选词数、跳过比例、PR 反馈耗时、耗时下降、漏检率、flaky 识别 P/R/F1、策略对比和选词量趋势。
- **提交与选词**：选择任意提交，查看改动文件、选中测试、风险排序分数和原因标签；支持测试名/owner/原因搜索与分页。
- **不稳定测试**：按系统状态或模拟真值筛选；点击任意测试查看每日通过率曲线、同 SHA 混合次数、翻转次数、原因证据、最近失败上下文和隔离时间线。
- **隔离与事件**：`quarantined`、`escalate_broken`、`released`、`broken_resolved` 等完整事件流。
- **报告**：对应每次运行后可发给开发者的 Markdown 报告。

## 选词模型

一次提交会先把文件映射到模块，然后组合以下信号：

1. **历史文件覆盖**：测试的 coverage edge 曾覆盖本次改动模块；
2. **模块依赖图**：在反向依赖图上做 BFS，选择所有可能消费改动模块的下游；
3. **同域安全网**：安全档纳入改动业务域的测试；
4. **历史共同失败**：按 `module:<id>`、`domain:<name>`、`layer:<name>`、变更类型和规模构造 change bucket，统计 `P(test fails | bucket changed)` 的稀疏 Beta 后验；
5. **风险预测 Top Pockets**：跨模块扫描高风险测试，避免漏掉没有覆盖/图边的候选；
6. **近期失败与隔离观察**：最近失败测试和所有 quarantined/broken-suspect 测试每次仍执行；
7. **残余风险预算**：对未选中的测试汇总结构距离、共同失败后验和稳定先验，在线用夜间全量反馈校准。

有两个对比档：

- **快速档**：只做直接覆盖、一级反向依赖、精简冒烟和风险 Top 候选，测试更少但漏检更高；
- **安全档（PR 默认）**：完整反向闭包、同域兜底、完整冒烟、风险候选和残余风险预算；
- **夜间全量**：每天跑全部 20,400 个测试，补抓 PR 选词漏网，并校准后续模型。

## 漏检估计

系统同时展示三种口径：

- 模型的每次提交期望漏检数；
- 基于夜间全量反馈的滚动漏检比例；
- Wilson 95% 漏检率上界。

评估时还区分：

- **新引入真实失败漏检率**：破坏提交当次没有选中对应失败测试；
- **全部真实失败漏检率**：包括后续提交上的重复失败。由于“近期失败必选”，同一破坏在首次漏检后通常不会继续漏选；
- **摊薄夜间全量成本后的 CI 资源下降**：这是资源口径，不是开发者 PR 等待时间口径。

## Flaky 判定与原因分类

核心证据不是“偶尔变红”，而是：

- 同一 commit SHA 的首跑失败、重跑通过（mixed SHA）；
- 相邻提交状态反复翻转；
- 失败在时间上离散，而不是从某提交边界开始连续高失败率；
- 重跑恢复次数和 Beta/频率信号。

系统会搜索持续性失败边界和高密度失败区间，从而区分：

- `flaky`：无明确破坏边界，不稳定证据充分；
- `broken`：从某提交开始确定性/高密失败；
- `flaky_then_broken`：先有离散 flaky 历史，之后出现明确持续破坏区间；
- `broken_episode`：曾被提交弄坏但后来修复。

原因分类使用执行上下文，而不是随机标签：

- `order_dependence`：失败高度集中于某个前置测试；
- `concurrency`：高负载/高并发分片下失败率显著升高；
- `time_or_timeout`：深夜、跨日、月末窗口或 `TimeoutError` 集中；
- `shared_state`：唯一约束、缓存、夹具状态泄漏等证据；
- `external_dependency`：网络/上游连接错误且重试常恢复。

## 隔离与自动恢复策略

- 同一 SHA 反复出现失败/通过混合证据后进入 `quarantined`；
- 隔离测试仍进入 allow-failure 观察任务，继续执行和记录，但失败不阻塞合并；
- 若出现连续确定性失败，改判/升级为 `broken_suspect` 并通知负责人，避免把真问题长期混入 flaky；
- flaky 测试连续 40 次运行绿色后自动 `released`；
- 放回后再次出现强混合证据会自动 `requarantined`；
- broken suspect 连续 20 次绿色后发出 `broken_resolved`。

## 模拟数据与 Ground Truth

生成器使用固定随机种子 `20261125`，因此结果可复现。主要埋点：

- 54 个普通提交破坏事件，每个影响 1–15 个测试，大多数会在后续修复；
- 280 个真值 flaky 测试，覆盖五类原因；
- 约 40 个“先 flaky、后来被提交弄坏”的转变测试；
- 约 52 个被修复后应自动解除隔离的 flaky 测试；
- 覆盖采集器故意漏掉少量 coverage edge，以模拟真实覆盖盲区。

数据库中的 `tests.truth_flaky`、`tests.archetype_gt`、`tests.broken_seq`、`incidents` 和 `incident_tests` 是评估用 ground truth。真实部署中不会有这些列，仪表盘保留它们是为了让演示能够直接验证识别准确度。

## 常用命令

```bash
# 重新生成全量数据
python3 -m ci_optimizer generate

# 仅重新回放（修改选词/隔离策略后很有用）
python3 -m ci_optimizer replay

# 重新生成 Markdown 报告
python3 -m ci_optimizer report

# 启动仪表盘
python3 -m ci_optimizer serve --host 127.0.0.1 --port 8765

# 小规模快速生成
python3 -m ci_optimizer demo --tests 1000 --days 45 --db /tmp/small.db --no-materialize
```

## 代码结构

```text
ci_optimizer/
  config.py            # 规模、路径、CI 并发参数
  db.py                # SQLite schema
  synthetic_repo.py    # 模块/依赖图/20k 测试/覆盖关系/模拟仓库文件
  history.py           # 半年提交、破坏事件、flaky 行为、运行与重试
  selector.py          # 依赖闭包、覆盖、共同失败、风险排序、漏检估计
  quarantine.py        # 增量隔离/升级/自动恢复状态机
  stats.py             # 批量 flake 判定、边界检测、原因分类、准确度评估
  simulator.py         # 按提交时间回放并写 selection/metrics
  report.py            # Markdown 报告
  web.py               # 标准库 HTTP API + 静态资源
  static/              # 原生 JS/CSS/SVG 仪表盘
tests/                 # 小型端到端与策略测试
data/                  # 生成的 SQLite/metrics/report（git 忽略）
generated_repo/        # 生成的模拟源码与测试树（git 忽略）
```

## 诚实的限制

这是一个高保真机制原型，不是可直接接入生产 CI 的完整服务：

- 测试结果由统计模型生成，不执行 20k 个真实测试；
- 覆盖和依赖图虽然按真实结构模拟，但生产中应从 coverage 上传、BUILD 文件和 import graph 更新；
- Wilson 上界校准依赖夜间全量结果及时回流；
- 前端图表使用手写 SVG，避免外部 CDN；
- 全量 SQLite 约 200–700 MB（取决于是否保留完整 reason detail），因此 `data/` 不提交到 git。
