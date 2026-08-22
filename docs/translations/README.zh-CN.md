<!-- 中文版 — 随 v0.76 生成；版本升级后可能滞后。 -->
<h1 align="center">codeloom</h1>

<p align="center">
  <b>在一秒内为你的 AI 编码代理提供一张仓库地图——以及能扛过上下文压缩的记忆。</b><br/>
  单文件 · 零依赖 · 无守护进程 · 100% 本地 · MIT
</p>

<p align="center">
  <a href="https://github.com/sloemo01/codeloom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8%2B-blue"/></a>
  <a href="https://github.com/sloemo01/codeloom/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-passing-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom#readme"><img src="https://img.shields.io/badge/deps-zero-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom/stargazers"><img src="https://img.shields.io/github/stars/sloemo01/codeloom"/></a>
</p>

<p align="center">
  <a href="#quickstart">快速开始</a> ·
  <a href="#what-it-gives-your-agent">功能特性</a> ·
  <a href="#mcp-server-75-tools--1-router">MCP</a> ·
  <a href="#pr-review-bot">PR 机器人</a> ·
  <a href="#why-its-different">与竞品对比</a> ·
  <a href="#documentation">文档</a>
</p>

---

## 问题所在：代理不只是烧 tokens——它们还会遗忘

每个 AI 编程代理都有同一个问题：在它真正开始 *做事* 之前，它必须先弄清楚
你的代码库到底 *是什么*。于是它 grep、通读整个文件，
烧掉 40,000+ tokens 来构建上下文。然后一次上下文压缩把这一切全部抹掉——
它又从零开始重新推导一遍。一遍又一遍。

**codeloom 把这两半问题都解决了。**

1. **地图** —— 一条命令生成你仓库的紧凑结构模型（文件夹树 + 模块单行摘要 + 入口点 +
   导入图 + 调用图），代理一秒即可读完。
2. **记忆** — `--decide`、`--checkpoint`、`--resume` 记录代理的决策流，
   让 `--resume` 在任意一次压缩之后，同时恢复*结构上下文*和
   代理已经尝试过、决定过、否决过的内容。

无需安装。无守护进程。无需 GPU。无遥测。100% 运行在你自己的机器上。

## 快速开始

```bash
# Option A: copy the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py

# Option B: pip
pip install codeloom

# Map any repo (<1s to first result)
python3 codeloom.py /path/to/repo > AGENTS.md

# Tell your agent: "read AGENTS.md first"
```

原生接入你的代理（支持 17 种）：

```bash
python3 codeloom.py --install-agent claude-code   # or cursor, codex, gemini-cli,
                                                  # opencode, cline, aider, ...
```

## 它能为你的代理做什么

### 面向任务的工具（护城河）

| 命令 | 代理获得什么 |
|---|---|
| `--pack "TASK"` | **一次性简报**：阅读顺序 + 影响面 + 相关符号，预先算好 |
| `--answer "Q"` | 带校准置信度的引用答案 |
| `--context-card S1 S2` | 一次调用为 N 个符号生成批量分诊卡片 |
| `--why QUERY` | 决策查询，标注 `[exact]`/`[fuzzy]`/`[unverified]` |
| `--plan TASK` | 代理原生、按优先级排序的阅读计划 |

### 跨压缩的工作记忆（没有别人做过）

```bash
codeloom --decide "use retry(3) not retry(∞) — unbounded hangs agents"
codeloom --checkpoint --task "fix login bug"     # save working state
codeloom --resume                                 # restore after compaction
```

此外还有：`--remember`、`--seen`、`--working-state`、`--lessons`、`--supersede`、
`--adr`、`--query-memory`。

### 结构化智能

