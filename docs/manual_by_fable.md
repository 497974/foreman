# Foreman 使用说明手册

> 一句话:把一张长长的需求清单丢给它,它负责**真的全部做完**——而不是做三件就说"全都搞定了"。

---

## 一、这是什么?

Foreman 是一个**长任务执行编排系统**。你给它一份需求清单(比如"给我的项目加 20 个功能"),它会:

1. **拆卡**(Planner):把清单拆成一个个带验收标准、带可运行测试命令的小任务
2. **干净上下文执行**(Executor):每个任务在独立的上下文里执行——不带上一个任务的包袱,不会越干越糊涂
3. **客观验收**(Verifier):先跑**真实的 pytest 测试门禁**(exit code 说了算),再让模型对照验收标准逐条打分——嘴上说"做完了"没用,测试过了才算
4. **打回重做**:没过就带着具体的失败反馈打回,最多 3 次;执行者不服可以**申辩**,由更强的仲裁模型裁决
5. **全程记账**(Ledger):每一步写进 SQLite 账本 + 事件流,断电、崩溃、额度耗尽,`--resume` 一条命令接着跑

它解决的痛点只有一个:**AI 干长活会偷懒、会谎报完成**。Foreman 用"客观测试门禁 + durable 账本"把这条路堵死。

### 它不是什么(诚实部分)

- ❌ **不是电脑操作 agent**——不能帮你换壁纸、点鼠标、控制其他软件。它的手只有:读写工作区文件 + 在工作区跑 shell 命令
- ❌ **不是沙箱/虚拟机**——命令有危险黑名单拦截(rm -rf /、格式化、git push 等),但这是防事故不防恶意;不放心就在容器里跑
- ❌ **效果依赖模型档位**——Planner 建议用 qwen-max;换太弱的模型(如 qwen-turbo)拆卡会失败(它会诚实报错拒绝,而不是硬跑)
- ❌ 单机 SQLite,不是分布式集群

---

## 二、环境准备(一次性)

```
cd C:\Users\24973\Desktop\Foreman
pip install -r requirements.txt
```

在项目根目录建 `.env` 文件:

```
DASHSCOPE_API_KEY=sk-你的阿里云百炼key
```

可选的模型档位调节(不写就用默认):

```
FOREMAN_PLANNER_MODEL=qwen-max        # 拆卡+仲裁,建议最强档
FOREMAN_EXECUTOR_MODEL=qwen3-coder-plus # 干活主力
FOREMAN_VERIFIER_MODEL=qwen-plus       # 验收
FOREMAN_FALLBACK_MODELS=qwen-plus,qwen-turbo  # 额度烧完自动降档续跑
```

---

## 三、两种玩法

### 玩法 A:从零建新项目(沙箱模式)

写一份需求清单 `reqs.md`(一行一条,写清楚要什么):

```markdown
1. 用 Flask 写一个报销单提交接口 POST /claims,字段:金额、事由、日期
2. 金额超过 1000 需要走审批状态,否则自动通过
3. 写一个 GET /claims 列表接口,支持按状态过滤
...(可以一直列到 20 条)
```

跑:

```bash
python main.py --checklist reqs.md
```

产物在 `runs/run_xxxx/workspace/` 里——完整的代码 + 测试,每条需求都被真实测试验证过。

### 玩法 B:改你现有的项目(existing-project 模式)⭐

```bash
python main.py --checklist reqs.md --project-dir C:\path\to\你的仓库
```

**安全承诺(每一条都有代码强制 + 测试钉死):**

| 承诺 | 机制 |
|---|---|
| 永不碰你的主分支 | 所有提交只落在隔离分支 `foreman/<run_id>` 上 |
| 每个任务一个 checkpoint 提交 | 验收通过才 commit,随时可回退到任意任务节点 |
| 不接手烂摊子 | 脏工作区、mid-merge/rebase 状态、指到子目录 → 直接拒绝并告诉你怎么办 |
| 不撞车 | 同一仓库同时只允许一个 run(`.git/foreman.lock`) |
| 不越权 | `git push`/`reset --hard`/`clean -f`/切分支 等破坏性命令全在黑名单 |
| 合不合并由你决定 | Foreman 永不 merge/rebase/push |

跑完看结果:

```bash
git log foreman/run_xxxx                # 每个任务一个提交
git diff main...foreman/run_xxxx        # 总 diff
git merge foreman/run_xxxx              # 满意就合并(这步永远是你手动做)
```

仓库有未提交改动但你就是想跑:加 `--force-dirty`,你的改动会先被打成一个**单独标注的快照提交**(不会和 AI 写的代码混在一个 commit 里污染 git blame)。

---

## 四、Web 控制台(不想敲命令行)

```bash
python serve.py
```

浏览器打开提示的地址(默认 http://127.0.0.1:8787):

- **New Run** 卡片:贴需求清单 → 可选填"现有项目文件夹" → 开跑
- 实时看:任务状态墙、事件流、**本次 run 花了多少钱**(per-run 成本遥测)
- 右上角切换 中文/English
- 不花钱先试试:勾 **mock/演示模式**,全流程走一遍但不调真模型

---

## 五、断了怎么办(核心卖点)

跑到一半崩了/额度没了/手滑关了窗口:

```bash
python main.py --resume run_xxxx
```

账本里记着每个任务的状态,已完成的不重跑,被卡住(BLOCKED)的复活重试,改现有项目的 run 连 `--project-dir` 都不用再填(它自己从 run 记录里读)。

想主动停:在 `runs/run_xxxx/` 里建一个名为 `STOP` 的空文件,当前任务跑完就停(不是立刻掐断,保证账本一致)。

---

## 六、常见问题

**Q:它说任务失败了 3 次被 BLOCKED,怎么办?**
看 `runs/run_xxxx/events.jsonl` 里的失败原因——通常是需求写得太模糊(拆出的验收标准无法测试)或模型档位不够。改清需求或换强模型后 `--resume`,BLOCKED 任务会复活重试。

**Q:为什么拒绝我的仓库说 "merge in progress"?**
你的仓库有一半的 merge/rebase 没做完。先 `git merge --abort`(或 `--continue` 做完),Foreman 才肯接手——这条 `--force-dirty` 也压不掉,故意的。

**Q:提示 another Foreman run is already using this repo?**
有另一个 run 正在跑这个仓库。等它跑完;如果确定是上次崩溃的残留,按提示删掉 `.git/foreman.lock` 即可。

**Q:跑一半提示额度不足?**
配了 `FOREMAN_FALLBACK_MODELS` 会自动降档续跑;没配就等额度恢复后 `--resume`,一分钱进度都不丢。

---

*本手册对应 2026-07 版本。安全边界的完整威胁模型见 `docs/SECURITY.md`,改现有项目的细节见 `docs/EXISTING_PROJECTS.md`。*
