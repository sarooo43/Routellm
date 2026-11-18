# How to review the routing-label dataset (20-prompt sample)

`evals/routing_labeled.jsonl` contains 100 prompts labelled with
`expected_task_type` ∈ {code, reasoning, summarization, qa, general}.
**The labels were authored by AI and have NOT been reviewed by the owner**
(`reviewed_by_owner: false` in the dataset header line). Until you review,
the routing-accuracy number is an AI-authored self-check, not a human-audited
measurement.

## Review a sample of 20 labels (~15 minutes)

1. Open `evals/routing_labeled.jsonl`.
2. Review **every 5th prompt**: ids 5, 10, 15, … 100 (20 prompts total).
   This stride covers all five task types and all difficulties without
   cherry-picking.
3. For each prompt, decide **without looking at the label first**: "what
   would a person call this task?" The definitions the classifier uses:
   - `code` — writing/debugging/explaining program code;
   - `reasoning` — multi-step logic, math word problems, cause-effect analysis;
   - `summarization` — condense existing text;
   - `qa` — a direct factual question (often ends with "?");
   - `general` — anything else (chat, commands, creative writing).
4. Tally disagreements. Expected noise for a good dataset is ≤ 2 of 20
   (≤ 10%).
5. If you disagree with **more than 2 labels**, re-label those prompts (and
   skim the rest of that task type) and re-run the eval:
   `python evals/run_routing_eval.py`.

## Mark the dataset reviewed

When you are satisfied with the sample, edit **line 1** of
`evals/routing_labeled.jsonl` and set the review flag:

```json
{"_dataset_header": {"authored_by": "AI", "reviewed_by_owner": true, ...}}
```

The eval's report then states `reviewed_by_owner=true`. There is exactly one
flag, on line 1 — do not add per-row flags.

## What the eval reports back

`python evals/run_routing_eval.py` prints:

- `authored_by` / `reviewed_by_owner` verbatim from the header line;
- a **keyword-leakage indicator**: the fraction of prompts containing a
  keyword from the rule-based classifier's own keyword lists. Expect this to
  be high — the dataset was written against the classifier's vocabulary —
  and treat the accuracy number accordingly (in-distribution self-check,
  likely inflated relative to real traffic).
