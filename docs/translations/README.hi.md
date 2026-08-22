<!-- हिंदी संस्करण — v0.76 के साथ निर्मित, अपडेट के बाद पुराना हो सकता है -->

<h1 align="center">codeloom</h1>

<p align="center">
  <b>अपने AI कोडिंग एजेंट को एक सेकंड में रेपो का नक्शा दें — और वह मेमोरी जो कॉम्पैक्शन में बच जाती है।</b><br/>
  एक फ़ाइल · शून्य डिपेंडेंसी · कोई डेमन नहीं · 100% लोकल · MIT
</p>

<p align="center">
  <a href="https://github.com/sloemo01/codeloom/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue"/></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.8%2B-blue"/></a>
  <a href="https://github.com/sloemo01/codeloom/actions/workflows/ci.yml"><img src="https://img.shields.io/badge/CI-passing-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom#readme"><img src="https://img.shields.io/badge/deps-zero-brightgreen"/></a>
  <a href="https://github.com/sloemo01/codeloom/stargazers"><img src="https://img.shields.io/github/stars/sloemo01/codeloom"/></a>
</p>

<p align="center">
  <a href="#quickstart">त्वरित शुरुआत</a> ·
  <a href="#what-it-gives-your-agent">फ़ीचर</a> ·
  <a href="#mcp-server-75-tools--1-router">MCP</a> ·
  <a href="#pr-review-bot">PR बॉट</a> ·
  <a href="#why-its-different">प्रतिस्पर्धियों से तुलना</a> ·
  <a href="#documentation">दस्तावेज़</a>
</p>

---

## समस्या: एजेंट सिर्फ़ टोकन नहीं जलाते — वे भूलते भी हैं

हर AI कोडिंग एजेंट की एक ही समस्या है: कुछ भी *करने* से पहले उसे समझना पड़ता है कि आपका कोडबेस *क्या* है। इसलिए वह grep करता है, पूरी फ़ाइलें पढ़ता है और कॉन्टेक्स्ट बनाने में 40,000+ टोकन जला देता है। फिर कॉन्टेक्स्ट कॉम्पैक्शन वह सब मिटा देता है — और वह सब कुछ शून्य से दोबारा निकालता है। बार-बार।

**codeloom उसके दोनों हिस्सों को ठीक करता है।**

1. **नक्शा** — एक कमांड आपके रेपो का एक कॉम्पैक्ट स्ट्रक्चरल मॉडल तैयार करती है (फ़ोल्डर ट्री + मॉड्यूल वन-लाइनर्स + एंट्री पॉइंट + इम्पोर्ट ग्राफ़ + कॉल ग्राफ़) जिसे एजेंट एक सेकंड में पढ़ लेता है।
2. **मेमोरी** — `--decide`, `--checkpoint`, `--resume` एजेंट के निर्णयों की धारा रिकॉर्ड करते हैं, ताकि `--resume` *दोनों* को बहाल करे — स्ट्रक्चरल कॉन्टेक्स्ट *और* वह सब जो एजेंट पहले आज़मा चुका, तय कर चुका और खारिज कर चुका है — किसी भी कॉम्पैक्शन के बाद।

कोई इंस्टॉल नहीं। कोई डेमन नहीं। कोई GPU नहीं। कोई टेलीमेट्री नहीं। 100% आपकी मशीन पर चलता है।

## त्वरित शुरुआत

```bash
# Option A: copy the one file (no pip, no deps)
curl -O https://raw.githubusercontent.com/sloemo01/codeloom/main/codeloom.py

# Option B: pip
pip install codeloom

# Map any repo (<1s to first result)
python3 codeloom.py /path/to/repo > AGENTS.md

# Tell your agent: "read AGENTS.md first"
```

इसे अपने एजेंट में नेटिव तरीके से जोड़ें (17 सपोर्टेड):

```bash
python3 codeloom.py --install-agent claude-code   # or cursor, codex, gemini-cli,
                                                  # opencode, cline, aider, ...
```

## आपके एजेंट को क्या मिलता है

### टास्क-शेप्ड टूल (हमारी प्रतिस्पर्धात्मक बढ़त)

