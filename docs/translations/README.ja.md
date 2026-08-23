<!-- 日本語版 — v0.79 で生成、バージョンアップで遅れる可能性あり -->

<h1 align="center">codeloom</h1>

<p align="center">
  <b>AI コーディングエージェントに、1 秒でリポジトリの地図を — そしてコンパクションを生き延びるメモリを。</b><br/>
  単一ファイル · 依存ゼロ · デーモン不要 · 100% ローカル · MIT
</p>

<p align="center">
  <a href="https://github.com/sloemo01/codeloom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8%2B-blue"/></a>
  <a href="https://github.com/sloemo01/codeloom/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-passing-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom#readme"><img src="https://img.shields.io/badge/deps-zero-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom/stargazers"><img src="https://img.shields.io/github/stars/sloemo01/codeloom"/></a>
</p>

<p align="center">
  <a href="#quickstart">クイックスタート</a> ·
  <a href="#what-it-gives-your-agent">機能</a> ·
  <a href="#mcp-server-82-tools--1-router">MCP</a> ·
  <a href="#pr-review-bot">PR ボット</a> ·
  <a href="#why-its-different">競合との比較</a> ·
  <a href="#documentation">ドキュメント</a>
</p>

---

## 問題: エージェントはトークンを消費するだけでなく、忘れる

どの AI コーディングエージェントにも同じ問題がある: 何か *する* 前に、まずコードベースが一体 *何なのか* を
把握しなければならない。そこで grep し、ファイル全体を読み、コンテキスト構築のために
40,000 トークン以上を消費する。そしてコンテキストのコンパクションがそのすべてを消し去り、
最初から全部を導き直す。それを何度も繰り返す。

**codeloom はその両方を解決する。**

1. **地図** — 1 コマンドで、リポジトリのコンパクトな構造モデルを生成する
   (フォルダツリー + モジュールの一言説明 + エントリポイント + import グラフ + call グラフ)。
   エージェントは 1 秒で読める。
2. **メモリ** — `--decide`、`--checkpoint`、`--resume` がエージェントの決定の流れを
   記録し、`--resume` が構造コンテキスト *と* エージェントがすでに試し、決定し、却下した
   内容の *両方* を、どのコンパクションの後でも復元する。

インストール不要。デーモンなし。GPU 不要。テレメトリなし。100% マシン上だけで動作する。

## Quickstart

```bash
# Option A: copy the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py

# Option B: pip
pip install codeloom

# Map any repo (<1s to first result)
python3 codeloom.py /path/to/repo > AGENTS.md

# Tell your agent: "read AGENTS.md first"
```

ネイティブにエージェントへ組み込む(17 種類対応):

```bash
python3 codeloom.py --install-agent claude-code   # or cursor, codex, gemini-cli,
                                                  # opencode, cline, aider, ...
```

## What it gives your agent

### タスク指向のツール(最大の強み)

| コマンド | エージェントが得るもの |
|---|---|
| `--pack "TASK"` | **ワンショットのブリーフ**: 読み順 + 影響範囲 + 関連シンボルを事前計算 |
| `--answer "Q"` | 出典付きの回答、校正された確信度付き |
| `--context-card S1 S2` | 1 回の呼び出しで N 個のシンボルを一括トリアージするカード |
| `--why QUERY` | `[exact]`/`[fuzzy]`/`[unverified]` の印が付いた決定の検索 |
| `--plan TASK` | エージェント向けの優先順位付きリーディングプラン |

### コンパクションをまたぐ作業メモリ (他社は提供していない)

```bash
codeloom --decide "use retry(3) not retry(∞) — unbounded hangs agents"
codeloom --checkpoint --task "fix login bug"     # save working state
codeloom --resume                                 # restore after compaction
```

さらに: `--remember`、`--seen`、`--working-state`、`--lessons`、`--supersede`、
`--adr`、`--query-memory`。

## Memory OS — グラフがあなたの代わりに覚える

**v0.79** で、各メモリは `.codeloom-memory/memory.jsonl` に追記される**型付きレコード**（型・重要度・信頼度・関連シンボル）になり、取得は**グラフ連動**です: `--memory <シンボル>` はそのシンボルに言及するエントリ*に加えて*、グラフ上の近傍（依存先・依存元・呼び出し元）に紐づいたメモリを重要度順で返します。日誌と、コードベースの形状に配線されたメモリとの違いです。

### 構造分析

| コマンド | 結果 |
|---|---|
| `--graph` | 完全な import グラフ (385 モジュール、1126 エッジを 1 秒未満で) |
| `--cross` | AST 解決済みのファイル横断 call グラフ |
| `--search` / `--usages` / `--grep` / `--read` | シンボル索引、呼び出し箇所、スニペット、トークン効率の良いソース |
| `--get-symbol X` | 要約優先の取得 (~95–99% のトークン削減) |
| `--impact M` / `--refactor` / `--rename` | 影響範囲(ブラスト半径)の予測 |
| `--similar` / `--deadcode` / `--explain` | リファクタリング分析、LLM 不要 |
| `--trace` | 静的解析では見えない実行時呼び出しエッジ |
| `--routes` / `--channels` | HTTP ルート、pub-sub イベントチャネル |
| `--pattern '$F($$$ARGS)'` | **メタ変数キャプチャ付き構造的 AST 検索** |

