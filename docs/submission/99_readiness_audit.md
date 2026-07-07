# Foreman — Submission Readiness Audit

Generated 2026-07-07. Deadline is tomorrow; submitting today. This is the
final gate before hitting submit on Devpost. Brutally honest, no sugarcoating.

Verified directly (not just read from docs) at audit time:
- `gh api repos/497974/foreman` → `isPrivate: false`, `license.spdx_id: Apache-2.0`,
  description field is filled in.
- `docs/architecture.svg` exists on disk.
- `foreman/config.py` and `foreman/llm.py` exist and do contain real DashScope
  client / `chat.completions.create` code.
- `fc/foreman-fc.zip` exists on disk (built package present).
- No `.mp4`/`.mov`/`.webm` file exists anywhere in the repo or working tree —
  **no video has been recorded yet, demo or deployment-proof.**
- No evidence in the repo of an actual live FC deployment (no saved
  `fcapp.run` URL, no deploy log, no screenshot) — `00_evidence_spine.md`
  and `30_deployment_proof_checklist.md` both say this step is planned, not
  done.
- `docs/submission/00_evidence_spine.md`, `10_devpost_description.md`,
  `20_video_script.md`, `30_deployment_proof_checklist.md` are all
  **untracked** in git (not committed, not pushed) as of this audit.
- `foreman/executor.py` has an uncommitted 34-line diff sitting in the
  working tree.
- Two **stale duplicate files** sit in the same folder and contradict the
  current plan: `docs/submission/checklist.md` and
  `docs/submission/video_script.md` (older, dated July 4) — the old
  `video_script.md` describes a single combined video with an embedded
  cloud-console screenshot, not the two-separate-recordings structure the
  official rules actually require and that `20_video_script.md` /
  `30_deployment_proof_checklist.md` correctly implement. `blog_post.md` and
  `devpost_description.md` (old, undated-suffix versions) also still exist
  alongside the new `10_devpost_description.md`.

## (A) Requirement Checklist

| Requirement | Status | Notes / Owner |
|---|---|---|
| Public repo | **DONE** | Confirmed via `gh api`: `isPrivate: false`. |
| Open-source license detectable in About section | **DONE** | `gh api` reports `license.spdx_id: Apache-2.0` — this is GitHub's own detector reading the root `LICENSE` file, which is exactly what populates the About-section badge. Visually re-confirm once in a logged-out browser tab as final sanity check (5 seconds) — owner: user, trivial. |
| Architecture diagram | **DONE** | `docs/architecture.svg` exists on disk. Not yet re-verified that it's linked/embedded from the README so a judge lands on it without digging — owner: user, 2-minute check. |
| Alibaba Cloud API usage — code evidence link | **DONE (content)** / **TODO (link)** | `foreman/config.py` (DashScope client factory, base_url) and `foreman/llm.py` (`chat.completions.create` call site) are real and correctly described. The actual GitHub permalinks to paste into Devpost's field have not been generated yet — owner: user, trivial once repo state is final (use permalinks, i.e. commit-SHA-pinned URLs, not `main`-branch links that can drift). |
| Alibaba Cloud deployment — separate short recording | **TODO — NOT STARTED, highest risk item** | No FC function has been confirmed live. No recording file exists anywhere. `30_deployment_proof_checklist.md` is a well-written runbook but is a plan, not evidence. Owner: **user**, must run on the Alibaba Cloud console + screen-record locally. This is the single biggest blocker to a valid submission. |
| Public ~3-minute demo video | **TODO — NOT STARTED** | Script (`20_video_script.md`) is finished and tight (~450 words, fits 3:00). No recording exists, no YouTube upload, no public link. Owner: user, needs the live console + demo-mode walkthrough recorded, then uploaded set to **Public** (not Unlisted) and re-checked after processing. |
| Text description | **DONE (content)**, **TODO (paste)** | `10_devpost_description.md` is complete, well-evidenced, honest about limitations. Not yet pasted into the actual Devpost form fields (inspiration/what it does/how built/challenges/accomplishments/learned/what's next). Owner: user, mechanical copy-paste, ~10 min. |
| Track identification | **DONE (content)**, **TODO (form field)** | Track 3 (Agent Society) is correctly and consistently identified in README, evidence spine, and Devpost description text. Still must be explicitly selected in Devpost's track/category **dropdown field** — a separate UI action from writing "Track 3" in prose, easy to forget. Owner: user, 10 seconds, do not skip. |
| Submission docs committed/pushed | **AT-RISK** | The four current submission files are untracked in git as of this audit. If judges or Devpost's "link to a repo file" review looks for these paths on GitHub and they were never pushed, any cross-reference to them (there mostly isn't one required, but internally you're relying on their committed content matching what you paste) is invisible upstream. Not a hard submission-rule violation (Devpost doesn't require the submission docs themselves to be in the repo), but should be committed for your own audit trail. Owner: user/agent, trivial `git add && commit && push`. |
| Working tree cleanliness | **AT-RISK** | `foreman/executor.py` has an uncommitted 34-line diff. Unknown if this is a fix that should ship or an experiment left dangling. If it's a real improvement relevant to what the demo video shows, it needs to be committed before recording the demo (otherwise the video shows behavior the public repo doesn't actually contain). Owner: user, decide and commit or discard **before** recording. |
| Stale duplicate docs in submission folder | **AT-RISK** | `checklist.md` and `video_script.md` (old, July 4) contradict the current two-recording plan; `blog_post.md`/`devpost_description.md` (old) duplicate `10_devpost_description.md`. Risk: whoever executes today (user, possibly tired, deadline pressure) opens the wrong file and follows a stale script that doesn't match the actual official two-recording requirement. Owner: user/agent — delete or clearly mark old files as superseded, right now, before execution begins. |
| Free-tier / no-coupon reproducibility claim | **DONE** | Evidence spine is explicit and consistent about which models are confirmed-working (`qwen-max`, `qwen3-coder-flash`, fallbacks `qwen-turbo`/`qwen3-max`) vs exhausted (`qwen-plus`, `qwen-flash`, `qwen3-coder-plus`). This framing is consistently carried into the Devpost description and video script. No action needed, just don't let a last-minute edit reintroduce an exhausted model name as if it were live. |
| A/B/C evaluation numbers consistency | **DONE**, verify once more before publishing | Numbers (0/5, 4/5, 5/5; 136s/218s/461s; 75,253/112,912/267,193 tokens) are identical across evidence spine, Devpost description, and video script. `checklist.md`'s "numbers sanity pass" step is good practice — do it once, at the very end, across whatever the final three public-facing docs turn out to be. |

