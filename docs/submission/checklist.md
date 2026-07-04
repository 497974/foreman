# Foreman — Final Submission-Day Checklist

Qwen Cloud Global AI Hackathon — Track 3: Agent Society.
**Soft deadline: July 8. Hard deadline: July 9, 2:00 PM PT — do not rely on
the buffer day; treat July 8 as the real deadline.**

## Repo

- [ ] Repo is **public** (not private, not "internal") — verify by opening
      the repo URL in a private/incognito browser window while logged out.
- [ ] **LICENSE is visible at the top of the repo page** — GitHub/GitLab
      shows a license badge near the repo name only when a recognized
      `LICENSE` file is at repo root; confirm `LICENSE` (Apache-2.0) is at
      the root, not nested under `docs/` or elsewhere.
- [ ] Repo **description** field (the one-line summary under the repo name)
      is filled in — short, states what it is and Track 3, not blank.
- [ ] `README.md` renders correctly on the repo homepage: architecture ASCII
      diagram displays without broken formatting, the failure-mode table
      renders as a table (not raw pipe characters), Quickstart command block
      is copy-pasteable.
- [ ] Architecture diagram is included and visible in the README (the ASCII
      block, `README.md` lines ~41–51) — confirm it survives whatever
      markdown renderer the judges' platform uses; ASCII art in a code fence
      is the safest choice, keep it that way rather than replacing with an
      image.
- [ ] `.env` is **not** committed (check `.gitignore` already excludes it —
      confirmed present) and no API key appears anywhere in tracked files,
      commit history, or the shakedown/eval logs (`evals_shakedown*.log`,
      `evals/results_*.json`) — grep the whole repo for the DashScope key
      pattern before the final push.
- [ ] Large/generated eval workspace directories
      (`evals/workspaces_*/`) are either committed intentionally (as
      evidence) or excluded — a decision, not an accident; if committed,
      confirm they don't also leak secrets.

## Video

- [ ] Video is recorded, ~3:00 runtime, covering all beats in
      `docs/submission/video_script.md`.
- [ ] Uploaded to YouTube (or platform of choice) set to **Public** —
      **not Unlisted**. Re-check the visibility toggle *after* processing
      completes, not just at upload time; YouTube has been known to reset it.
- [ ] Video link is pasted into the Devpost submission form's video field,
      and separately verified by opening the link in a logged-out /
      incognito browser window.
- [ ] The sped-up run segment (1:30–2:00 in the script) has a visible,
      genuine timestamp/clock on screen, and the caption crediting "8x
      speed, full recording in repo" is burned in, not relying on a spoken
      line alone.
- [ ] The full, unedited recording of the money-shot run promised by that
      caption actually exists in the repo (linked from `evals/` or this
      submission folder) — the caption is a claim; make sure it's true
      before publishing.

## Devpost submission text

- [ ] `docs/submission/devpost_description.md` content is pasted into the
      Devpost project description fields (inspiration / what it does / how
      we built it / challenges / accomplishments / what we learned /
      what's next all present as separate sections or headers per Devpost's
      form).
- [ ] **Track selection**: Track 3 — Agent Society is explicitly selected in
      the submission form's track/category dropdown (not just mentioned in
      text) — this is a separate form field from the description and it is
      easy to forget.
- [ ] Built-with / technology tags include Qwen, DashScope/Alibaba Cloud,
      Python, and any other real dependencies — matches `requirements.txt`.

## Alibaba Cloud proof

- [ ] **Alibaba Cloud proof file link** is included in the submission:
      point to `foreman/config.py` (shows the DashScope
      international `base_url`, per-role model env vars) as the code-level
      proof, AND include the console screenshot/proof shot referenced in
      the video script (2:30–2:40) showing the live Model Studio /
      DashScope endpoint.
- [ ] Confirm the proof doesn't accidentally include a visible API key in
      the screenshot (crop/blur the key field, keep the endpoint/model list
      visible).

## Blog

- [ ] `docs/submission/blog_post.md` is posted to dev.to (or chosen
      platform) with the front-matter tags (`ai, agents, qwen, hackathon`)
      applied as actual platform tags, not just YAML text.
  - Note: front-matter tags exist for dev.to auto-import from GitHub;
    if pasting into dev.to's own editor instead, re-apply the tags manually
    in the editor's tag field since pasted markdown does not always carry
    front-matter through.
- [ ] Blog post link is attached to the Devpost submission (separate field
      from the video link — don't conflate them).
- [ ] Any code snippets in the blog (the DashScope error message, the
      routing table) render correctly on the target platform — preview
      before publishing, dev.to's code fences sometimes mangle nested
      quotes in JSON error strings.

## Numbers sanity pass (do this last, right before submitting)

- [ ] Every place a number appears (video captions, Devpost text, blog post)
      — 0/5, 4/5, 5/5, 136s/218s/461s, 75k/113k/267k tokens — matches
      `evals/results_20260704T080344Z.json` exactly. If the eval is rerun
      before submission and numbers shift, update **all three** documents,
      not just one.
- [ ] The "single-run, not averaged, sampling variance" caveat appears
      wherever the numbers appear — video caption, Devpost accomplishments
      section, and blog results section all currently have it; don't let an
      edit silently drop it from one.

## Timing

- [ ] Aim to have everything above done and double-checked by **July 8**,
      treating July 9 2:00 PM PT as a true hard stop with zero slack for
      timezone confusion, upload processing time, or last-minute re-records.
- [ ] Confirm the deadline's timezone (PT) against your local time
      explicitly the day before — don't do this conversion under pressure
      on submission day.
