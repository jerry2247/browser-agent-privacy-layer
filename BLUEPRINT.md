# PLVA Blueprint — Holo CUA Privacy Environment

This file specifies the **CUA-management component** of the PLVA project. It is written to be
built by an AI coding agent (Claude Fable 5) in small, independently testable steps.

---

## 0. How to build from this document (read first)

- Build **one step at a time, in order**. After each step, stop and run the step's **Verify**
  check. Do **not** start the next step until Verify passes.
- Prefer the smallest change that satisfies the step. Each step must be independently runnable.
- Treat everything under **Hard constraints** as non-negotiable. If a step seems to require
  breaking one, stop and surface it instead of proceeding.
- **Secrets:** never commit API keys, screenshots, transcripts, or vault contents. Keys live in
  environment variables and an untracked `.env` only. Keep a safe `.env.example`.
- **Stubs:** when a step depends on another component that is out of scope here (the PII
  detector), build against the **stub interface** defined in §5 so this component is testable
  before the real one exists.
- Any fact marked **VERIFY** is an assumption to confirm empirically. If it turns out false,
  stop and report — do not build on it.

---

## 1. Goal and scope

**PLVA** is a privacy layer for computer-use agents (CUAs). Full pipeline:

> screenshot → detect PII + bounding boxes → paint placeholders over the PII values
> (e.g. `PASSWORD_1`) → store the real values in a key-value **vault** keyed by placeholder →
> tell the model how to reason about placeholders → when the model returns actions, resolve each
> placeholder back to its real value **before** the action executes.

**This component covers:** running the CUA, sandboxing it, giving it skills, and **intercepting
and modifying both directions of the model traffic** (the request to the inference provider and
the returned actions), plus the **vault**.

**Out of scope (separate component):** the actual detection/redaction of PII pixels. Here it is a
**pluggable dependency** with a stub for testing (see §5).

---

## 2. Glossary

- **CUA** — computer-use agent: takes a screenshot + task, emits GUI actions (click/type/scroll).
- **Holo3** — H Company's vision computer-use model. This project uses Overshoot's
  **`Hcompany/Holo3-35B-A3B`** model only.
  Consumes images + text; has **no audio input**.
- **HoloDesktop CLI** — the harness (`github.com/hcompai/holo-desktop-cli`, Apache-2.0, Python/uv).
  Its open code is the CLI/integration wrapper; it downloads a **closed `hai-agent-runtime`
  binary** that owns the perceive→act loop (screen capture, model call, and action execution).
- **Overshoot** — the inference provider. OpenAI-compatible HTTP API.
- **NemoClaw / OpenShell** — NVIDIA's agent stack. **OpenShell** is its sandbox (restricted
  network + file isolation). In this project, "integrate NemoClaw" = **use OpenShell for
  isolation and controlled network egress** (see §7).
- **PLVA proxy** — a local OpenAI-compatible HTTP proxy we build; the single point where model
  traffic is intercepted and rewritten.
- **Vault** — private, in-memory, session-scoped map from placeholder → real value.
- **Placeholder** — a stable semantic token painted over a PII value, e.g. `EMAIL_1`, `PHONE_2`.

---

## 3. Technical choices (fixed)

| Choice | Value | Notes |
|---|---|---|
| Model | **`Hcompany/Holo3-35B-A3B`** | Exact Overshoot model id selected by the user on 2026-07-11. Do **not** use Nemotron, OpenClaw, or another model/harness. |
| Harness | **HoloDesktop CLI** | Closed `hai-agent-runtime` binary owns the loop — see §4 for why this forces a proxy. |
| Inference provider | **Overshoot** (OpenAI-compatible) | Start on the easiest working endpoint, then move to Overshoot (Steps 1→2). |
| NemoClaw integration | **OpenShell sandbox** | Used for network-egress control / isolation, **not** as an orchestrator brain. |

---

## 4. Architecture and why a proxy is required

```
 Host machine
 ┌──────────────────────────────────────────────────────────────┐
 │  HoloDesktop CLI (open wrapper)                                │
 │      │ launches                                                │
 │      ▼                                                         │
 │  hai-agent-runtime (CLOSED binary)                             │
 │    • captures the real screen                                  │
 │    • BUILDS the model request (embeds the raw screenshot)      │
 │    • executes the returned actions on the real desktop         │
 │      │ sends OpenAI-format request to its configured base URL  │
 └──────┼─────────────────────────────────────────────────────────┘
        ▼   (base URL points at the proxy, not at Overshoot)
 ┌──────────────────────────────────────────────────────────────┐
 │  PLVA PROXY (local, OpenAI-compatible)   ── holds the VAULT ── │
 │   REQUEST leg  (runtime → model):                              │
 │     1. redact the screenshot (call scrubber stub) + fill vault │
 │     2. scrub leaked values out of the message/text history     │
 │     3. inject any extra context (e.g. redacted audio, §9)      │
 │   RESPONSE leg (model → runtime):                              │
 │     4. parse the proposed action; resolve placeholders only in │
 │        the type-text field; forward the rewritten action       │
 └──────┼─────────────────────────────────────────────────────────┘
        ▼
    Overshoot  (`Hcompany/Holo3-35B-A3B`)
```

