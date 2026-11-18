# Eval report `ci_broken` - 2026-11-17 12:39

**Gate: FAILED** - faithfulness = None < threshold 0.85; refusal_accuracy = 0.282 < threshold 0.85; citation_coverage = None < threshold 0.85

- Questions: 39 (28 answerable, 11 must-refuse), tickers AAPL, TSLA, NVDA
- Prompt `answer_broken_demo` · model `anthropic:claude-haiku-5-5` · retrieval `hybrid_rerank` · guardrails True
- Judge `claude-opus-5-5` (effort low): 28 calls, 186,488 in / 4,624 out tokens, 0 errors

| Metric | Value | Threshold |
|---|---|---|
| hit_rate@5 | 93% ✅ | 80% |
| mrr@5 | 0.70 |  |
| context_recall | 96% |  |
| faithfulness | - ❌ | 85% |
| answer_correctness | 0% |  |
| citation_coverage | - ❌ | 85% |
| refusal_accuracy | 28% ❌ | 85% |
| false_refusal_rate | 100% |  |
| p50 / p95 latency | 2477 / 6171 ms | |

## Per question

| id | question | hit rank | refused | faithful | correct | cited |
|---|---|---|---|---|---|---|
| q001 | How many people did Apple employ at the end of fiscal 2026? | 1 | yes ⚠️ | - | 0% | 0% |
| q002 | What were Apple's total net sales in fiscal 2026? | 1 | yes ⚠️ | - | 0% | 0% |
| q003 | How much is Apple's quarterly dividend per share? | 1 | yes ⚠️ | - | 0% | 0% |
| q004 | Where is Apple's head office? | 1 | yes ⚠️ | - | 0% | 0% |
| q005 | What share of Apple's revenue is sold through resellers and carriers r | 5 | yes ⚠️ | - | 0% | 0% |
| q006 | Why is Tesla's reliance on its CEO considered a risk? | 2 | yes ⚠️ | - | 0% | 0% |
| q007 | When did Tesla start its autonomous ride-hailing service? | 3 | yes ⚠️ | - | 0% | 0% |
| q008 | How much battery storage capacity did Tesla deploy in 2025? | 1 | yes ⚠️ | - | 0% | 0% |
| q009 | What is Tesla's Cybercab? | 2 | yes ⚠️ | - | 0% | 0% |
| q010 | Which chip architecture generated most of NVIDIA's data center sales? | 1 | yes ⚠️ | - | 0% | 0% |
| q011 | How large is NVIDIA's workforce and how much of it is in R&D? | - | yes ⚠️ | - | 0% | 0% |
| q012 | How is the Chinese government affecting NVIDIA's data center business? | 1 | yes ⚠️ | - | 0% | 0% |
| q028 | How much revenue did the iPhone bring Apple in fiscal 2026? | 2 | yes ⚠️ | - | 0% | 0% |
| q029 | How much did Apple spend on research and development in fiscal 2026? | 2 | yes ⚠️ | - | 0% | 0% |
| q030 | How much of its own stock did Apple buy back in fiscal 2026? | 2 | yes ⚠️ | - | 0% | 0% |
| q031 | What were Apple's Services net sales in fiscal 2026? | - | yes ⚠️ | - | 0% | 0% |
| q032 | Why did Apple's sales in Greater China rise in 2026? | 1 | yes ⚠️ | - | 0% | 0% |
| q033 | What was Apple's gross margin percentage on products in 2026? | 1 | yes ⚠️ | - | 0% | 0% |
| q034 | What were Tesla's total revenues in 2025 and how did they change from  | 3 | yes ⚠️ | - | 0% | 0% |
| q035 | How much revenue did Tesla's energy business generate in 2025? | 3 | yes ⚠️ | - | 0% | 0% |
| q036 | What is Tesla's Cortex? | 1 | yes ⚠️ | - | 0% | 0% |
| q037 | Which company is Tesla working with to make AI chips in the United Sta | 3 | yes ⚠️ | - | 0% | 0% |
| q038 | What share of Tesla's employees receive stock as part of their pay? | 1 | yes ⚠️ | - | 0% | 0% |
| q039 | What was NVIDIA's total revenue for fiscal year 2026? | 1 | yes ⚠️ | - | 0% | 0% |
| q040 | How large was NVIDIA's charge related to the H20 chip and what caused  | 1 | yes ⚠️ | - | 0% | 0% |
| q041 | How much did NVIDIA spend repurchasing its shares in fiscal 2026? | 2 | yes ⚠️ | - | 0% | 0% |
| q042 | How much H20 revenue did NVIDIA earn under U.S. export licenses? | 1 | yes ⚠️ | - | 0% | 0% |
| q043 | When did NVIDIA buy Mellanox and what did it add? | 1 | yes ⚠️ | - | 0% | 0% |
| q053 | What will Apple's revenue be in 2030? | - | yes (should) | - | - | 0% |
| q054 | What were Ford Motor Company's main risk factors? | - | yes (should) | - | - | 0% |
| q055 | What is the capital of France? | - | yes (should) | - | - | - |
| q056 | What will NVIDIA's stock price be next year? | - | yes (should) | - | - | 0% |
| q057 | How many employees does Berkshire Hathaway have? | - | yes (should) | - | - | - |
| q058 | What was Tesla's total revenue in 2015? | - | yes (should) | - | - | 0% |
| q059 | What is Rivian's vehicle delivery guidance for 2026? | - | yes (should) | - | - | 0% |
| q060 | Who will be Apple's CEO in 2030? | - | yes (should) | - | - | 0% |
| q061 | What was Microsoft's revenue in fiscal year 1995? | - | yes (should) | - | - | - |
| q062 | Should I buy Tesla stock right now? | - | yes (should) | - | - | 0% |
| q063 | What did Meta report for its third quarter of 2027? | - | yes (should) | - | - | 0% |
