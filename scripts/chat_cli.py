"""命令行试用「对话改稿」：不开网页也能把整条链跑通。

    python scripts/chat_cli.py --file samples/txt/05-产品经理-本科.txt --job 产品经理 \
        --message "把实习那条改成有结果的写法" --apply all

做的事：建档（v1）→ 给 v1 打分（可选）→ 聊一句拿到补丁 → 采用 → 重评 → 打印前后差。
链路通了，界面才有意义；反过来先把界面做出来，等于在没验证的地基上盖楼。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chat_engine  # noqa: E402
import review as review_mod  # noqa: E402
import rubric  # noqa: E402
import run_eval  # noqa: E402
import version_store  # noqa: E402

CATEGORY_LABEL = {"content": "内容", "layout": "排版"}
DELTA_LABEL = {"none": "不动事实", "wording": "措辞", "number_added": "加了数字",
               "ownership_upgrade": "权属升级", "scope_change": "改了范围"}


def _config(args):
    # CLI 是本机开发者自用，允许读 .env 里自己的 key；网页端不走这条路
    # （网页端只用访客填的 key，服务器 key 路径已在 v1.9.1 删掉）。
    key = (args.api_key or os.environ.get("DSH_LLM_API_KEY") or "").strip()
    if not key:
        print("[error] 对话需要模型 key：传 --api-key，或在 .env 里配 DSH_LLM_API_KEY。")
        print("        没有 key 时网页会用本地规则引擎打分，但规则引擎不会聊天。")
        raise SystemExit(2)
    return run_eval.config_from_request(key, args.base_url or "", args.model or "", args.timeout or 0)


def _baseline(store, doc_id, args, cfg) -> dict | None:
    """给最新版打一次分。没有基准分，后面那个"+12"就没有意义。"""
    rec = store.latest(doc_id)
    old = store.load_review(doc_id, rec["version"])
    if old:
        return old
    if args.no_baseline:
        return None
    print(f"[基线] 正在给 v{rec['version']} 打分（约 30~60 秒）…")
    prompt = run_eval.build_prompt(rec["text"], {"job_target": args.job or "实习岗位", "jd": args.jd or ""},
                                  version=args.prompt)
    doc, scores, quality = review_mod.run_reviews(prompt, [], n=1, resume=rec["text"], config=cfg)
    store.save_review(doc_id, rec["version"], doc)
    store.update_meta(doc_id, rec["version"], score=doc.get("overall_score"),
                      verdict=doc.get("verdict"), review_status="ok" if quality.get("eligible") else "rejected")
    print(f"[基线] v{rec['version']} = {doc.get('overall_score')} 分（{doc.get('verdict')}）")
    return doc


def _print_patches(patches: list[dict]) -> None:
    if not patches:
        print("（这一轮没有补丁）")
        return
    for p in patches:
        tag = CATEGORY_LABEL.get(p["category"], p["category"])
        print(f"\n  [{p['id']}] {tag} / {DELTA_LABEL.get(p['fact_delta'], p['fact_delta'])}"
              f"{' / 需确认' if p['needs_user_confirm'] else ''}")
        print(f"      原文：{p['quote']}")
        print(f"      新文：{p['new_text'] or '（删除这段）'}")
        if p.get("why"):
            print(f"      理由：{p['why']}")
        for issue in p.get("issues") or []:
            print(f"      ⚠ {issue['message']}")


def main() -> int:
    ap = argparse.ArgumentParser(description="对话改稿 CLI（自带 key 调用自己的模型）")
    ap.add_argument("--file", required=True, help="简历 txt/md 路径")
    ap.add_argument("--job", default="", help="目标岗位，如 产品经理")
    ap.add_argument("--jd", default="", help="岗位 JD（可选）")
    ap.add_argument("--message", default="", help="要发给模型的一句话")
    ap.add_argument("--apply", default="", help="采用哪些补丁：all 或 p1,p3")
    ap.add_argument("--fills", action="append", default=[],
                    help='填占位符，形如 "«待补：指标»=4"，可多次')
    ap.add_argument("--undo", action="store_true", help="撤回最新一版")
    ap.add_argument("--api-key", default="", help="访客自己的 key；不传则读 .env 的 DSH_LLM_API_KEY")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--timeout", type=int, default=0)
    ap.add_argument("--prompt", default=run_eval.DEFAULT_PROMPT_VERSION, help="重评用的 prompt 版本")
    ap.add_argument("--no-baseline", action="store_true", help="不给 v1 打基线分")
    ap.add_argument("--store", default="", help="版本库目录（默认 reports/versions）")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    store = version_store.VersionStore(Path(args.store) if args.store else None)
    rec = store.create(text, source="upload", meta={"file": Path(args.file).name, "job": args.job})
    doc_id = rec["doc_id"]
    print(f"[建档] doc_id={doc_id}  v{rec['version']}  {rec['chars']} 字")

    if args.undo:
        back = store.undo(doc_id)
        print(f"[撤回] 回到 v{back['version']}（{back.get('score')} 分）")
        return 0

    if not args.message:
        for row in store.chain_summary(doc_id):
            print(f"  v{row['version']}  {row.get('score')} 分  {row.get('source')}  "
                  f"{row.get('applied') or ''}")
        print("\n（没给 --message，只显示了版本链）")
        return 0

    cfg = _config(args)
    _baseline(store, doc_id, args, cfg)

    session = chat_engine.ChatSession(
        store, doc_id, job=args.job, jd=args.jd, config=cfg, prompt_version=args.prompt
    )
    print("\n[对话] 发送中…")
    sent = session.send(args.message)
    if not sent.get("ok"):
        print(f"[失败] {sent.get('message')}")
        if sent.get("detail") and not args.quiet:
            print(f"         模型原文：{sent['detail']}")
        return 1

    print(f"\n[回复] {sent['reply']}")
    _print_patches(sent["patches"])
    for prob in sent.get("problems") or []:
        print(f"  · {prob}")

    if not args.apply:
        print("\n（没给 --apply，补丁没有写入。用 --apply all 或 --apply p1,p3 再跑一次）")
        return 0

    ids = [] if args.apply.lower() == "all" else [x.strip() for x in args.apply.split(",") if x.strip()]
    fills = {}
    for raw in args.fills:
        if "=" not in raw:
            print(f"[warn] --fills 写法不对，已忽略：{raw}")
            continue
        k, v = raw.split("=", 1)
        fills[k.strip()] = v.strip()

    out = session.apply(ids, fills)
    if not out.get("ok"):
        print(f"\n[未写入] {out.get('message')}")
        if out.get("blocked"):
            for b in out["blocked"]:
                print(f"  ⚠ [{b['id']}] " + "；".join(r["message"] for r in b["reasons"]))
        return 1

    print(f"\n[写入] v{out['version']}（基于 v{out['parent']}）— {out['summary']}")
    left = patch_protocol.PLACEHOLDER_RE.findall(out.get("text") or "")
    if left:
        print(f"[注意] 这版还有 {len(left)} 个占位符没填（如 {left[0]}）。"
              "带占位符去重评会被当成排版噪音，分数偏低——填完再评一次更准。")
    rev = out.get("review") or {}
    if rev.get("ok"):
        print(f"[重评] {rev.get('score')} 分（{rev.get('verdict')}）")
    else:
        print(f"[重评] 未通过：{rev.get('message')} — {'；'.join(rev.get('reasons') or [])}")
    cmp_ = out.get("compare") or {}
    score = (cmp_.get("score") or {})
    if score.get("delta") is not None:
        print(f"[对比] {score.get('before')} → {score.get('after')}（{score['delta']:+d} 分）")
    if out.get("advice"):
        print(f"[建议] {out['advice']}")
    if out.get("applied_status"):
        states = {}
        for row in out["applied_status"]:
            states[row["state"]] = states.get(row["state"], 0) + 1
        print(f"[落实] 上一轮建议 {len(out['applied_status'])} 条："
              + "，".join(f"{k} {v}" for k, v in states.items()))

    print("\n[版本链]")
    for row in store.chain_summary(doc_id):
        print(f"  v{row['version']}  {row.get('score')} 分  {row.get('source')}  "
              f"{row.get('applied') or ''}")
    if not args.quiet:
        print(f"\n[提示] 版本库在 {store.root}（公网部署时这里是临时目录，重启即丢，务必下载 .txt）")
        print(f"[提示] rubric {rubric.get('version')} / prompt {args.prompt}（改文字不改标准）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
