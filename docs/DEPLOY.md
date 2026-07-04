# Deploying Foreman to Alibaba Cloud Function Compute (FC)

*本文档为中英双语，每一节先英文后中文 / This guide is bilingual: each section is
English followed by 中文.*

This guide gets the Foreman web console (`serve.py`) running as a public URL
on Alibaba Cloud Function Compute (FC) 3.0, using its **custom runtime**
feature (a zip file + a `bootstrap` script — no Docker required). It is
written for someone who has never used FC before and is doing everything
through the web console (no CLI/SDK).

本指南将帮助你把 Foreman 的网页控制台（`serve.py`）部署到阿里云函数计算
（FC）3.0，使用它的**自定义运行时**功能（一个 zip 包 + 一个 `bootstrap`
启动脚本，不需要 Docker）。本指南面向从未用过 FC 的用户，全程通过网页控制台
操作（不需要命令行 SDK）。

---

## 0. What you need before you start / 开始前需要准备

- An **Alibaba Cloud International** account, with Function Compute enabled,
  in the **Singapore (ap-southeast-1)** region — the same region as the
  DashScope endpoint Foreman calls, which keeps latency low and avoids any
  cross-region data questions.
  一个已开通函数计算服务的**阿里云国际版**账号，区域选择**新加坡
  (ap-southeast-1)** —— 这与 Foreman 调用的 DashScope 服务同区域，延迟更低，
  也避免了跨区域的问题。
- Your `DASHSCOPE_API_KEY` (the same one that's normally in your local
  `.env` file). **Never put this key in the zip file** — it is set as an FC
  environment variable instead (step 4).
  你的 `DASHSCOPE_API_KEY`（就是平时写在本地 `.env` 文件里的那个）。
  **绝对不要把这个 key 打包进 zip 文件** —— 而是作为 FC 的环境变量单独设置
  （见第 4 步）。
- The deployment zip, built locally (step 1).
  本地构建好的部署 zip 包（见第 1 步）。

---

## 1. Build the deployment package locally / 本地构建部署包

From the repo root:

在仓库根目录下执行：

```bash
python fc/build_package.py
```

This writes `fc/foreman-fc.zip`, containing `foreman/`, `serve.py`,
`main.py`, `requirements.txt`, `demo/`, and `fc/bootstrap`. It deliberately
**excludes** `runs/`, `evals/`, `tests/`, `.git/`, and `.env` — see the
docstring in `fc/build_package.py` for the full rationale (short version: the
API key must never be shipped in a file that ends up in a cloud console's
upload history).

这条命令会生成 `fc/foreman-fc.zip`，包含 `foreman/`、`serve.py`、`main.py`、
`requirements.txt`、`demo/` 和 `fc/bootstrap`。它**特意排除**了 `runs/`、
`evals/`、`tests/`、`.git/` 和 `.env` —— 详细原因见 `fc/build_package.py`
的文件说明（简单说：API key 绝不能被打包进一个会留在云控制台上传历史里的文件）。

**Pre-flight check** — confirm the zip was built and looks right before
uploading anything:

**上传前自检** —— 在上传之前确认 zip 包构建成功且内容正确：

```bash
python fc/build_package.py && python -c "import zipfile; print(zipfile.ZipFile('fc/foreman-fc.zip').namelist()[:10])"
```

You should see `serve.py`, `main.py`, `requirements.txt`, and `foreman/...`
entries in the printed list, and **no** `.env`, `runs/`, or `tests/` entries.

你应该能在打印出的列表里看到 `serve.py`、`main.py`、`requirements.txt` 和
`foreman/...` 这些条目，并且**不应该**看到 `.env`、`runs/` 或 `tests/`。

### Getting the `openai` package onto FC / 让 FC 上能用 `openai` 包

Foreman's only runtime dependency is `openai` (see `requirements.txt`). A
custom runtime does **not** auto-run `pip install` for you, so pick one of
two options:

Foreman 唯一的运行时依赖是 `openai`（见 `requirements.txt`）。自定义运行时
**不会**自动帮你 `pip install`，所以需要在下面两个方案里选一个：