**Why a proxy and not "just change the request":** the closed `hai-agent-runtime` binary is what
builds and sends the request. We cannot edit that code, so there is no point where the request
exists as our own object to modify. The **only** knob the binary exposes is **where** it sends
(its base URL). Pointing that at a local proxy is therefore the only way to see and rewrite the
traffic. OpenAI-compatibility makes the proxy easy to write (we know the schema); it does not
remove the need for it. The same reasoning applies to the response: actions flow back into the
closed binary to be executed, so placeholder resolution must also happen in the proxy, in transit.

---

## 5. The PII scrubber interface (stub for this component)

The real detector is a separate component. Build this component against a **stub** with a stable
contract, so the pipeline is testable now and the real detector drops in later.

**`redact(frame)` → `{ redactedFrame, spans }`**
- `frame`: raw RGBA image (the screenshot pulled from the request).
- `redactedFrame`: a **new** image with placeholder chips painted over PII values; pixels outside
  the covered regions are byte-identical to the input.
- `spans`: list of `{ class, value, boundingBox, placeholder }` for each detected PII value.

**Stub behavior for testing:** deterministic and configurable — e.g. detect a hardcoded string or
a fixed screen region, paint a chip, and return one span. It must let tests exercise the full
store→resolve loop without a real detector. Provide a "detect nothing" mode and a "detect fixture
X" mode.

The proxy depends only on this interface, never on the detector's internals.

---

## 6. The vault (store/resolve loop)

Private, in-memory, session-scoped. Two operations:

- **Store (request leg):** for each detected span, canonicalize the value (normalize whitespace,
  Unicode NFKC; keep credentials exact/case-sensitive), then look it up. Same value → **same**
  placeholder (stability). New value → next per-class counter → new placeholder. Store
  `placeholder → value` only for **reversible** classes.
- **Resolve (response leg):** given a placeholder, return its value **only if** it was issued this
  session, is a reversible class, and passes host policy.

Rules:
- **Secret-tier classes** (`PASSWORD`, `CVC`, `CARD_NUMBER`, `PRIVATE_KEY`) are **masked, never
  stored, never resolvable.**
- **Credential classes** (`API_KEY`, `AUTH_TOKEN`) may get a placeholder but resolution is
  **denied** unless the host explicitly allows that class/destination.
- Different values must **never** alias to the same placeholder; the same value must always map to
  the same placeholder within a session.
- **No persistence** anywhere (disk, IndexedDB, localStorage, logs, telemetry). Cleared on
  `dispose()`.

---

## 7. NemoClaw / OpenShell integration

We are **not** using OpenClaw as an orchestrator, so integration means **running under OpenShell
for isolation and controlled network egress** — this is what makes "only the redacted frame
leaves" an *infrastructure* guarantee, not just a code invariant.

> **Open decision — resolve in Step 1 (pick one, document choice in `docs/decisions/`):**
> - **(a) Egress-isolation (recommended to start):** the runtime drives the *host* desktop; the
>   PLVA proxy and all outbound model/web traffic are forced through OpenShell's restricted
>   network so the proxy is the only egress. Sandbox = the network boundary.
> - **(b) Contained-desktop (stronger, more setup):** the CUA operates a desktop *inside* a
>   sandbox; screenshots and actions stay inside it; egress is controlled at the sandbox edge.
>   Stronger privacy but may fight HoloDesktop's "drives your real computer" design.

> **Note:** HoloDesktop's built-in NemoClaw wiring (`holo install` → the MCP bridge) assumes an
> **OpenClaw** sandbox with Holo exposed as a tool. We are **not** doing that. Treat wiring
> HoloDesktop's egress under OpenShell without OpenClaw as a **spike** in Step 1, and record what
> actually works.

---

## 8. Hard constraints (do not violate)

1. **Fail closed.** If any stage fails, no request/response is forwarded and no raw frame ever
   reaches the provider. There is no raw-frame fallback.
2. **Redacted-only egress.** The provider must never receive an un-redacted screenshot or a vault
   value through the proxy.
3. **New output buffer.** Redaction renders into a new image; input pixels are never mutated.
4. **Resolution is narrow.** Placeholders are resolved **only** in the type-action text field —
   never in prose, URLs, click targets, logs, or arbitrary JSON.
5. **Privacy-safe logs.** Logs may contain class, placeholder, bounding box, backend, timings —
   **never** recognized text, real values, transcripts, or frames.
6. **No persistence of session state.** Vault and any cache are memory-only and dropped on dispose.
7. **Streaming-safe.** The proxy must handle **SSE / streamed** responses: buffer or incrementally
   parse the tool call enough to resolve the action before forwarding. Never forward an
   unresolved placeholder to the executor.
8. **Model id is not hardcoded blindly.** Confirm the exact provider model-id string by querying
   the provider's `/models` first (see Step 0).

---

## 9. Build steps

Each step lists **Goal → Build → Verify**. Do not proceed past a failing Verify. After every step, pause to allow me to verify the outputs. Describe how to test the current step once done with a step.