| कमांड | एजेंट को क्या मिलता है |
|---|---|
| `--pack "TASK"` | **एक-शॉट ब्रीफ़**: पढ़ने का क्रम + प्रभाव + प्रासंगिक सिंबल, पहले से गणना किया हुआ |
| `--answer "Q"` | उद्धरणयुक्त उत्तर, कैलिब्रेटेड विश्वास के साथ |
| `--context-card S1 S2` | एक कॉल में N सिंबल के लिए बैच ट्राइएज कार्ड |
| `--why QUERY` | निर्णय लुकअप, `[exact]`/`[fuzzy]`/`[unverified]` स्टैम्प के साथ |
| `--plan TASK` | एजेंट-नेटिव, प्राथमिकताकृत पढ़ने की योजना |

### कॉम्पैक्शन के पार काम करने वाली मेमोरी (यह फ़ीचर कोई और नहीं देता)

```bash
codeloom --decide "use retry(3) not retry(∞) — unbounded hangs agents"
codeloom --checkpoint --task "fix login bug"     # save working state
codeloom --resume                                 # restore after compaction
```

इसके अलावा: `--remember`, `--seen`, `--working-state`, `--lessons`, `--supersede`, `--adr`, `--query-memory`।

### स्ट्रक्चरल इंटेलिजेंस

| कमांड | परिणाम |
|---|---|
| `--graph` | पूरा इम्पोर्ट ग्राफ़ (385 मॉड्यूल, <1 सेकंड में 1126 एज) |
| `--cross` | क्रॉस-फ़ाइल कॉल ग्राफ़, AST-हल किया हुआ |
| `--search` / `--usages` / `--grep` / `--read` | सिंबल इंडेक्स, कॉल साइटें, स्निपेट, टोकन-कुशल स्रोत |
| `--get-symbol X` | समरी-फर्स्ट रिट्रीवल (~95–99% टोकन बचत) |
| `--impact M` / `--refactor` / `--rename` | ब्लास्ट रेडियस की भविष्यवाणी |
| `--similar` / `--deadcode` / `--explain` | रिफ़ैक्टरिंग इंटेलिजेंस, बिना किसी LLM के |
| `--trace` | रनटाइम कॉल एज, जिन्हें स्टैटिक एनालिसिस नहीं देख सकता |
| `--routes` / `--channels` | HTTP रूट, pub-sub इवेंट चैनल |
| `--pattern '$F($$$ARGS)'` | **स्ट्रक्चरल AST खोज** मेटावेरिएबल कैप्चर के साथ |

### स्पीड और गुणवत्ता

| कमांड | परिणाम |
|---|---|
| `--health` | कोड-हेल्थ स्क्रीन: प्रति फ़ाइल 0–10, **0.2s**, डिटरमिनिस्टिक डिटेक्टर |
| `--risk HEAD~1..HEAD` | किसी भी कमिट रेंज के लिए चेंज-रिस्क स्कोर 0–100 + नामित ड्राइवर |
| `--embed-search Q` | ऑफ़लाइन सिमेंटिक खोज — subword-hash, शून्य डिपेंडेंसी (ggml वैकल्पिक) |
| `--watch` → `--watch-merge` | लाइव फ़्रेशनेस: नेटिव वॉचर सीधे पर्सिस्टेंट इंडेक्स में पाइप करता है |
| `--engine c` | स्वचालित-निर्मित C कोर: ~91s में Linux-कर्नेल का पूरा ग्राफ़ |
| `--verify FILE` | SHA-256 चेकसम सत्यापन |

**50 tree-sitter भाषाएँ डिस्पैच · 46 फ़िक्सचर-प्रोवन** (गोल्डन-फ़ाइल पैरिटी टेस्ट हर ग्रामर पर CI को गेट करते हैं) · **regex फ़ॉलबैक से 100+ एक्सटेंशन**।

## MCP सर्वर (78 टूल + 1 राउटर)

```json
{"command": "python3", "args": ["-m", "codeloom_mcp"]}
```

या 17 एजेंटों में से किसी को भी ऑटो-वायर करें: `codeloom --install-agent <name>`।

कुल 78 टूल, लेकिन एजेंट का प्रभावी सरफ़ेस **एक ही टूल** है: `codeloom_ask` प्राकृतिक भाषा लेता है और निर्धारित रूप से रूट करता है — टूल-चयन की कोई चूक नहीं। पूरी सूची: [`docs/mcp-listing.md`](docs/mcp-listing.md)।

## PR रिव्यू बॉट

`.github/workflows/pr-bot.yml` हर पुल रिक्वेस्ट को इसमें बदल देता है:

1. **इनलाइन लाइन-पिन्ड कमेंट्स** सटीक डिफ़ स्थानों पर:
   - **P1** सुरक्षा (`eval`/`exec`, हार्डकोडेड सीक्रेट), **P2** (असुरक्षित http, `shell=True`), **P3** (अनाथ नए सिंबल, TODO/FIXME मार्कर)
2. **स्टिकी समरी कमेंट** (हर पुश पर अपडेट): ड्राइवरों के साथ रिस्क फ़ैसला 0–100, डिफ़-डाइजेस्ट, प्रभावित फ़ाइलों की सेहत, अनुकूली रिव्यू चेकलिस्ट, रिव्यूअर का शुरुआती कॉन्टेक्स्ट
3. **रिस्क लेबल**: `risk:low/medium/high/critical`, अपने आप रोटेट होते हैं
4. **हैंडऑफ़**: `@codex` हमारी पास के बाद अपना LLM पास चलाता है, सीमा में केवल semantics/लॉजिक/डिज़ाइन (हमारी निर्धारित श्रेणियाँ पहले से कवर हैं)

स्टेज 1 के लिए शून्य LLM लागत। किसी भी GitHub रेपो पर चलता है — वर्कफ़्लो फ़ाइल कॉपी करें।

## यह क्यों अलग है

पूरा स्रोत-उद्धृत मैट्रिक्स: [`docs/COMPETITION.md`](docs/COMPETITION.md)। क्षेत्र के मुक़ाबले सारांश (उनके रेपो से सत्यापित, 2026-08-22 को लाइव मापा गया — आँकड़ों के लिए देखें [`benchmarks/README.md`](benchmarks/README.md)):

| | **codeloom** | code-review-graph (30.6k★) | code-context-engine | claude-context |
|---|---|---|---|---|
| इंस्टॉल | **एक stdlib फ़ाइल** | pip: **78 पैकेज** + डेमन + TOML कॉन्फ़िग | pip + ONNX + सर्वर | npm |
| बैकग्राउंड प्रोसेस | **कोई नहीं** | `crg-daemon` (16MB RSS, हेल्थ चेक) | `cce serve` + रिसोर्स गवर्नर | — |
| कॉम्पैक्शन मेमोरी | ✅ **निर्णय-लेजर, मापा गया: रिकवरी में 2 कॉल / 777 tok** (97.9% कम) | ⚠️ markdown Q&A जर्नल, कॉम्पैक्शन का ज़िक्र नहीं | ⚠️ एजेंट-कॉल `record_decision` MCP | memsearch प्लगइन |
| MCP सरफ़ेस | **78 + 1 NL राउटर** | 30, राउटर नहीं | 22 | कई |
| सिमेंटिक खोज | ✅ शून्य-डिपेंडेंसी, ऑफ़लाइन | ❌ `[embeddings]` एक्स्ट्रा (~2GB) या क्लाउड की | ❌ ONNX आवश्यक | ✅ (Zilliz) |
| भाषा का प्रमाण | **46 फ़िक्सचर-प्रोवन, CI में** | प्रकाशित नहीं | — | — |
| सेटअप→उत्तर | **0.105s वार्म** | 41s pip + 4s बिल्ड + डेमन | इंडेक्सिंग के बाद | इंडेक्सिंग के बाद |

मापे गए आँकड़े: crg के मुक़ाबले सिंबल रिट्राईवल में **24–36× कम टोकन**; कॉम्पैक्शन रिकवरी **97.9% कम टोकन**; Linux कर्नेल का पूरा ग्राफ़ ~91s। विवरण और रिप्रोडक्शन कमांड [`benchmarks/README.md`](benchmarks/README.md) में।

जहाँ प्रतिस्पर्धी आगे हैं, वहाँ साफ़ कहा गया है: jcodemunch के पास व्यापक सेफ़्टी प्री-फ़्लाइट है (edit/delete-safe, SCIP कंपाइलर सत्यापन); codegraph के पास 67k★ कम्युनिटी स्केल है; codebase-memory के पास 158 ग्रामर और arXiv-प्रकाशित eval है; repowise (AGPL) के पास दोष-सत्यापित रिस्क स्कोरिंग है। हम स्पीड + आकार + प्रति-ग्रामर प्रमाण + मेमोरी की गहराई का दावा करते हैं — उनकी खाइयों का नहीं।

## ज्ञात सीमाएँ (ईमानदारी से)