| 命令 | 结果 |
|---|---|
| `--graph` | 完整导入图（385 个模块、1126 条边，<1s） |
| `--cross` | 跨文件调用图，AST 解析 |
| `--search` / `--usages` / `--grep` / `--read` | 符号索引、调用点、代码片段、token 高效的源码 |
| `--get-symbol X` | 摘要优先检索（节省约 95–99% tokens） |
| `--impact M` / `--refactor` / `--rename` | 爆炸半径预测 |
| `--similar` / `--deadcode` / `--explain` | 重构智能，零 LLM |
| `--trace` | 静态分析看不到的运行时调用边 |
| `--routes` / `--channels` | HTTP 路由、pub-sub 事件通道 |
| `--pattern '$F($$$ARGS)'` | 带元变量捕获的**结构化 AST 搜索** |

### 速度与质量

| 命令 | 结果 |
|---|---|
| `--health` | 代码健康度面板：每文件 0–10 分，**0.2 秒**，确定性检测器 |
| `--risk HEAD~1..HEAD` | 任意提交范围的变更风险评分 0–100 + 具名驱动因素 |
| `--embed-search Q` | 离线语义搜索——子词哈希，零依赖（ggml 可选） |
| `--watch` → `--watch-merge` | 实时保鲜：原生 watcher 接入持久化索引 |
| `--engine c` | 自动构建的 C 核心：约 91 秒跑完 Linux 内核全图 |
| `--verify FILE` | SHA-256 校验和验证 |

**50 种 tree-sitter 语言已调度 · 46 种经 fixture 验证**（golden-file 一致性测试
在每个文法上把关 CI）· **100+ 扩展名通过正则回退支持**。

## MCP 服务器（78 个工具 + 1 个路由器）

```json
{"command": "python3", "args": ["-m", "codeloom_mcp"]}
```

或者自动接入 17 种代理中的任意一种：`codeloom --install-agent <name>`。

总共 78 个工具，但代理的有效面只有一个**工具**：
`codeloom_ask` 接收自然语言并确定性路由——
不会出现工具选择失误。完整清单：
[`docs/mcp-listing.md`](docs/mcp-listing.md)。

## PR 审查机器人

`.github/workflows/pr-bot.yml` 把每一个 pull request 变成：

1. **行级内联评论**，精确落在 diff 对应位置：
   - **P1** 安全问题（`eval`/`exec`、硬编码密钥），**P2**（不安全的 http、
     `shell=True`），**P3**（孤立的新符号、TODO/FIXME 标记）
2. **置顶总结评论**（每次推送更新）：风险结论 0–100 并附驱动因素、
   diff 摘要、涉及文件的健康度、自适应审查清单、
   审查者的起始上下文
3. **风险标签**：`risk:low/medium/high/critical`，自动轮换
4. **交接**：`@codex` 在我们之后执行它的 LLM 检查，聚焦
   语义/逻辑/设计（我们的确定性类别已被覆盖）

第一关零 LLM 成本。适用于任何 GitHub 仓库——复制 workflow 文件即可。

## 为什么它不一样

完整带源码引用的对比矩阵：[`docs/COMPETITION.md`](docs/COMPETITION.md)。
与同类工具对比的摘要（从它们的仓库逐一验证，crg 实时实测
2026-08-22 —— 数据见 [`benchmarks/README.md`](benchmarks/README.md)）：

| | **codeloom** | code-review-graph (30.6k★) | code-context-engine | claude-context |
|---|---|---|---|---|
| 安装 | **一个 stdlib 文件** | pip：**78 个包** + 守护进程 + TOML 配置 | pip + ONNX + 服务器 | npm |
| 后台进程 | **无** | `crg-daemon`（16MB RSS，健康检查） | `cce serve` + 资源治理 | — |
| 压缩后记忆 | ✅ **决策台账，实测：2 次调用 / 777 个 token 即可恢复**（少 97.9%） | ⚠️ markdown 问答日志，零压缩提及 | ⚠️ 代理调用的 `record_decision` MCP | memsearch 插件 |
| MCP 表面 | **78 + 1 个自然语言路由器** | 30，无路由器 | 22 | 很多 |
| 语义搜索 | ✅ 零依赖、离线 | ❌ `[embeddings]` 附加项（约 2GB）或云端密钥 | ❌ 需要 ONNX | ✅ (Zilliz) |
| 语言证明 | **46 种语言在 CI 中经 fixture 验证** | 未公布 | — | — |
| 安装→出答案 | **0.105 秒热启动** | 41 秒 pip + 4 秒构建 + 守护进程 | 建索引后 | 建索引后 |

