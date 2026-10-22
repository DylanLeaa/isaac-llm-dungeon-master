"""Probe OpenRouter models as the DM and write backend/model_whitelist.json (needs a key: Settings or OPENROUTER_API_KEY).

    python tests/probe_models.py [--max-price 0.50] [--budget 3.00] [--only SUBSTR] [--limit N] [--retest] [--dry-run]

Each candidate gets the eight decision calls of tests/live_smoke.py through the app's own OpenRouterDM (same system
prompt, tools, token limit and reasoning fallback), then the same eight again if the first pass was clean. A model is
  ok      every call returned a tool call that clamp() accepts, both passes, and it answered fast enough for prefetch
  slow    valid, but over the latency budget
  fail    a call errored, was cut off, or returned something unusable
  limited the provider rate-limited us: untested, retried next run
--max-price caps the completion price (USD per million tokens); --budget stops the run once that much has been spent.
Results are saved after every model, so an interrupted run resumes where it stopped.
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))
import appdata  # noqa: E402
import model_catalog as mc  # noqa: E402
from dm_agent import LLMCallError, OpenRouterDM  # noqa: E402
from live_smoke import CASES, STATE  # noqa: E402
from memory import RunMemory  # noqa: E402
from schemas import clamp  # noqa: E402

OUT = ROOT / "backend" / "model_whitelist.json"
CRUELTY = 7
MEDIAN_MAX_S, WORST_MAX_S = 10.0, 20.0           # the prefetch lead times the DM has to fit
MIN_CONTEXT = 16_000
SKIP_WORDS = ("image", "audio", "guard", "safety", "embed", "moderat", "router", "lyria", "tts", "-mt", "auto",
              "fusion", "bodybuilder", "switchyard", "summarize", "extract", "apply", "search")


def candidates(text: list, max_price: float, only: str) -> list:
    out = []
    for m in text:
        if (not m.tools or m.id.startswith(("~", "openrouter/")) or m.context < MIN_CONTEXT
                or m.completion is None or not 0 <= m.completion <= max_price
                or m.prompt is None or m.prompt < 0 or any(w in m.id.lower() for w in SKIP_WORDS)
                or (only and only.lower() not in m.id.lower())):
            continue
        out.append(m)
    return sorted(out, key=lambda m: m.id)


async def run_pass(dm: OpenRouterDM, mem: RunMemory, times: list[float]) -> tuple[float, str]:
    """(cost, "") if all eight calls came back usable, else (cost, reason)."""
    cost = 0.0
    for kind, ctx in CASES:
        t0 = time.perf_counter()
        try:
            raw = await dm.decide(kind, STATE, ctx, mem.digest())
            cost += (raw.get("_usage") or {}).get("cost", 0.0)
            clamp(kind, raw, STATE, ctx, CRUELTY, mem.counters)
            times.append(time.perf_counter() - t0)
        except LLMCallError as e:
            cost += ((e.call or {}).get("usage") or {}).get("cost", 0.0)
            return cost, f"{kind}: {str(e)[:120]}"
        except Exception as e:                                       # noqa: BLE001 (any failure is a verdict)
            status = getattr(getattr(e, "response", None), "status_code", None)
            body = getattr(getattr(e, "response", None), "text", "") or ""
            if status == 429:
                return cost, "429"
            return cost, f"{kind}: {type(e).__name__} {status or ''} {body[:140]}".strip()
    return cost, ""


async def probe(m, key: str) -> dict:
    dm = OpenRouterDM(CRUELTY, model=m.id, api_key=key)
    times: list[float] = []
    cost = 0.0
    try:
        for _ in range(2):
            c, why = await run_pass(dm, RunMemory(), times)
            cost += c
            if why:
                break
    finally:
        await dm.client.aclose()
    res = {"cost": round(cost, 5), "calls": len(times), "effort": "low" if "effort" in dm.reasoning else "off",
           "price_out": m.completion}
    if why == "429":
        return {**res, "status": "limited", "reason": "rate limited"}
    if why:
        return {**res, "status": "fail", "reason": why}
    med, worst = statistics.median(times), max(times)
    res.update(median_s=round(med, 1), max_s=round(worst, 1))
    if med > MEDIAN_MAX_S or worst > WORST_MAX_S:
        return {**res, "status": "slow", "reason": f"median {med:.1f}s, worst {worst:.1f}s"}
    return {**res, "status": "ok", "reason": ""}


def load() -> dict:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"models": {}}


def save(data: dict) -> None:
    data["updated"] = date.today().isoformat()
    tmp = OUT.with_name(OUT.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(OUT)


async def main(args) -> None:
    key = appdata.api_key(json.loads((appdata.data_dir() / "config.json").read_text(encoding="utf-8")))
    if not key and not args.dry_run:
        sys.exit("no OpenRouter key in Settings or OPENROUTER_API_KEY")
    catalog = mc.parse(mc.fetch())
    todo = candidates(catalog["text"], args.max_price, args.only)
    data = load()
    done = data["models"]
    todo = [m for m in todo if args.retest or done.get(m.id, {}).get("dm", {}).get("status") in (None, "limited")]
    if args.limit:
        todo = todo[:args.limit]
    print(f"{len(todo)} models to probe (completion <= ${args.max_price}/M, budget ${args.budget:.2f})")
    if args.dry_run:
        for m in todo:
            print(f"  {m.id:55} ${m.completion}")
        return
    spent, sem, lock = 0.0, asyncio.Semaphore(args.parallel), asyncio.Lock()

    async def one(i: int, m) -> None:
        nonlocal spent
        async with sem:
            if spent >= args.budget:
                return
            res = await probe(m, key)
            async with lock:
                spent += res["cost"]
                done.setdefault(m.id, {"name": m.name})["dm"] = {**res, "tested": date.today().isoformat()}
                save(data)
                print(f"[{i + 1}/{len(todo)}] {m.id:55} {res['status']:8} "
                      f"{res.get('median_s', '-')!s:>5}s  ${res['cost']:.4f}  {res['reason']}", flush=True)

    await asyncio.gather(*(one(i, m) for i, m in enumerate(todo)))
    tally: dict[str, int] = {}
    for v in done.values():
        s = v.get("dm", {}).get("status", "untested")
        tally[s] = tally.get(s, 0) + 1
    print(f"\nspent ${spent:.2f}; whitelist {tally}; written to {OUT}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-price", type=float, default=0.50, help="max completion price, USD per million tokens")
    ap.add_argument("--budget", type=float, default=3.00, help="stop after spending this much, USD")
    ap.add_argument("--only", default="", help="only model ids containing this text")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--retest", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    asyncio.run(main(ap.parse_args()))