- Python को सबसे गहरा विश्लेषण मिलता है (stdlib `ast`); बाकी भाषाओं को tree-sitter आउटलाइन + regex फ़ॉलबैक।
- हेल्थ/रिस्क स्ट्रक्चरल ह्यूरिस्टिक्स हैं — किसी लेबल किए गए कॉर्पस से **दो**-सत्यापित नहीं (यही repowise की खाई है; हम अतिशयोक्ति नहीं करते, यह साफ़ कहते हैं)।
- लाइव-एजेंट टोकन-बचत बेंचमार्क डिज़ाइन किए गए हैं पर साबित नहीं — हमारे प्रकाशित आँकड़े स्टैटिक रीप्ले हैं, जिनमें लॉस रो शामिल हैं ([`bench/RESULTS.md`](bench/RESULTS.md))।
- न्यूरल एम्बेडिंग के लिए वैकल्पिक ggml/मॉडल इंस्टॉल चाहिए; इसके बिना आपको शून्य-निर्भर subword हैश मिलता है (फिर भी ऑफ़लाइन, फिर भी टाइपो पकड़ता है)।

## दस्तावेज़

| दस्तावेज़ | सामग्री |
|---|---|
| [`CAPABILITIES.md`](CAPABILITIES.md) | codeloom जो कुछ भी कर सकता है |
| [`USER_GUIDE.md`](USER_GUIDE.md) | व्यावहारिक वॉकथ्रू |
| [`CLI.md`](CLI.md) | हर फ़्लैग की व्याख्या |
| [`FEATURES.md`](FEATURES.md) | रणनीतिक फ़ीचर नक्शा |
| [`SECURITY.md`](SECURITY.md) | विश्वास मॉडल और सत्यापन |
| [`docs/COMPETITION.md`](docs/COMPETITION.md) | स्रोत-उद्धृत प्रतिस्पर्धी मैट्रिक्स |
| [`docs/FAQ.md`](docs/FAQ.md) | "vs LSP/RAG/repomix/code-review-graph" — ईमानदार ट्रेडऑफ़ |
| [`docs/mcp-listing.md`](docs/mcp-listing.md) | MCP मार्केटप्लेस लिस्टिंग कॉपी |
| [`bench/RESULTS.md`](bench/RESULTS.md) | रीप्ले-बेंच नतीजे (लॉस रो प्रकाशित) |
| [`BENCHMARKS.md`](BENCHMARKS.md) | मापा गया प्रदर्शन आँकड़े |
| [`TECHNICAL_REPORT.md`](TECHNICAL_REPORT.md) | आर्किटेक्चर निर्णयों का लेखा-जोखा |
| [`AGENT_TRACE.md`](AGENT_TRACE.md) | एजेंट का काम से पहले/बाद का ट्रेस |

## विश्वास और सत्यापन

- **CI**: Linux/macOS/Windows × Python 3.8–3.12, 75 टेस्ट, गोल्डन फ़ाइलों से गेट किए गए ≥45 ग्रामर फ़िक्सचर
- **चेकसम**: हर रिलीज़ `codeloom.py` की SHA-256 प्रकाशित करती है; सत्यापित करें: `codeloom --verify codeloom.py`
- **ऑडिट योग्य**: एक stdlib फ़ाइल — चलाने से पहले पूरी पढ़ लें

## योगदान

PR स्वागतयोग्य हैं। टेस्ट चलाएँ: `python3 tests.py`। सिद्धांत: zero-dependency, तेज़, एक फ़ाइल, ईमानदार दावे।

## एजेंट स्किल

codeloom के उपयोग और रखरखाव के लिए एक रेडी-टू-लोड स्किल [`skills/codeloom/SKILL.md`](skills/codeloom/SKILL.md) पर उपलब्ध है — हर फ़्लैग, MCP वायरिंग, टेस्ट सूट, डेमो GIF को दोबारा रिकॉर्ड करना, और टूल को कैसे बढ़ाना है। इसे अपने एजेंट की स्किल डायरेक्टरी में इंस्टॉल करें (जैसे `~/.hermes/skills/software-development/codeloom/`)।

## लाइसेंस

MIT — do whatever you want with it.

---

*उन लोगों के लिए बनाया गया है जो चाहते हैं कि उनका AI एजेंट हर कॉम्पैक्शन के बाद 15 मिनट 40k-LOC रेपो दोबारा पढ़ने के बजाय कोड शिप करे।*