## (B) Ordered Critical Path for TODAY

This is sequenced so that nothing gets recorded twice and nothing gets
recorded against a moving target.

**Phase 0 — Repo hygiene (do first, ~10 min, mixed owner)**
1. Decide on `foreman/executor.py`'s uncommitted diff: commit it if it's a
   real fix, discard if it's stray. Do this **before** anything gets
   recorded, since both videos will show live behavior of whatever's in the
   repo at record time.
2. Delete or clearly rename the stale files (`checklist.md`,
   `video_script.md`, `blog_post.md`, `devpost_description.md` — the old
   undated-suffix ones) so there is exactly one current copy of each
   document type in `docs/submission/`. (Agent can do this now, zero risk.)
3. Commit and push the four current submission docs
   (`00_evidence_spine.md`, `10_devpost_description.md`,
   `20_video_script.md`, `30_deployment_proof_checklist.md`) plus whatever
   was decided in step 1. (Agent can do this now.)

**Phase 1 — Alibaba Cloud deployment (user, console + recording, ~45–60 min)**
4. Follow `30_deployment_proof_checklist.md` Steps 1–5 exactly: rebuild/
   verify the zip, create the FC web function in **Singapore**, Custom
   Runtime, set the six environment variables (pinning to confirmed-working
   free-tier models), set **max-instances = 1**, deploy, get the
   `fcapp.run` URL, smoke-test it loads and a demo-mode run completes.
5. **Record the separate 30–60s deployment-proof video** per Step 6 of that
   checklist: console region/runtime → Triggers tab URL → live browser hit
   → mock run completing → cut to `foreman/config.py` / `foreman/llm.py`.
   This must be its own file, not spliced into the 3-minute demo.
6. Immediately shut down the function (Step 7) — delete or zero
   max-instances — same session, don't leave it live.
