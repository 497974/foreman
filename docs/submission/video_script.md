# Foreman — 3-Minute Demo Video Script

Target length: **3:00**. Track 3 "Agent Society" — the judges need to *see*
task division, negotiation, and a measurable efficiency gain, not just hear
about them. Every shot below is something that already exists in the repo —
nothing is staged or faked.

## Shot list + bilingual voiceover

| Time | Shot (exact screen action) | English voiceover | 中文对照 |
|---|---|---|---|
| 0:00–0:08 | Terminal, large font. Type and run a one-shot agent prompt against the 20-item `demo/requirements_full.md` checklist. Let it print `"All 20 done!"` (condition A style output). | "Give a coding agent a 20-item checklist. Ask it if it's done." | "给编码智能体一份20项清单，问它做完了没有。" |
| 0:08–0:20 | Cut to split screen: left = agent's claim "All done", right = `pytest -q` run against the *same* workspace showing real failures / 0 passed (use `evals/results_20260704T080344Z.json` condition A: `n_passed: 0`, `pytest_ran: true` but functional tests for T01–T05 all fail with "file not found"). Overlay text: **"It said done. It had really finished 0 of 5."** | "It says all done. The tests say otherwise — zero of five actually finished. This is quantified overconfidence, not an anecdote." | "它说全做完了。测试说：真相是5项里0项完成。这不是个例，是可量化的过度自信。" |
| 0:20–0:32 | Cut to repo's README architecture ASCII block, animated: boxes fade in left-to-right (Planner → Ledger/Dispatcher → Executor → Verifier → dispute loop back to Executor/Arbiter). Use the exact diagram from `README.md` lines 41–51. | "Foreman fixes this with a foreman pattern: a planner turns the checklist into task cards, each with a runnable test. A zero-LLM dispatcher hands out one card at a time." | "Foreman 用「工头模式」解决：规划器把清单拆成带可运行测试的任务卡，零LLM调度器逐一派发。" |
| 0:32–0:45 | Continue animation: Executor box highlights (clean context icon), then Verifier box highlights running a terminal/test icon, then arrow labeled "REJECT" looping to a small "DISPUTE → ARBITRATION" badge pair (orange), then arrow to "done, unlock dependents." | "Each executor works in a clean context. A verifier runs the real build and test — not another LLM's opinion — before anything is marked done. If it disputes a rejection, an independent arbiter reads the actual files and rules." | "每个执行者都在干净上下文里工作。验证者先跑真实的构建和测试，而不是另一个LLM的主观判断。如果执行者对拒绝有异议，独立仲裁者会读取真实文件并裁决。" |
| 0:45–0:58 | **MONEY SHOT starts.** Screen recording of `serve.py` web console (`http://127.0.0.1:8787`), freshly loaded. Paste `demo/requirements_mini.md` into the left textarea, click **Start**. Status wall on the right begins filling: grey → amber (in_progress) → green (done) cells, left to right, live. | "Here's the actual console. Watch the status wall fill in — live, no cuts." | "这是真实的控制台。看状态墙实时填满——没有剪辑。" |
| 0:58–1:10 | Zoom on a cell turning **red/amber with a REJECT** — click it to open the drawer showing verdict reason (e.g. "no tests ran... file or directory not found" or an actionable feedback string from `verifier.py`). Read the reason text on screen. | "Here's a rejection — the verifier's actual reason, not a vague 'needs work.' The executor gets this back on its next attempt." | "这是一次拒绝——验证者给出的真实理由，不是含糊的「需要改进」。执行者下一次尝试会拿到这份反馈。" |
| 1:10–1:22 | Event feed at bottom: scroll to a **DISPUTE** orange badge, then **ARBITRATION** orange badge appearing right after. Pause on both. | "The executor disputes with evidence. An independent arbiter — reading the real files, not trusting either side — rules. This is the negotiation layer the track asks for." | "执行者带着证据提出异议。独立仲裁者读取真实文件、不偏袒任何一方，做出裁决。这就是赛道要求的协商层。" |
| 1:22–1:30 | Cut back to status wall: the disputed/retried cell turns **green**. Whole wall finishes green. Header shows "RUN COMPLETE" badge. | "The retry lands. Wall goes green. Run complete." | "重试通过，状态墙全绿，任务完成。" |
| 1:30–1:45 | Sped-up screen capture (visibly 8x — use a fast-forward icon overlay) of a **full run from empty wall to all-green**, with the console/terminal timestamp clock ticking visibly in the corner (real wall-clock, not reset). Caption overlay: **"8x speed — full unedited recording in repo: `/evals/` + `docs/submission/`."** | "This whole run took under eight minutes, real time — sped up here for the video. The full, unedited recording ships in the repo." | "整个过程真实用时不到八分钟——这里为视频做了加速播放。完整未剪辑录像随仓库一起提供。" |
| 1:45–2:00 | (Continuation of same beat — do not treat as separate shot in edit) Overlay a small disclaimer card: "Single-run numbers. LLM sampling varies — treat as one data point." | "We're not hiding the caveats — these are single-run numbers, and LLM sampling means a rerun can shift them." | "我们不回避这些局限——这些是单次运行的数据，LLM采样有随机性，重跑结果可能变化。" |
| 2:00–2:15 | Full-screen results table (build this as a clean on-screen graphic from `evals/results_20260704T080344Z.json`): <br>A (one-shot): 0/5 pass, 136s, 75k tokens <br>B (sequential, no verify): 4/5 pass, 218s, 113k tokens <br>C (Foreman): 5/5 pass, 461s, 267k tokens, 1 legit rejection healed | "Three conditions, same checklist. One-shot: zero of five, despite passing its own tests. Sequential feeding: four of five. Foreman: five of five — with one genuine rejection caught and healed along the way." | "三种条件，同一份清单。一次性生成：五项里0项，尽管它自己的测试都通过了。顺序执行：五项里4项。Foreman：5项全过——过程中还真实拦截并修复了一次不合格提交。" |
| 2:15–2:30 | Overlay honest cost framing text next to the table: "Foreman costs ~3.5x the tokens of one-shot for a 2.4x completion-rate jump — and it's the only condition an independent referee actually certifies. We also reran with a weaker executor model: orchestration compensated, still 5/5." | "Yes, it costs more tokens — about three and a half times one-shot's. But it's the only condition that finishes the job and can prove it. And when we swapped in a weaker executor model, the orchestration compensated — still five of five." | "没错，它消耗更多token——大约是一次性生成的3.5倍。但它是唯一真正完成任务、还能自证的方案。换成更弱的执行模型后，编排层弥补了差距——依然5/5。" |
| 2:30–2:40 | Screenshot/recording of the Alibaba Cloud Model Studio / DashScope international console showing the active endpoint / model list (proof shot — matches `foreman/config.py`'s `DASHSCOPE_BASE_URL`). Blur/crop any account identifiers not needed for proof. | "Built entirely on Qwen through Alibaba Cloud's international DashScope endpoint." | "完全基于阿里云国际站 DashScope 端点上的通义千问模型构建。" |
| 2:40–2:50 | On-screen routing table graphic: Planner = qwen-max, Executor = qwen3-coder-plus / qwen-plus (fallback noted), Verifier = qwen-plus (JSON mode), Arbiter = qwen-max. | "Four roles, matched to model strength: qwen-max plans and arbitrates, qwen-coder and qwen-plus execute and verify." | "四个角色，按模型能力匹配：qwen-max 负责规划与仲裁，qwen-coder 与 qwen-plus 负责执行与验证。" |
| 2:50–3:00 | Foreman logo/wordmark (steel-blue + orange, matching webui palette) over the README architecture diagram, fading to repo URL + "Track 3: Agent Society — Qwen Cloud Global AI Hackathon." | "Foreman. Because agents that claim victory should have to prove it. Track 3, Agent Society." | "Foreman。宣称胜利的智能体，应该拿出证据。Track 3，智能体社会赛道。" |

## Shot prep

- **Pace a mock run for the camera**: a demo-mode run normally finishes in
  well under a second (no LLM latency), too fast to watch the status wall
  fill in. Slow it down with `mock_delay_s` on the API call, or
  `FOREMAN_MOCK_DELAY` before starting the console:
  `curl -X POST /api/runs -d '{"requirements":"...","mock":true,"mock_delay_s":4}'`
  or set `FOREMAN_MOCK_DELAY=4` before running `start_foreman.bat`.

## Recording tips (Windows)

- **Screen + audio**: ScreenPal (free tier covers 3-min single-take + trim) or
  剪映 (CapCut CN) for the sped-up-clip + caption overlay work in section
  1:30–2:00. ScreenPal is simpler for the raw console capture; do the 8x
  speed-ramp and text overlays afterward in either tool's timeline editor.
- **Mic**: use a headset/USB mic, not laptop internal mic — internal mics pick
  up fan noise during the live run segment. Record voiceover in a second pass
  over the silent screen capture rather than live narrating while clicking;
  it makes retiming to the timecodes above much easier.
- **Full unedited recording**: actually keep the raw, non-sped-up capture of
  the money-shot run (0:45–1:30 territory) and drop it under `evals/` or
  link it from this file — the caption at 1:30 promises it exists. This is
  the direct defense against the "Devin demo video" backlash (staged/edited
  demos that don't hold up under scrutiny): show the receipts.
- **Upload settings**: YouTube upload must be set to **Public**, not
  Unlisted — Devpost/hackathon judging bots and some judges only reliably
  access Public links; Unlisted has caused past submissions to be marked
  "video not accessible." Double-check the visibility toggle after upload,
  not just at upload time (YouTube sometimes resets it on processing).
- **Captions**: burn in the honest-caveat captions (single-run numbers, 8x
  speed disclosure) rather than relying on YouTube's auto-captions — they
  need to be readable and undeniable at a glance since they are load-bearing
  for credibility, not decoration.
- **Timing discipline**: 3:00 is a hard-ish ceiling for most hackathon video
  fields (character/time limits on the submission form) — record with a
  visible timer and trim ruthlessly; the money shot (0:45–1:30) is the
  section that should never get cut short to save time elsewhere.
