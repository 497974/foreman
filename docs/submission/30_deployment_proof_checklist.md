# Foreman → Alibaba Cloud FC — Deployment Proof Checklist (do today)

Copy-paste, do-it-now checklist for a first-time FC user. Goal: deploy
Foreman's web console to Alibaba Cloud Function Compute on the **free tier
(no coupon needed)**, get a public `fcapp.run` URL, and capture the
deployment-proof recording for the submission — all in one sitting.

Source docs this is derived from: `docs/submission/00_evidence_spine.md`,
`docs/DEPLOY.md`. If anything here conflicts with `docs/DEPLOY.md`, that file
is the fuller reference — this is the fast path.

---

## COST GUARD — read this before you touch the console

- **FC's free tier is generous and this demo fits inside it**: a Custom
  Runtime web function with **max-instances = 1**, called a handful of times
  for a short recording, costs nothing under the standard FC free monthly
  quota (request count + compute-GB-seconds). No coupon required.
- **What could actually cost money — avoid these:**
  - Leaving **max-instances** above 1, or enabling any **provisioned
    concurrency / always-on instances** setting — provisioned concurrency is
    billed by the hour whether or not it's invoked, even on light usage. Keep
    it OFF and max-instances at **1**.
  - Leaving the function running for days while you iterate — fine while
    actively testing, but don't leave it deployed indefinitely "just in
    case."
  - Long-lived, high-frequency polling scripts hammering the endpoint after
    you're done recording.
- **After you've captured the recording (Step 6), come back and either:**
  1. Delete the function (Function Compute console → the function → Delete), or
  2. Delete the whole service (`foreman-svc`), or
  3. At minimum, set max-instances to 0 / disable the trigger.
  Do this the same day. Do not leave it live "for the judges to click" —
  the submission recording IS the proof; a live URL left running unattended
  is pure cost risk with no benefit.

---

## Step 1 — Confirm the zip is built and self-contained

The zip must already vendor `openai` (no console-side pip step needed on a
first deploy).

```bash
cd "C:\Users\24973\Desktop\Foreman"
python fc/build_package.py && python -c "import zipfile; z=zipfile.ZipFile('fc/foreman-fc.zip'); names=z.namelist(); print('total files:', len(names)); print([n for n in names if n.startswith('fc/vendor/openai')][:5]); print('has .env:', any('.env' in n for n in names)); print('has runs/:', any(n.startswith('runs/') for n in names))"
```

**Expect to see:**
- `total files: <a few hundred>`
- a non-empty list of `fc/vendor/openai/...` entries (proves `openai` is
  vendored inside the zip, not relying on a console install step)
- `has .env: False`
- `has runs/: False`

If `fc/vendor/openai/...` is empty, vendor it first, then rebuild:

```bash
pip install openai -t fc/vendor
python fc/build_package.py
```

---

## Step 2 — Create the FC web function (Singapore, Custom Runtime)

1. Log into the **Alibaba Cloud International** console →
   **Function Compute**.
2. Top-of-page region selector → set to **Singapore (ap-southeast-1)**.
   (Must match the DashScope international endpoint region.)