- **Option 1 — console-side install (no local step)**: after uploading the
  zip (step 2), use the FC console's function → "Layers" or
  "Install dependencies" feature (under Code / Configuration, the exact
  label has moved around between console versions — look for anything
  mentioning "layer" or "pip") and point it at `requirements.txt`, or add
  the public `openai` layer if one is listed. This is one extra console
  step but keeps the zip itself smaller.
  **方案一 —— 控制台安装（本地无需额外步骤）**：上传 zip 后（第 2 步），
  在 FC 控制台的函数页面里找 "层 (Layers)" 或 "安装依赖" 功能（在代码/配置
  区域，不同控制台版本里位置和名字可能不完全一样，找带有 "layer" 或 "pip"
  字样的入口），指向 `requirements.txt`，或者如果列表里有官方 `openai`
  层，直接添加它。这样只是多一步控制台操作，但 zip 包本身更小。

- **Option 2 — vendor it into the zip (recommended for a first deploy,
  fewer moving parts)**: run this locally *before* building the zip, then
  rebuild:
  **方案二 —— 把依赖打包进 zip（推荐首次部署使用，环节更少更不容易出错）**：
  在构建 zip **之前**先在本地执行下面命令，然后重新构建：

  ```bash
  pip install openai -t fc/vendor
  python fc/build_package.py
  ```

  `fc/build_package.py` automatically detects `fc/vendor/` and includes it
  in the zip; `fc/bootstrap` automatically prepends it to `PYTHONPATH` at
  boot. Nothing else to configure. The zip will be larger (openai + its
  transitive deps), which is fine for FC's package size limits.

  `fc/build_package.py` 会自动检测 `fc/vendor/` 目录并把它打包进 zip；
  `fc/bootstrap` 启动时会自动把这个目录加到 `PYTHONPATH` 前面。不需要额外
  配置。zip 包会变大（包含 openai 及其依赖），这在 FC 的包大小限制内完全没问题。

---

## 2. Create the FC function in the console / 在控制台创建 FC 函数

*(Flagging for verification: exact menu wording/order below is based on FC
3.0's documented web-function + custom-runtime flow as of this writing; if
your console shows slightly different labels, look for the concepts, not the
literal strings — "Web function" + "Custom Runtime" is the pairing that
matters.)*

（**需要人工核实**：以下菜单文字/顺序是基于撰写时 FC 3.0 文档中"Web 函数 +
自定义运行时"流程写的；如果你的控制台文字略有不同，请按概念而不是字面文字去找
—— 关键是选中"Web 函数 (Web function)" + "自定义运行时 (Custom Runtime)"
这个组合。）

1. Go to **Function Compute** console → make sure the region selector
   (top of the page) is set to **Singapore**.
   进入**函数计算**控制台 → 确认页面顶部的地域选择器为**新加坡**。