### 速度と品質

| コマンド | 結果 |
|---|---|
| `--health` | コードヘルス画面: ファイルごとに 0〜10、**0.2 秒**、決定的な検出器 |
| `--risk HEAD~1..HEAD` | 任意のコミット範囲に対する変更リスクスコア 0〜100 + 原因の名前 |
| `--embed-search Q` | オフラインのセマンティック検索 — サブワードハッシュ、依存ゼロ (ggml はオプトイン) |
| `--watch` → `--watch-merge` | ライブ鮮度: ネイティブウォッチャーが永続インデックスにパイプ接続 |
| `--engine c` | 自動構築の C コア: Linux カーネル完全グラフを約 89–113 秒で (C エンジン) |
| `--verify FILE` | SHA-256 チェックサムの検証 |

**50 言語の tree-sitter をディスパッチ · 46 言語をフィクスチャで実証済み** (ゴールデンファイル
一致テストが全グラマーの CI をゲート) · **regex フォールバックで 130+ 拡張子に対応**。

## MCP server (82 tools + 1 router)

```json
{"command": "python3", "args": ["-m", "codeloom_mcp"]}
```

または 17 種類のエージェントのいずれかに自動配線: `codeloom --install-agent <name>`。

ツールは全部で 82 個だが、エージェントが実際に触れる面は**ツール 1 個**:
`codeloom_ask` が自然言語を受け取り、決定的にルーティングする —
ツール選択のミスは起きない。完全な一覧は
[`docs/mcp-listing.md`](docs/mcp-listing.md)。

v0.79 の MCP サーフェスには **Memory OS トリオ** が加わる: `codeloom_memory_add`（重要度付きの型付きメモリ）、`codeloom_remember`（グラフ連動の取得）、`codeloom_memory_stats`（分布レポート）。`codeloom_ask` から memory/remember/stats キーワードでルーティングされる。

## PR review bot

`.github/workflows/pr-bot.yml` はすべてのプルリクエストを次の形に変える:

1. **diff の正確な位置にピン留めされたインラインコメント**:
   - **P1** セキュリティ (`eval`/`exec`、ハードコードされたシークレット)、**P2** (安全でない
     http、`shell=True`)、**P3** (孤立した新規シンボル、TODO/FIXME マーカー)
2. **要約コメントを固定表示** (プッシュごとに更新): リスク判定 0〜100 + 原因、
   diff のダイジェスト、変更ファイルのヘルス、適応型レビューチェックリスト、
   レビュアーの初期コンテキスト
3. **リスクラベル**: `risk:low/medium/high/critical`、自動ローテーション
4. **引き継ぎ**: `@codex` が LLM パスを実行する、セマンティクス/ロジック/デザインに限定
   (決定論的カテゴリはすでにカバー済み)

ステージ 1 は LLM コストゼロ。どの GitHub リポジトリでも動作 — ワークフローファイルをコピーするだけ。

## Why it's different

出典付きの完全な比較マトリクス: [`docs/COMPETITION.md`](docs/COMPETITION.md)。
競合に対する要約 (競合のリポジトリから検証済み、crg の計測は 2026-08-22 に実行 —
数値は [`benchmarks/README.md`](benchmarks/README.md) 参照):

| | **codeloom** | code-review-graph (30.6k★) | code-context-engine | claude-context |
|---|---|---|---|---|
| インストール | **stdlib ファイル 1 個** | pip: **75 パッケージ** + デーモン + TOML 設定 | pip + ONNX + サーバー | npm |
| バックグラウンドプロセス | **なし** | `crg-daemon` (16MB RSS、ヘルスチェック) | `cce serve` + リソースガバナー | — |
| コンパクション後のメモリ | ✅ **決定台帳、計測値: 復旧に 2 呼び出し / ~985 トークン** (95.4% 削減) | ⚠️ markdown の Q&A ジャーナル、コンパクションの言及ゼロ | ⚠️ エージェントが呼ぶ `record_decision` MCP | memsearch プラグイン |
| MCP サーフェス | **82 + 1 NL ルーター** | 30、ルーターなし | 22 | 多数 |
| セマンティック検索 | ✅ 依存ゼロ、オフライン | ❌ `[embeddings]` オプション (~2GB) またはクラウドキー | ❌ ONNX 必須 | ✅ (Zilliz) |
| 言語実証 | **CI で 46 言語をフィクスチャ実証** | 非公開 | — | — |
| セットアップから回答まで | **ウォーム 0.13 秒** | pip 41 秒 + ビルド 4 秒 + デーモン | インデックス化後 | インデックス化後 |

