"""Live connectivity check for Qwen via DashScope. Confirms the key works and
which of our chosen models actually exist on this account, before we build on
them. Prints latency + token usage; never prints the key.

Run:  python demo/check_qwen.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from foreman.config import Settings, make_client

CANDIDATES = ["qwen-plus", "qwen-max", "qwen3-coder-plus", "qwen-turbo", "qwen-flash"]


def main() -> None:
    s = Settings.from_env(os.path.join(os.path.dirname(__file__), "..", ".env"))
    client = make_client(s)
    print(f"endpoint : {s.base_url}")
    print(f"roles    : planner={s.planner_model}  executor={s.executor_model}  "
          f"verifier={s.verifier_model}\n")
    print("probing models (1 tiny call each):\n")

    available = []
    for model in CANDIDATES:
        t0 = time.time()
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with the single word: pong"}],
                max_tokens=16,
                temperature=0,
            )
            dt = time.time() - t0
            text = (resp.choices[0].message.content or "").strip()
            usage = resp.usage
            print(f"  OK   {model:18s} {dt:5.2f}s  in={usage.prompt_tokens} "
                  f"out={usage.completion_tokens}  -> {text!r}")
            available.append(model)
        except Exception as e:
            msg = str(e).splitlines()[0][:90]
            print(f"  FAIL {model:18s} {msg}")

    print(f"\navailable: {available}")
    for role, model in (("planner", s.planner_model), ("executor", s.executor_model),
                        ("verifier", s.verifier_model)):
        flag = "OK" if model in available else "!! NOT AVAILABLE — pick another"
        print(f"  {role:9s} {model:18s} {flag}")


if __name__ == "__main__":
    main()