2. **Create Service** (if you don't already have one) — any name, e.g.
   `foreman-svc`.
   **创建服务**（如果还没有的话）—— 名字随意，例如 `foreman-svc`。
3. **Create Function** → choose **Web function (Web 函数)** as the function
   type (this is the type that expects an HTTP server listening on a port,
   as opposed to the older "event function" type).
   **创建函数** → 函数类型选择**Web 函数**（这种类型期望有一个监听端口的
   HTTP 服务器，区别于旧版的"事件函数"类型）。
4. **Runtime**: choose **Custom Runtime**, and pick a **Python 3.10+**
   base image from the custom-runtime image options (FC ships a family of
   base images per language; pick the Python one closest to 3.10/3.11 so the
   `python3` on PATH matches what `foreman/` was tested against).
   **运行时**：选择**自定义运行时 (Custom Runtime)**，并从自定义运行时的
   基础镜像选项里选一个 **Python 3.10 及以上**版本的镜像（FC 为每种语言提供
   一系列基础镜像；选一个最接近 3.10/3.11 的 Python 镜像，让 PATH 里的
   `python3` 版本与 `foreman/` 实际测试过的版本一致）。
5. **Code upload**: choose "upload zip" and select `fc/foreman-fc.zip` from
   step 1.
   **代码上传**：选择"上传 zip 包"，选中第 1 步生成的 `fc/foreman-fc.zip`。
6. **Startup command / entrypoint**: FC's custom runtime looks for
   `bootstrap` at the code root by convention; the box may be pre-filled
   with `./bootstrap` — leave it as-is (our `fc/bootstrap` was zipped at the
   path `fc/bootstrap`, i.e. `/code/fc/bootstrap` once unzipped; if your
   console asks for an explicit path rather than the convention, enter
   `./fc/bootstrap`).
   **启动命令 / 入口**：FC 自定义运行时按约定会在代码根目录寻找
   `bootstrap` 文件；这个输入框可能已经预填了 `./bootstrap` —— 保持不变即可
   （我们的 `fc/bootstrap` 在 zip 里的路径是 `fc/bootstrap`，解压后即
   `/code/fc/bootstrap`；如果控制台要求你显式填路径而不是用默认约定，填
   `./fc/bootstrap`）。
7. **Port**: set to **9000** (this is what `fc/bootstrap` binds via
   `serve.py --port 9000`; FC's gateway proxies external HTTP traffic to
   this port inside the instance).
   **端口**：设置为 **9000**（这正是 `fc/bootstrap` 通过
   `serve.py --port 9000` 绑定的端口；FC 网关会把外部 HTTP 流量代理到实例内的
   这个端口）。

---

## 3. Set the environment variables / 设置环境变量

In the function's **Configuration → Environment Variables** section, add:

在函数的**配置 → 环境变量**部分，添加：

| Key | Value | Notes / 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | `sk-...` (your key) | Required. Never commit this to git or put it in the zip. 必填。绝不要提交到 git 或打包进 zip。 |
| `DASHSCOPE_BASE_URL` | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` | Matches `foreman/config.py`'s default for the International account; set explicitly so it's visible in the console. 与 `foreman/config.py` 中国际版账号的默认值一致；显式设置以便在控制台中可见。 |
| `FOREMAN_EXECUTOR_MODEL` | `qwen3-coder-plus` (or your preferred override) | Optional — only needed if you want a different model than the code default. 可选 —— 仅当你想覆盖代码默认模型时才需要设置。 |
| `FOREMAN_PLANNER_MODEL` / `FOREMAN_VERIFIER_MODEL` | (optional) | Same idea, for the other two roles, see `foreman/config.py`. 同理，用于另外两个角色，见 `foreman/config.py`。 |

These map directly to the env vars `foreman/config.py`'s `Settings.from_env`
reads via `os.environ` — FC injects console-configured environment variables
into the process before `bootstrap` runs, so no `.env` file is needed on FC
at all.

这些变量与 `foreman/config.py` 里 `Settings.from_env` 通过 `os.environ`
读取的环境变量一一对应 —— FC 会在 `bootstrap` 运行之前把控制台配置的环境变量
注入到进程里，所以在 FC 上完全不需要 `.env` 文件。

---

## 4. Timeouts, the 600-second gateway limit, and what that means for runs / 超时、600 秒网关限制及其影响

*(Flagging for verification: the exact timeout field name/max value in the
console may differ slightly by console version — the concept below is
correct, but confirm the number against your account's current console.)*

（**需要人工核实**：控制台里超时字段的具体名字/最大值可能因版本略有不同 ——
下面的概念是对的，但请对照你账号当前控制台上的实际数字确认。）

- Set the function's **request timeout** to the maximum your console
  allows (FC web functions typically allow a long configurable timeout for
  the function itself). However — **FC's gateway independently drops any
  idle synchronous HTTP connection after 600 seconds**, regardless of the
  function timeout setting. This is a platform-level limit on the
  connection, not something `serve.py` or the function config can override.
  将函数的**请求超时**设为控制台允许的最大值（FC Web 函数通常允许为函数本身
  配置较长的超时）。但是 —— **无论函数超时设置为多少，FC 网关会独立地在
  600 秒后断开任何空闲的同步 HTTP 连接**。这是平台层面对连接本身的限制，
  `serve.py` 或函数配置都无法覆盖。
- **What this means for Foreman specifically**: a full checklist run
  (`POST /api/runs`) can easily take longer than 600 seconds. Foreman's web
  console is already built for this — starting a run returns immediately
  with a `run_id` (see `serve.py`'s `_start_run_in_background`, which kicks
  the real orchestrator off on a background thread and returns right away),
  and progress is read back via polling `GET /api/runs/<id>/events`. As
  long as you **use the polling UI/API instead of one long-held request**,
  the 600s idle-connection limit never applies — each poll is its own
  short-lived request.
  **这对 Foreman 具体意味着什么**：完整跑一遍 checklist（`POST /api/runs`）
  很容易超过 600 秒。Foreman 的网页控制台本身就是这么设计的 —— 启动一次
  run 会立即返回一个 `run_id`（见 `serve.py` 的
  `_start_run_in_background`，它把真正的编排逻辑丢到后台线程执行，然后马上
  返回），后续进度通过轮询 `GET /api/runs/<id>/events` 读取。只要你**使用
  轮询式的界面/接口，而不是发起一个长期占用的单一请求**，600 秒空闲连接限制
  就不会生效 —— 每次轮询都是一个独立的短请求。
- **Session-affinity caveat**: FC may route different requests to different
  instances (especially if the function scales to multiple instances under
  load). Foreman's run state (`runs/<run_id>/ledger.db`, `events.jsonl`) is
  written to local disk (`/tmp` inside the instance, since FC's writable
  filesystem is ephemeral and instance-local — see next bullet). If a poll
  for `GET /api/runs/<id>/events` lands on a *different* instance than the
  one that started the run, it will 404 with "unknown run". **For this demo
  deployment, keep concurrency/max-instances at 1** in the function's scaling
  settings so every request — start and all subsequent polls — hits the same
  instance and therefore the same `runs/` directory. This is a real
  limitation and is intentionally left as-is for a hackathon demo rather
  than solved with an external database.
  **会话亲和性说明**：FC 可能把不同请求路由到不同实例上（尤其是负载较高、
  函数扩容出多个实例时）。Foreman 的 run 状态（`runs/<run_id>/ledger.db`、
  `events.jsonl`）写在本地磁盘上（实例内的 `/tmp`，因为 FC 的可写文件系统是
  临时性且仅限当前实例的 —— 见下一条）。如果轮询 `GET
  /api/runs/<id>/events` 落到了启动该 run 的**另一个**实例上，就会返回
  404 "unknown run"。**对于这次演示部署，把函数的扩缩容设置中并发/最大实例数
  设为 1**，这样从启动到之后所有的轮询都会落在同一个实例、同一个 `runs/`
  目录上。这是一个真实存在的局限，出于黑客松演示的目的有意保留，而不是引入
  外部数据库去解决。
- **Ephemeral `/tmp` caveat**: FC's writable local disk is per-instance and
  not persisted across instance recycling/cold starts. This means a run's
  `runs/<run_id>/` state (ledger + events + workspace files) can disappear
  if the instance is recycled mid-demo. This is acceptable and documented
  here as a known limitation of this deployment path — for anything beyond
  a demo, the ledger backend would need to move to a real managed store
  (e.g. an OSS bucket or a managed database), which is out of scope for
  this hackathon submission.
  **`/tmp` 临时性说明**：FC 可写的本地磁盘是每个实例独立的，实例被回收/冷启动
  后不会保留。这意味着如果实例在演示过程中被回收，某次 run 的
  `runs/<run_id>/` 状态（ledger、events、workspace 文件）可能会丢失。这是
  这种部署方式的已知局限，此处已如实记录并可接受 —— 如果要用于演示之外的场景，
  ledger 后端需要换成真正的托管存储（例如 OSS 或托管数据库），这超出了本次
  黑客松提交的范围。

---

## 5. Deploy and get the public URL / 部署并获取公网访问地址

1. Save/deploy the function.
   保存/部署函数。
2. The FC console shows a **public endpoint / trigger URL** for the web
   function (usually under the function's "Triggers" tab, listed as an
   `https://<...>.fcapp.run`-style address for the default HTTP trigger that
   web functions get automatically).
   FC 控制台会为该 Web 函数显示一个**公网访问地址 / 触发器 URL**（通常在函数
   的"触发器"标签页下，是类似 `https://<...>.fcapp.run` 这样的地址，Web
   函数会自动获得一个默认的 HTTP 触发器）。