3. **Create Service** (if you don't have one yet) → name it `foreman-svc`
   (any name is fine).
4. **Create Function** → function type = **Web function (Web 函数)**.
5. **Runtime** = **Custom Runtime**, base image = any **Python 3.10+**
   option in the custom-runtime image list.
6. **Code upload** → "Upload zip" → select
   `C:\Users\24973\Desktop\Foreman\fc\foreman-fc.zip`.
7. **Startup command / entrypoint (bootstrap)**: leave the default
   (`./bootstrap`) if pre-filled; if the console asks you to type a path
   explicitly, enter `./fc/bootstrap`.
8. **Port**: set to **9000**.
9. Save this function (don't worry about env vars or scaling yet — next
   steps).

---

## Step 3 — Set environment variables

Function → **Configuration → Environment Variables** → add each of these
(exact keys, no quotes):

| Key | Value |
|---|---|
| `DASHSCOPE_API_KEY` | `sk-...` (your real key — never paste this into the zip or into git) |
| `DASHSCOPE_BASE_URL` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `FOREMAN_PLANNER_MODEL` | `qwen-max` |
| `FOREMAN_EXECUTOR_MODEL` | `qwen3-coder-flash` |
| `FOREMAN_VERIFIER_MODEL` | `qwen-max` |
| `FOREMAN_FALLBACK_MODELS` | `qwen-turbo,qwen3-max` |

These model overrides matter: they pin the **deployed** instance to the
confirmed-working free-tier models (per
`docs/submission/00_evidence_spine.md`'s no-coupon section), not the
exhausted `qwen-plus`/`qwen3-coder-plus` tiers used in the earlier local
eval run. Without this step the deployed demo can fail on an exhausted model.

Save.

---

## Step 4 — Set max-instances = 1

Function → **Configuration → Scaling / Instance settings** (label varies by
console version — look for "Max Instances" / "实例数" under
Concurrency/Scaling):

- Set **Max Instances = 1**.
- Confirm **provisioned concurrency / always-on instances is OFF / 0**.

This is required for correctness, not just cost: Foreman's run state
(`runs/<run_id>/ledger.db`, `events.jsonl`) lives on the instance's local
disk. If FC scales to a second instance, a poll can land on the wrong
instance and 404 with "unknown run." Max-instances = 1 guarantees every
request (start + all polls) hits the same instance. See `docs/DEPLOY.md` §4
for the full explanation.

Save.

---

## Step 5 — Deploy, get the URL, open it

1. Click **Deploy** (or **Save & Deploy**, wording varies).
2. Go to the function's **Triggers** tab → copy the default HTTP trigger
   URL — it looks like `https://<random>.<region>.fcapp.run`.
3. Open that URL in a browser tab.
4. **Verify it's really Foreman, not an error page**: you should see the
   same web console UI you get locally at `http://127.0.0.1:8787` — the
   Foreman header/title, a form to start a run, and (if you've run anything
   before) a run list.
5. Quick smoke test: start the smallest run you have (or use "Demo mode" /
   `--mock`-equivalent checkbox in the console if present, to avoid burning
   a real DashScope call just to prove the page loads) — confirm you get a
   `run_id` back and the events panel starts updating via polling.

If the page doesn't load: check the Triggers tab for the function's
invocation logs (Function Compute console → Logs) — most first-deploy
failures are either the bootstrap path (Step 2.7) or a missing env var
(Step 3).

---

## Step 6 — Recording script (30–60 seconds, exactly what to show/say)

Record your screen (Windows: Win+Alt+R for the Xbox Game Bar recorder, or
whatever's easiest) and narrate roughly this script:

1. **(0–10s) Show the FC console.** Have the function's overview page open:
   region selector visibly reading **Singapore**, function type **Web
   function**, runtime **Custom Runtime**. Say:
   *"This is Foreman's web function deployed on Alibaba Cloud Function
   Compute, region Singapore, custom runtime, free tier, no coupon."*
2. **(10–20s) Show the Triggers tab** with the `fcapp.run` URL visible. Say:
   *"Here's the public trigger URL Function Compute assigned."*
3. **(20–45s) Switch to the browser tab with that URL open.** Show the
   Foreman console UI loading, then start (or show already-running) a
   checklist run with events streaming into the UI via polling. Say:
   *"This is that same URL in a browser — this is Foreman's actual web
   console, running inside Alibaba Cloud. I'll kick off a run... and you can
   see the planner/executor/verifier events streaming in live — that's a
   real DashScope call from Qwen-max/Qwen3-coder-flash succeeding from
   inside Function Compute, not a static page."*
4. **(45–60s) Close by pointing at the two evidence files** (can be a quick
   cut to your editor/GitHub, doesn't have to be live): `foreman/config.py`
   and `foreman/llm.py`. Say:
   *"Those calls go through these two files — config.py builds the
   OpenAI-compatible client against DashScope's endpoint, and llm.py issues
   the actual chat completion calls. That's the Alibaba Cloud API usage
   powering everything you just saw."*

Save the recording alongside your other submission evidence.

---

## Step 7 — Shut it down (see COST GUARD above)

Immediately after the recording is captured and saved:

1. Function Compute console → your function → **Delete function** (or
   delete the whole `foreman-svc` service), **or**
2. At minimum: Scaling settings → Max Instances → **0**, and remove/disable
   the HTTP trigger.

Do this today, same session as the recording — don't leave a live
`fcapp.run` URL running unattended.

---

## Quick reference

| Setting | Value |
|---|---|
| Region | Singapore (ap-southeast-1) |
| Function type | Web function |
| Runtime | Custom Runtime, Python 3.10+ |
| Zip | `fc/foreman-fc.zip` (vendored `openai`, verify command in Step 1) |
| Entrypoint | `./bootstrap` (or `./fc/bootstrap` if asked explicitly) |
| Port | 9000 |
| `DASHSCOPE_API_KEY` | your key |
| `DASHSCOPE_BASE_URL` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| `FOREMAN_PLANNER_MODEL` | `qwen-max` |
| `FOREMAN_EXECUTOR_MODEL` | `qwen3-coder-flash` |
| `FOREMAN_VERIFIER_MODEL` | `qwen-max` |
| `FOREMAN_FALLBACK_MODELS` | `qwen-turbo,qwen3-max` |
| Max instances | 1 |
| After recording | delete function / disable trigger — see COST GUARD |