实测数据：符号检索比 crg 少 24–36 倍 tokens；压缩恢复
**少 97.9% tokens**；Linux 内核完整图约 91 秒。细节和
复现命令见 [`benchmarks/README.md`](benchmarks/README.md)。

竞品领先的地方，直说无妨：jcodemunch 有更广的安全预检
（编辑/删除安全、SCIP 编译器验证）；codegraph 有 67k★ 的
社区规模；codebase-memory 提供 158 种文法和一个 arXiv 发表的
评测；repowise（AGPL）有经过缺陷验证的风险评分。我们主张的是
速度 + 形态 + 每种文法的证明 + 记忆深度——而不是它们的护城河。

## 已知局限（坦诚）

- Python 获得最深入的分析（stdlib `ast`）；其他语言用
  tree-sitter 大纲 + 正则回退。
- 健康度/风险是结构性启发式——**并未**针对带标签的语料库
  做缺陷验证（这是 code-review-graph 的护城河；我们直说而不是夸大）。
- 活体代理的 token 节省基准已设计但尚未证明——我们公布的
  数字是静态回放，且包含缺失行
  （[`bench/RESULTS.md`](bench/RESULTS.md)）。
- 神经嵌入需要一个可选的 ggml/model 安装；不装的话你会得到
  零依赖的子词哈希（仍然离线，仍然能捕捉拼写错误）。

## 文档

| 文档 | 内容 |
|---|---|
| [`CAPABILITIES.md`](CAPABILITIES.md) | codeloom 能做的一切 |
| [`USER_GUIDE.md`](USER_GUIDE.md) | 实操演练 |
| [`CLI.md`](CLI.md) | 每个旗标的解释 |
| [`FEATURES.md`](FEATURES.md) | 战略性功能地图 |
| [`SECURITY.md`](SECURITY.md) | 信任模型与验证 |
| [`docs/COMPETITION.md`](docs/COMPETITION.md) | 带源码引用的竞品对比矩阵 |
| [`docs/FAQ.md`](docs/FAQ.md) | "vs LSP/RAG/repomix/code-review-graph"——诚实的取舍 |
| [`docs/mcp-listing.md`](docs/mcp-listing.md) | MCP 市场列表文案 |
| [`bench/RESULTS.md`](bench/RESULTS.md) | 回放基准结果（缺失行已公布） |
| [`BENCHMARKS.md`](BENCHMARKS.md) | 实测性能数字 |
| [`TECHNICAL_REPORT.md`](TECHNICAL_REPORT.md) | 架构决策文档 |
| [`AGENT_TRACE.md`](AGENT_TRACE.md) | 代理任务前后轨迹 |

## 信任与验证

- **CI**：Linux/macOS/Windows × Python 3.8–3.12，75 个测试，≥45 个文法
  fixture 由 golden files 把关
- **校验和**：每个版本公布 `codeloom.py` 的 SHA-256；
  用 `codeloom --verify codeloom.py` 验证
- **可审计**：单个 stdlib 文件——运行之前通读全文

## 参与贡献

欢迎 PR。用 `python3 tests.py` 运行测试。信条：零依赖、快速、
单文件、诚实的声明。

## 代理技能

一个开箱即用的、用于使用和维护 codeloom 的技能随附在
[`skills/codeloom/SKILL.md`](skills/codeloom/SKILL.md) — 每个旗标、MCP
接入、测试套件、重新录制演示 GIF，以及如何扩展这个工具。
安装到你的代理的技能目录（例如
`~/.hermes/skills/software-development/codeloom/`）。

## License

MIT — do whatever you want with it.

---

*Built for people who'd rather their AI agent ship code than spend 15 minutes re-reading a 40k-LOC repo after every compaction.*