3. Open that URL in a browser — you should see the same Foreman web console
   UI you see locally at `http://127.0.0.1:8787`. Submitting a small
   checklist should start a run and the events should start streaming in via
   the polling UI, proving the DashScope call is actually reaching Alibaba
   Cloud's Singapore endpoint from within FC.
   在浏览器打开这个 URL —— 你应该会看到和本地 `http://127.0.0.1:8787`
   一样的 Foreman 网页控制台界面。提交一个小的 checklist 应该能启动一次 run，
   并通过轮询界面开始看到事件流，这证明了 DashScope 调用确实是从 FC 内部
   打到了阿里云新加坡的服务端点。

---

## 6. Capturing the two proof artifacts for the hackathon submission / 为黑客松提交采集两项证明材料

The submission requires "Proof of Alibaba Cloud Deployment": the backend
genuinely running on Alibaba Cloud, plus a repo file demonstrating Alibaba
Cloud API usage.

提交要求提供"阿里云部署证明"：后端确实运行在阿里云上，以及一个能证明使用了
阿里云 API 的仓库代码文件。

1. **Live-deployment proof**: take a screenshot (or short screen recording)
   showing (a) the FC console with the function's configuration (region =
   Singapore, custom runtime, the `fcapp.run` URL visible), and (b) that
   same URL open in a browser tab, showing the Foreman console UI actually
   responding. If you can, also capture a run's events streaming in live —
   that demonstrates the DashScope call succeeding from inside FC, not just
   a static page load.
   **实时部署证明**：截图（或简短录屏）展示 (a) FC 控制台上该函数的配置
   （地域 = 新加坡，自定义运行时，可见 `fcapp.run` 地址），以及 (b) 在浏览器
   标签页中打开同一个地址，展示 Foreman 控制台界面确实在响应。如果可以，
   最好再录一段某次 run 的事件实时流动的画面 —— 这能证明 DashScope 调用
   确实是从 FC 内部成功发起的，而不只是一个静态页面加载。
