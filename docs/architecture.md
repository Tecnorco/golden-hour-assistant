# Architecture

Golden Hour Assistant is split into five layers. A layer only talks to the one below it through a typed
interface, so a new channel (Gmail, WhatsApp) or a new tool can be added without touching the agents.

## Layers

```mermaid
flowchart TB
    subgraph L1["1 · Channels"]
        EML["EmlAdapter<br/>parse() / send_reply()"]
        FUT["GmailAdapter · WhatsAppAdapter<br/>(designed for, not built)"]:::future
    end
    subgraph L2["2 · Orchestration & guardrails"]
        IG["Input guardrails<br/>length · injection heuristics + LLM classifier · spam"]
        RT["Router<br/>label + confidence"]
        QA["Quote agent<br/>tool loop"]
        FA["FAQ agent<br/>RAG only"]
        OA["Reschedule / other agent<br/>always flags owner"]
        OG["Output guardrails<br/>6 deterministic checks + 1 repair"]
        SM["State machine<br/>(orchestrator.py)"]
    end
    subgraph L3["3 · Capabilities"]
        LLM["LLM wrapper<br/>Gemini · retries · structured output · cost"]
        RAG["RAG from scratch<br/>chunker · cached embedder · numpy store · retriever"]
        MCP["MCP server (stdio)<br/>lookup_distance · check_availability<br/>calculate_quote · list_packages · save_draft"]
        PR["Pricing engine<br/>deterministic, Decimal"]
    end
    subgraph L4["4 · Data & logs"]
        DOCS["data/docs · pricing.yaml · calendar.json"]
        IDX["var/index (vectors.npy + chunks.json)"]
        DB["SQLite: inquiries · drafts · decisions · transitions · spans"]
    end
    subgraph L5["5 · Review & quality"]
        UI["Streamlit approval screen<br/>(no API key)"]
        EV["Eval harness + LLM judge"]
        OBS["trace · report · doctor"]
    end

    EML --> IG --> RT
    RT --> QA & FA & OA
    QA & FA & OA --> OG --> SM
    QA & FA & OA --> LLM
    QA & FA & OA --> RAG
    QA & FA & OA -- "MCP client" --> MCP --> PR
    MCP --> DOCS
    RAG --> IDX
    SM --> DB
    MCP -- save_draft --> DB
    UI --> DB
    UI -- "send gate" --> EML
    EV --> SM
    OBS --> DB
    classDef future stroke-dasharray: 5 5,color:#888
```

## One inquiry, end to end

```mermaid
sequenceDiagram
    autonumber
    participant E as Email (.eml)
    participant O as Orchestrator
    participant G as Guardrails
    participant R as Router
    participant A as Quote agent
    participant M as MCP server
    participant S as SQLite
    participant U as Owner (Streamlit)

    E->>O: EmlAdapter.parse() -> Inquiry
    O->>S: RECEIVED
    O->>G: input checks (length, injection x2, spam)
    alt blocked
        G-->>O: BLOCKED (never reaches an agent)
    end
    O->>R: classify (structured output)
    alt confidence < 0.7
        R-->>O: OWNER_QUEUE
    end
    O->>A: extract fields, then tool loop
    A->>M: lookup_distance, check_availability
    A->>M: calculate_quote (only source of prices)
    A->>A: search_policies (RAG, cite chunk ids)
    A->>M: save_draft (code fills inquiry_id, tool_results)
    A->>A: verify quote args == extracted fields + looked-up distance
    O->>G: output checks (amounts, citations, promises, tone, leakage)
    alt fail
        G->>G: one repair attempt, re-check
        G-->>O: OWNER_QUEUE if still failing
    end
    O->>S: PENDING_APPROVAL
    U->>S: approve / edit & approve / reject (logged with timestamp)
    U->>O: send_reply()
    O->>O: raise unless APPROVED / EDITED_APPROVED + approval on record
    O->>E: reply (citation markers stripped) -> outbox/
```

## State machine

Every status change goes through `orchestrator.transition()`, which rejects anything not in this table.

```mermaid
stateDiagram-v2
    [*] --> RECEIVED
    RECEIVED --> BLOCKED: injection / spam
    RECEIVED --> ROUTED
    RECEIVED --> OWNER_QUEUE: error / low confidence
    ROUTED --> DRAFTED
    ROUTED --> OWNER_QUEUE: agent could not draft
    ROUTED --> BLOCKED: router says spam
    DRAFTED --> PENDING_APPROVAL: guardrails pass
    DRAFTED --> OWNER_QUEUE: guardrails fail after repair
    BLOCKED --> OWNER_QUEUE: owner reviews a false positive
    PENDING_APPROVAL --> APPROVED
    PENDING_APPROVAL --> EDITED_APPROVED
    PENDING_APPROVAL --> REJECTED
    OWNER_QUEUE --> APPROVED
    OWNER_QUEUE --> EDITED_APPROVED
    OWNER_QUEUE --> REJECTED
    APPROVED --> SENT
    EDITED_APPROVED --> SENT
    REJECTED --> [*]
    SENT --> [*]
```

## Trust boundaries

| Boundary | Rule | Where |
|---|---|---|
| Email text → any prompt | Always wrapped in `<untrusted_email>`; a closing tag inside the email is neutralised | `agents/common.py` |
| Email → agents | Input guardrails run first; blocked mail never reaches an agent | `guardrails/input.py` |
| Model → facts | Prices, distances, availability, packages come only from MCP tools; policies only from retrieval | `prompts/*`, `agents/quote_agent.py` |
| Model → stored draft | `inquiry_id`, `tool_results`, `needs_info` are code-owned and hidden from the model | `agents/loop.py` |
| Model → quote | `calculate_quote` args are cross-checked against extracted fields and the looked-up distance | `verify_quote_calls` |
| Draft → owner | Six deterministic output checks, one repair, then the owner queue | `guardrails/output.py` |
| Owner → customer | `send_reply` raises unless approved and an approval decision is on record | `orchestrator.py` |
| API key | Only the pipeline process holds it. The MCP server gets an env allowlist; the UI drops the key | `mcp_server/client.py`, `ui/streamlit_app.py` |

## Adding a channel (Gmail, WhatsApp)

1. Implement `Adapter.parse()` to build an `Inquiry` (WhatsApp: set `reply_window_expires_at` = last
   customer message + 24 h and use the phone number as `thread_id` for conversation memory).
2. Implement `Adapter.send_reply()` (Gmail: reply in-thread with `In-Reply-To`; WhatsApp: refuse after the
   window closes and keep messages short).
3. Nothing else changes: agents, guardrails, the state machine and the send gate are channel-agnostic.
