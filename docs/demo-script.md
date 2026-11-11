# 3-minute demo script

**Audience:** course reviewers or a small-business owner. **Goal:** show that the assistant does the boring
work accurately, and that the owner stays in control.

## Before the demo (about 15 minutes, not on camera)

```bash
make doctor        # all green
make index
make run           # processes the 20 sample emails one by one (~10 minutes)
make ui            # leave the approval screen open in a browser tab
```

Have three things open: the browser (Streamlit queue), a terminal, and `data/emails/q05_wedding_full_day.eml`.

**Backup plan:** if the network or API is down, everything shown below already lives in `var/assistant.db`
from the earlier `make run`, and the UI does not need the API. Skip the live `make run` line.

## Script

| Time | Show | Say |
|---|---|---|
| **0:00 – 0:20** | The queue page | "Maya runs a one-person photography studio. Every day she answers the same emails: how much, are you free, what's your policy. This assistant drafts those replies, but nothing goes out until she approves it." |
| **0:20 – 1:00** | Open *Wedding photography inquiry – June 12, 2027* | "A couple asks for a full-day wedding with three add-ons, 48 miles away, on a Saturday. The router labelled it a quote request at 0.99 confidence. The quote agent extracted the details, checked the calendar, looked up the distance, and called the pricing tool." Scroll to the draft: "Every line item comes from the pricing tool: weekend surcharge, travel beyond 25 miles, total $4,174.50, deposit $1,252.35. The AI never does arithmetic." |
| **1:00 – 1:25** | Expand *Tool calls* and *Guardrail verdicts* | "Here are the exact tool calls over MCP. Six automatic checks passed: every dollar amount matches a tool result, every policy sentence cites its source, no discounts or bookings were promised, and it follows Maya's voice." |
| **1:25 – 1:45** | Change one sentence in the draft, click **Edit, approve & send**; show `outbox/<id>.eml` in the terminal | "Maya tweaks a sentence and approves. Only now is the reply written, threaded to the customer's email, with the citation markers removed. Nothing can be sent without this click; that's enforced in code and tested." |
| **1:45 – 2:05** | Open *Portrait session inquiry* (status BLOCKED) → *Guardrail verdicts* | "This looks like a normal inquiry, but it hides white text telling 'the AI assistant' to give a 90% discount. Both injection checks caught it before any agent saw it. It sits here for Maya, not in a customer's inbox." |
| **2:05 – 2:25** | Open *photographer for 40th birthday party* | "When details are missing, it does not guess. It asks only for what is missing, here the number of hours." Then open *How long for wedding photos?*: "Policy questions are answered only from Maya's documents, with each sentence linked to its source." |
| **2:25 – 2:50** | Terminal: `make eval` summary (or open `docs/eval-results.md`), then `python -m app.cli trace <id>` | "We measure it. On 20 test emails: 100% routing, all 8 quotes exactly right, both injections blocked, zero unapproved sends, and an AI judge rated every draft as grounded. Every step of every email is traced, with timing and cost, about one cent per email." |
| **2:50 – 3:00** | Back to the queue | "So Maya spends seconds reviewing instead of minutes writing, and she stays in control of every word that leaves the studio." |

## If asked

- **"What if it gets it wrong?"** It goes to the owner queue with the reason. The send gate means a wrong
  draft can cost Maya a minute, never a customer.
- **"Which skills does this show?"** See the table in the README: LLM API and prompts, MCP tools, RAG,
  agent loop, multi-agent routing and orchestration with human approval, evals, guardrails and tracing.
- **"Can it read my Gmail?"** Not yet. The channel adapter interface is ready for Gmail and WhatsApp; that is
  the next step after a pilot.