計測値: シンボル取得は crg より 43〜54 倍少ないトークン; コンパクション復旧は
**95.4% トークン削減**; Linux カーネル完全グラフ約 89–113 秒 (C エンジン)。詳細と再現コマンドは
[`benchmarks/README.md`](benchmarks/README.md)。

**自家製の正直な比較（dogfood、2026-08-23、pallets/flask、同一セッション）**: 小さなリポジトリで codeloom と素の grep+read エージェントを比較しましたが、結果は正直に言って五分五分です。ターミナルペイロード計測では、codeloom は素の grep+read より**総トークンが多く（+14.5%）**、所要時間も長め（+2.6 倍）でした。勝ったのはエビデンス面です — `--impact` は爆発半径（0.23 秒で直接 5 + 間接 33）を、`--task` は正確に 4 モジュールをランク付けし、`--checkpoint`/`--checkpoint-restore` は編集 diff を完全に再現しました。上記のトークン効率の主張（98.9%、43〜54 倍）は**大きなリポジトリと grep+read ベースラインに対する呼び出し連鎖**で成立します — それが測定された範囲であり、小さな単発タスク向けではありません。

競合が優れている点を正直に言う: jcodemunch はより広いセーフティ事前チェック
(編集/削除安全、SCIP コンパイラ検証) を持つ; codegraph は 67k★ の
コミュニティ規模を持つ; codebase-memory は 158 グラマーと arXiv 公開の
評価を出荷している; repowise (AGPL) は欠陥検証済みのリスクスコアリングを持つ。
私たちが主張するのは速度 + 形状 + グラマーごとの実証 + メモリの深さ — 彼らの強みではない。

## 既知の制限 (正直に)

- Python が最も深い分析を受ける (stdlib `ast`); 他の言語は
  tree-sitter の概要 + regex フォールバック。
- ヘルス/リスクは構造的なヒューリスティック — ラベル付きコーパスに対する
  欠陥検証済み**ではない** (repowise の強み; 誇張せずそう明記する)。
- ライブエージェントのトークン削減ベンチマークは設計はしたが未検証 —
  公開数値は損失行を含む静的リプレイ
  ([`bench/RESULTS.md`](bench/RESULTS.md))。
- ニューラル埋め込みはオプションの ggml/model インストールが必要; なければ
  ゼロ依存のサブワードハッシュが得られる (オフラインのまま、タイポも捕捉する)。

## Documentation

| ドキュメント | 内容 |
|---|---|
| [`CAPABILITIES.md`](CAPABILITIES.md) | codeloom でできることすべて |
| [`USER_GUIDE.md`](USER_GUIDE.md) | 実践的なチュートリアル |
| [`CLI.md`](CLI.md) | すべてのフラグの説明 |
| [`FEATURES.md`](FEATURES.md) | 戦略的な機能マップ |
| [`SECURITY.md`](SECURITY.md) | 信頼モデル & 検証 |
| [`docs/COMPETITION.md`](docs/COMPETITION.md) | 出典付きの競合比較マトリクス |
| [`docs/FAQ.md`](docs/FAQ.md) | 「vs LSP/RAG/repomix/code-review-graph」 — 正直なトレードオフ |
| [`docs/mcp-listing.md`](docs/mcp-listing.md) | MCP マーケットプレイス掲載用コピー |
| [`bench/RESULTS.md`](bench/RESULTS.md) | リプレイベンチ結果 (損失行を公開) |
| [`BENCHMARKS.md`](BENCHMARKS.md) | 計測済みパフォーマンス数値 |
| [`TECHNICAL_REPORT.md`](TECHNICAL_REPORT.md) | アーキテクチャ決定の記録 |
| [`AGENT_TRACE.md`](AGENT_TRACE.md) | エージェントの前後タスクトレース |

## 信頼と検証

- **CI**: Linux/macOS/Windows × Python 3.8–3.12、111 テスト、ゴールデンファイルで
  ゲートされた ≥46 グラマーフィクスチャ
- **チェックサム**: すべてのリリースは `codeloom.py` の SHA-256 を公開;
  `codeloom --verify codeloom.py` で検証
- **監査可能**: 単一の stdlib ファイル — 実行前に全部読める

## コントリビューション

PR 歓迎。テストは `python3 tests.py` で実行。理念: ゼロ依存、高速、
単一ファイル、誠実な主張。

## エージェントスキル

codeloom の利用とメンテナンスのための即利用可能なスキルが
[`skills/codeloom/SKILL.md`](skills/codeloom/SKILL.md) に同梱 — すべてのフラグ、MCP
配線、テストスイート、デモ GIF の再録画、ツールの拡張方法。
エージェントのスキルディレクトリにインストールする (例:
`~/.hermes/skills/software-development/codeloom/`)。

## License

MIT — do whatever you want with it.

---

*AI エージェントに、コンパクションのたびに 40k-LOC のリポジトリを 15 分かけて再読させるより、コードを出荷させたい人たちのために。*
