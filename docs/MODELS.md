# Choosing a model

ClaudePhone speaks one wire format — OpenAI-compatible `/chat/completions` — so
OpenRouter, `llama.cpp`, `ollama` and `vllm` are all reachable with nothing but
a base URL and a key.

---

## What the agent actually demands of a model

Driving a phone is not a hard reasoning task, but it is an unforgiving one:

1. **Tool calling that works**, ideally native. This is the real filter.
2. **Instruction adherence over many turns.** A 30-step run means 30 chances to
   forget that it must `ui_dump` before it taps.
3. **Enough context for repeated screen dumps.** ~1.5 KB per dump, times the
   `KEEP_FULL_RESULTS` window, plus ~1,600 tokens of tool schema per turn.
4. **Not much else.** No long-form writing, no deep reasoning, no code
   generation. This is why cheap models are a good fit.

---

## OpenRouter (default)

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export CLAUDEPHONE_MODEL=deepseek/deepseek-v4-flash-0731
```

The default is **`deepseek/deepseek-v4-flash-0731`** — $0.05/M input, $0.16/M
output, native tool calling, a sparse MoE with 13B active parameters. At those
rates a 30-step run costing ~50k input tokens is well under a cent.

Alternatives worth knowing:

| Model | Input / Output per M | Note |
|---|---|---|
| `deepseek/deepseek-v4-flash-0731` | $0.05 / $0.16 | current default |
| `deepseek/deepseek-v4-flash` | $0.068 / $0.168 | 1M context |
| `minimax/minimax-m3:free` | free | rate-limited, good for smoke tests |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | free | rate-limited |
| `anthropic/claude-haiku-4.5` | $1 / $5 | when a cheap model keeps failing |

> **Verify before committing.** Model availability and pricing on OpenRouter
> change faster than this file. Check the live
> [tool-calling collection](https://openrouter.ai/collections/tool-calling-models)
> and [free models](https://openrouter.ai/collections/free-models). Free tiers
> are typically capped around 20 requests/minute — an agent loop will hit that.

`openrouter/free` is a router that picks among free models while filtering for
requested capabilities such as tool calling; convenient, but the model you get
varies between runs, which makes debugging harder.

---

## Local, on the phone

```bash
export CLAUDEPHONE_PROVIDER=local
export CLAUDEPHONE_LOCAL_URL=http://127.0.0.1:8080/v1
export CLAUDEPHONE_TOOL_MODE=json
```

Any OpenAI-compatible server on the phone works. With `llama.cpp`:

```bash
pkg install -y clang cmake git ninja
git clone https://github.com/ggerganov/llama.cpp && cd llama.cpp
cmake -B build -DGGML_NATIVE=ON && cmake --build build -j4
./build/bin/llama-server -m <model>.gguf --host 127.0.0.1 --port 8080 -c 8192
```

**Memory is the constraint.** Measured on this device: 7.36 GiB usable, with a
real ceiling of ~4–4.5 GB before Android's low-memory killer starts culling
Termux.

| Model | Q4 size | Verdict |
|---|---|---|
| LFM2-700M | ~450 MB | trivially safe |
| LFM2-1.2B | ~800 MB | trivially safe |
| LFM2-2.6B | ~1.6 GB | comfortable |
| any 7B | ~4.5 GB | LMK kills Termux |

**CPU only.** The Mali-G68 shares the same RAM pool, so `-ngl` cannot relieve
memory pressure the way discrete VRAM does, and llama.cpp's Vulkan/OpenCL
backends on Mali are slower and flakier than CPU. Use the 2× Cortex-A78 cores.

### Set expectations honestly

A sub-3B model has **no tool-call training**, which is why `CLAUDEPHONE_TOOL_MODE=json`
exists — it prompts the model to emit a fenced object instead:

```json
{"tool": "ui_dump", "args": {"limit": 40}}
```

Even so, expect a small local model to manage a handful of steps on a familiar
screen, not a 30-step run through an unfamiliar app. Realistic local uses:

- classification and triage ("is this screen a login wall?")
- reading a dump and extracting one field
- a fallback when the network is down

Nothing on this device has been benchmarked for tokens/sec yet — see
[BENCHMARKS.md §5](BENCHMARKS.md#5-what-has-not-been-measured-yet).

---

## Tool-calling conventions

| `CLAUDEPHONE_TOOL_MODE` | Behaviour |
|---|---|
| `auto` (default) | try native; permanently fall back to json on a 400 |
| `native` | OpenAI `tools` / `tool_calls` only |
| `json` | fenced JSON protocol only — force this for small local models |

When the fallback fires, the system prompt is **rebuilt** to teach the json
protocol and a `note` event is emitted. In json mode the loop enforces one call
per turn, because small models lose track when asked to batch.

---

## Controlling cost

- **Packs.** Only `core` loads by default: ~1,600 tokens of schema per turn
  instead of ~12,200. This is the single biggest lever —
  [measured](BENCHMARKS.md#2-tool-schema-overhead).
- **Compaction.** Tool results older than 6 steps are clipped automatically.
- **Budgets.** `--max-steps` (default 30) and `Budget.max_tokens` (250k) both
  hard-stop a run and report why.
- **Screens, not screenshots.** `ui_dump` returns 1–3 KB of typed elements;
  `screenshot` returns a *path*, never image data, so it cannot silently
  multiply your bill.

Every run's `final` event carries the usage totals; the CLI prints them:

```
— 3 steps · 6.1s · 4,120 tokens
```

Setting `HTTP-Referer` and `X-Title` means runs show up identifiably in your
OpenRouter dashboard, which is worth having when watching spend.

---

## Picking, in practice

| Situation | Use |
|---|---|
| Normal use | `deepseek/deepseek-v4-flash-0731` |
| Smoke-testing the harness | a `:free` model |
| A cheap model keeps mis-picking tools | `anthropic/claude-haiku-4.5` |
| No network / privacy-sensitive | local + `CLAUDEPHONE_TOOL_MODE=json` |
| Debugging one tool | skip the model: `claudephone tool <name> --args '{}'` |
