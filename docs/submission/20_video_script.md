# Foreman — Public Demo Video Script (~3:00)

Target: YouTube, public. Total spoken word budget ≈ 430–460 words for a
relaxed ~150 wpm pace across ~3:00. Word budgets per beat are approximate —
optimize for natural delivery over hitting the number exactly.

Format: TIME | ON-SCREEN ACTION | EXACT NARRATION.

| TIME | ON-SCREEN ACTION | EXACT NARRATION |
|---|---|---|
| 0:00–0:25 (~65 words) | Cold open: screen recording of a normal coding-agent chat session. Show a 20-item requirements checklist pasted in, agent replying, then a fast time-lapse/jump-cut to the agent's final message claiming completion. Overlay text on screen: "20 requirements. 1 hour. Claimed: all done." | "Here's a checklist of twenty requirements for a small app — auth, filtering, pagination, all of it. I hand it to a coding agent and let it run for an hour. It comes back and says: all done. So I check. Three of the twenty are actually finished. The other seventeen are either missing, broken, or quietly skipped — and the agent never told me." |
| 0:25–0:40 (~40 words) | Cut to a simple title card / diagram: Planner → Ledger → Executor → Verifier → Dispute/Arbitration loop (can reuse the ASCII loop diagram from the evidence spine, rendered cleanly). | "That's the problem Foreman solves. Instead of trusting one long agent run, Foreman plans the checklist into small tasks, hands each one to an executor with a clean context, and refuses to call anything done until an independent verifier proves it." |
| 0:40–1:25 (~110 words) | Switch to screen recording of the real Foreman web console. Show the "Demo mode" checkbox UNCHECKED, model config visible showing planner = qwen-max, executor = qwen3-coder-flash. Start a real run on the 5-item checklist (`demo/requirements_mini.md`). Let the status wall show tasks moving PENDING → READY → IN_PROGRESS → PENDING_REVIEW → DONE in real time, live token/cost ticking. | "This is the real thing, live, no mock mode — planner is qwen-max, executor is qwen3-coder-flash, both on free DashScope quota, so anyone watching can reproduce this for zero dollars. I load a five-item checklist. Foreman's planner breaks it into a dependency-ordered task list. Watch the board: each task claims itself, an executor picks it up in a clean context, writes the code, and submits. Then — this is the part a single agent never does — an independent verifier actually runs the tests before anything is marked done." |
| 1:25–1:45 (~55 words) | Console continues: one task goes to PENDING_REVIEW then back for a retry (or highlight a DONE transition with the verifier's pass evidence visible — checkmarks, gate output). All 5 items reach DONE. | "No LLM opinions until the real gate passes — pytest runs for real, against the actual files on disk. Here, all five tasks clear verification and the board goes green end to end. That's the loop: plan, execute, verify, done — not claimed done." |
| 1:45–2:15 (~80 words) | Switch to Demo/mock mode (check the "Demo mode" box, no API key). Kick off a run showing: (a) amber DISPUTE badge appearing, then ARBITRATION badge, then resolved; (b) two runs going in parallel side by side; (c) click Stop on one run, then Resume, showing it picks back up on the same ledger; (d) per-run live cost readout ticking. | "Now, no API key at all — this is Demo mode, fully offline, scripted but running the exact same engine. Here's what negotiation actually looks like: a task gets rejected, the executor disputes it, and an arbiter — same tier as the planner — rules on the evidence. You can run tasks in parallel. You can stop a run mid-flight and resume it later from the same ledger, nothing lost. And every run shows its live token cost as it happens." |
| 2:15–2:40 (~65 words) | Cut to the A/B/C evaluation table on screen (rendered cleanly — condition, referee pass, wall-clock, tokens). Highlight the pass column: 0/5, 4/5, 5/5. | "So does this actually work better? Same checklist, same executor model, one independent referee that never trusts Foreman's own verdicts. Condition A — one agent, one shot — zero of five pass. Condition B — split into tasks but no verification — four of five. Condition C — full Foreman — five out of five. Full verification costs about two-point-four times the tokens of B — for going from partial credit to all five, referee-confirmed." |
| 2:40–3:00 (~35 words) | Final card: Foreman logo/name, GitHub URL large and readable, license (Apache-2.0), track (Track 3, Qwen Cloud Global AI Hackathon). | "Foreman is open source, Apache-2.0, running entirely on free Qwen quota. The repo is at github.com/497974/foreman — clone it, run it yourself, no coupon needed. Thanks for watching." |

Total: ≈ 450 words spoken, fits comfortably inside 3:00 at a natural pace,
leaving slack for the visual beats (status-wall transitions, badge
animations) that need a second or two of silent screen time each.

---

## **SEPARATE 30–60s Alibaba Cloud Deployment-Proof Recording**

**This is a DIFFERENT recording from the demo video above — the hackathon
rules require the cloud deployment proof to be its own, separate clip. Do
not splice it into the 3-minute demo; upload/submit it as its own file.**

Suggested shot list for this second recording (no narration strictly
required, but a short voiceover is fine — keep it under ~60s total):

1. **Alibaba Cloud Function Compute console** — show the function's
   overview page with the **region set to Singapore** clearly visible in the
   console chrome/breadcrumb.
2. **Custom runtime config** — open the function's configuration tab showing
   "Custom Runtime" as the runtime type, plus the handler/startup command
   pointing at `serve.py`, and the deployed package (the vendored
   `fc/foreman-fc.zip`) if visible in the console.
3. **Function config detail** — show memory/timeout/region settings and
   environment variables panel (values redacted/blurred if it displays the
   DashScope API key) to prove this is a real deployed configuration, not a
   local screenshot.
4. **Open the live `fcapp.run` URL in a browser** — navigate to the public
   Foreman console URL served by the deployed function, showing the UI load
   from the cloud instance (not localhost — the address bar should visibly
   show the `fcapp.run` domain).
5. **Kick off a mock run on the deployed instance** — check "Demo mode" on
   the cloud-served console and run it to completion (PENDING → DONE across
   the board), proving the cloud backend actually executes the engine
   end-to-end, not just serving a static page.

Keep this recording tightly cropped to these five beats — console → runtime
config → function config → live URL in browser → mock run completing — so a
judge can verify in under a minute that the deployment is real, in
Singapore, on a custom runtime, and functionally alive.
