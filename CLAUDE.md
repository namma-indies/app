# Working agreement & project guide

## How Akash and Claude work together (read this first)

**Decision ownership is split, and the split is the point.**

- **Akash owns all _product_ decisions** — what the app does, its features, the flows, what's critical, what's in/out of scope, how it should feel to use. He is in the driver's seat here, always.
- **Claude owns all _technical_ decisions** — stack, schema, architecture, libraries, structure, provisioning, mechanics. Akash delegates these deliberately and trusts Claude to implement them better than he would himself.

**Surface decisions, not code.** Akash wants to see the *decisions* being made — a "decision diff" — at every altitude: brainstorming, implementation plans, and during implementation. He does **not** want code walkthroughs or mechanics explained line by line. When reporting work, lead with:

1. The decisions made (technical choices, with a one-line why).
2. **Especially the decisions Claude is uncertain about** — flag these explicitly so Akash can weigh in or redirect.
3. Anything that turned out to be a *product* question in disguise — kick those back to Akash.

Do not narrate implementation mechanics unless asked. Trust is that the code is handled; the review surface is the decisions.

**When uncertain whether something is a product or technical call:** if it changes what the app *does* or how it *feels*, it's Akash's. If it only changes how it's *built*, it's Claude's — proceed and note it.

---

## Project

See `build-foundations.md` (stack, guardrails, north star) and `docs/specs/` for design specs.
Current build: **IndieDex MVP** — `docs/specs/2026-07-19-indiedex-mvp-design.md`.
For what's **actually deployed and how to operate it** (deploy, testers, dog gate, flags) see `docs/OPERATIONS.md`.