### Step 0 — Setup and external-contract probe
- **Goal:** clean repo, secrets handling, and confirmed external contracts before building.
- **Build:** initialize the repo and `.env.example`; install HoloDesktop CLI; obtain keys for
  Overshoot (and the fallback provider). Write a tiny throwaway probe (not committed with keys)
  that: (a) GETs the provider `/models` list and confirms that
  **`Hcompany/Holo3-35B-A3B`** is ready; (b)
  sends one hardcoded screenshot as an OpenAI-format `chat/completions` request and records the
  **exact response shape**, including whether actions come back as **tool calls** or text, and
  whether responses are **streamed**.
- **Verify:** you have written down (i) the exact model-id string, (ii) the action/response
  schema, (iii) streaming yes/no. `.env` is git-ignored; `.env.example` is committed.
- **VERIFY (critical):** confirm the runtime sends the **screenshot** through the configurable
  base URL (point HoloDesktop at a logging stub and check the image appears in the request body).
  If it does not, the interception proxy cannot see the frame — stop and report before continuing.

### Step 1 — Functioning HoloDesktop agent under NemoClaw/OpenShell
- **Goal:** HoloDesktop completes a simple terminal task, running under OpenShell isolation.
- **Build:** get HoloDesktop running on the easiest working inference endpoint (Overshoot if not
  much harder). Resolve the §7 **Open decision** and stand up the chosen OpenShell topology.
  Confirm a basic task (e.g. "open a terminal and run a command") completes.
- **Verify:** the agent finishes the task end-to-end; you can name exactly what runs inside
  OpenShell and what its allowed egress is; the decision is recorded in `docs/decisions/`.

### Step 2 — Move inference to Overshoot and measure
- **Goal:** all inference goes to Overshoot on `Hcompany/Holo3-35B-A3B`; latency is known.
- **Build:** set HoloDesktop's base URL / key to Overshoot using the id confirmed in Step 0.
- **Verify:** the same task from Step 1 completes via Overshoot; you have recorded per-step and
  end-to-end latency (p50/p95) for a fixed small task set.

### Step 3 — Interception proxy (no redaction yet)
- **Goal:** a local OpenAI-compatible proxy sits between the runtime and Overshoot and can apply
  arbitrary modifications to both directions, transparently.
- **Build:** implement the proxy; point HoloDesktop's base URL at it; forward to Overshoot.
  Support streamed responses (constraint §8.7). Add a pass-through mode plus a **test hook** that
  can mutate request and response (e.g. tag a header, no-op rewrite the action).
- **Verify:** with the proxy in pass-through, the Step 2 task still completes unchanged. With the
  test hook on, an injected request/response modification is observable and the task still runs.
  Streamed responses work.

### Step 4 — Plug in the scrubber + vault (the privacy core)
- **Goal:** the proxy redacts the screenshot outbound and resolves placeholders inbound, using the
  §5 stub scrubber and the §6 vault.
- **Build:** request leg → call `redact(frame)`, replace the image with `redactedFrame`, store
  spans in the vault, and scrub leaked values from message/text history. Response leg → parse the
  action, resolve placeholders **only in type-text**, forward the rewritten action. Enforce all
  §8 constraints and the §6 secret/credential rules.
- **Verify (build fixtures):**
  - With the stub set to "detect fixture X", the request forwarded to Overshoot contains the
    **redacted** image and **no** real value (assert on captured request bodies).
  - A model response containing `EMAIL_1` in a type action is resolved to the real value before
    execution; the same placeholder in prose is **not** touched.
  - Secret-tier fixtures are masked and their placeholders are **never** resolvable.
  - After a value is "typed", the next request's text history no longer contains the raw value
    (history scrub works).
  - Every injected stage failure yields no forwarded request/response (fail-closed).
  - No log line contains a real value, transcript, or frame.

### Step 5 — (Optional) Gradium speech-to-text for computer audio
- **Goal:** live computer audio becomes redacted text context the CUA can reason with.
- **Build:** capture system/loopback audio (OS-specific — the fiddly part), stream to Gradium STT
  (WebSocket), and inject **finalized** transcript segments into **each step's request** via the
  proxy (the runtime owns the loop, so the initial task string cannot carry mid-task audio; the
  proxy is the per-step injection seam). Run every transcript segment through the **same** text
  scrubber + vault before injection, sharing placeholders with the screen.
- **Verify:** a spoken value mid-task appears to the model only as a placeholder (never raw) and is
  resolvable exactly like an on-screen value; with audio disabled, behavior is unchanged.

---

## 10. Definition of done (this component)

- HoloDesktop runs `Hcompany/Holo3-35B-A3B` via Overshoot under OpenShell isolation.
- The proxy intercepts both directions, redacts outbound frames, resolves inbound actions, and
  handles streaming.
- All Step 4 fixtures pass; every §8 constraint has a direct test.
- No key, real value, transcript, or frame is ever committed or logged.
- The scrubber remains a clean plug-in seam (real detector can replace the stub with no proxy
  changes).