7. Upload the deployment-proof recording somewhere with a stable link
   (repo release asset, YouTube unlisted-is-fine-here-since-it's-not-the-main-demo-video-but-check-rules,
   or wherever the submission form's "deployment proof" field expects it).

**Phase 2 — Public demo video (user, recording + upload, ~30–45 min)**
8. Record the ~3:00 demo per `20_video_script.md`: cold open →
   loop diagram → real console run (planner=qwen-max,
   executor=qwen3-coder-flash, real DashScope calls, not mock) → verification
   passing live → switch to Demo/mock mode for dispute/arbitration/parallel/
   stop-resume/cost readout → A/B/C table → closing card with repo URL,
   license, track.
9. Upload to YouTube, visibility **Public** (not Unlisted). Re-check the
   visibility toggle **after** processing finishes, not just at upload —
   YouTube has reset this before.
10. Copy the public video URL; open it in a logged-out/incognito window to
    confirm it's actually reachable without being signed in as the owner.

**Phase 3 — Devpost form assembly (user, ~20–30 min)**
11. Paste `10_devpost_description.md` content into the corresponding Devpost
    form sections.
12. Explicitly select **Track 3 — Agent Society** in the track/category
    dropdown (separate field from the text).
13. Paste the public demo video link into the video field; verify logged-out.
14. Paste the deployment-proof recording link into whatever field the
    platform provides for it (if Devpost has no dedicated separate field,
    put it clearly labeled in the description or an explicit "Alibaba Cloud
    Deployment Proof" section/link — do not let it get lost inside the main
    video).
15. Paste the two Alibaba API evidence links — GitHub permalinks (commit-SHA
    pinned, not `main`) to `foreman/config.py` and `foreman/llm.py`.
16. Fill built-with/technology tags: Qwen, DashScope, Alibaba Cloud Function
    Compute, Python, etc.

**Phase 4 — Final numbers/consistency pass (user or agent, ~10 min)**
17. Grep every place a number appears (video narration/captions, Devpost
    text) against `evals/results_20260704T080344Z.json` — 0/5, 4/5, 5/5;
    136s/218s/461s; 75,253/112,912/267,193 tokens — and confirm the
    "single run, not an average" caveat rides along everywhere the numbers
    do.
18. Re-open the repo URL in a private/incognito window as the very last
    step: confirm public, confirm license badge visible, confirm README
    renders (architecture diagram, tables, code fences) correctly on
    GitHub's actual renderer, confirm no API key anywhere in tracked files
    or commit history.
19. Submit.

## (C) What Could Still Disqualify Us — Unvarnished

1. **The two required recordings do not exist yet, at all, at time of this
   audit, with the deadline tomorrow.** Everything else is polish; this is
   the actual submission. If today runs out before both are captured,
   uploaded, and public, there is no valid submission regardless of how good
   the written docs are. This is the single real risk and it dwarfs
   everything else on this list.
2. **FC deployment has never been executed end-to-end.** The checklist is
   detailed and looks correct on paper, but it is untested against the real
   console. First-time FC users routinely hit friction on exactly the two
   things flagged as likely failure points (bootstrap path, missing env var)
   — if that eats an hour of the one day available, it directly threatens
   item 1.
3. **Max-instances / provisioned-concurrency mistake could both break the
   proof and cost money.** If max-instances is left above 1 or provisioned
   concurrency is left on, polling can 404 against the wrong instance
   *during the recording itself*, forcing a re-record under worse time
   pressure, and it also risks a real bill on a "free tier, no coupon"
   submission whose whole pitch is zero cost.
4. **YouTube visibility silently reverting to Unlisted after processing** is
   a documented failure mode called out in the team's own old checklist —
   if it happens after upload and isn't re-checked, the "public video"
   requirement fails invisibly; a judge just sees a private/unlisted link
   and the submission is likely disqualified or heavily penalized with zero
   warning to the team.
5. **The deployment-proof recording could accidentally leak the DashScope
   API key** on screen (env-vars panel, address bar, terminal history). This
   is called out in the script but depends entirely on the person recording
   remembering to crop/blur in the moment — an easy miss under deadline
   stress, and a real credential leak, not just a cosmetic issue.
6. **`foreman/executor.py`'s uncommitted diff is an unknown quantity.** If
   whatever it changes is visibly exercised in the demo recording but never
   committed/pushed, the video shows functionality the public repo doesn't
   contain — a discrepancy a careful judge could notice, and worse, an
   honesty problem for a project whose entire pitch is "we don't let claims
   outrun proof."
7. **Stale duplicate files in the submission folder are a self-inflicted
   footgun.** Under time pressure, it is entirely plausible the user opens
   the old `video_script.md` (which describes a different, non-compliant
   single-video structure) instead of `20_video_script.md`, wasting the one
   irreplaceable resource today actually has: time.
8. **The A/B/C numbers and "single run, not average" caveat must survive
   every paste.** It's currently consistent across three docs; one hurried
   copy-paste that drops the caveat while keeping the number turns an honest
   result into something that reads as cherry-picked — a credibility risk
   with judges who explicitly reward honesty per the project's own framing.
9. **Devpost's track dropdown is a separate field from the prose.** Every
   document says "Track 3" in text; none of that satisfies the form if the
   literal dropdown/category selector isn't also set. This is a one-click
   action that is easy to simply forget in the adrenaline of final
   submission.
10. **No slack left for a re-record.** With recording, upload/processing
    time, and form assembly all still ahead on the actual deadline day, any
    single failed take, upload error, or console hiccup consumes the
    remaining buffer directly. There is no "do it tomorrow instead" — that
    buffer day was explicitly flagged (in the team's own older checklist) as
    not to be relied on.

**Bottom line:** the written/evidentiary case for Foreman is strong,
consistent, and unusually honest about its own limitations — that part of
the submission is in good shape. The entire remaining risk is procedural and
time-bound: two recordings and one live cloud deployment that have not been
started, on the last day. Nothing on the content side is at risk; everything
on the execution-today side is.