2. **Alibaba Cloud API usage code file**: point the submission at
   `foreman/config.py` (builds the OpenAI-compatible client pointed at the
   DashScope `base_url`, i.e. Alibaba Cloud's Model Studio API) and
   `foreman/llm.py` (the shared `chat_json` helper that actually issues the
   `client.chat.completions.create(...)` calls against that DashScope
   endpoint). Together these two files are the concrete evidence of Alibaba
   Cloud (DashScope/Model Studio) API usage; `foreman/planner.py`,
   `foreman/executor.py`, `foreman/verifier.py`, and `foreman/arbiter.py` are
   the callers if the reviewer wants to see it used in context.
   **阿里云 API 使用代码文件**：把提交材料指向 `foreman/config.py`
   （构建指向 DashScope `base_url` 即阿里云百炼 Model Studio API 的
   OpenAI 兼容客户端）和 `foreman/llm.py`（真正发起
   `client.chat.completions.create(...)` 调用、对接该 DashScope 端点的共用
   helper `chat_json`）。这两个文件合在一起就是使用阿里云
   （DashScope/百炼）API 的具体证据；如果评审想看到实际调用场景，
   `foreman/planner.py`、`foreman/executor.py`、`foreman/verifier.py` 和
   `foreman/arbiter.py` 是这些函数的调用方。

---

## Quick reference / 速查

| Setting / 设置项 | Value / 值 |
|---|---|
| Region / 地域 | Singapore / 新加坡 (ap-southeast-1) |
| Function type / 函数类型 | Web function / Web 函数 |
| Runtime / 运行时 | Custom Runtime, Python 3.10+ base image |
| Entrypoint / 启动脚本 | `fc/bootstrap` (zipped at that path; FC convention looks for `bootstrap` at code root) |
| Port / 端口 | 9000 |
| Env vars / 环境变量 | `DASHSCOPE_API_KEY`, `DASHSCOPE_BASE_URL`, optional `FOREMAN_*_MODEL` |
| Max instances / 最大实例数 | 1 (session affinity for `runs/` state — see §4) |
| Gateway idle limit / 网关空闲限制 | 600s per sync connection — use the polling API, not one long request |
