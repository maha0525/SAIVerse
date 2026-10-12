"""Retrieval / STS accuracy benchmark on JGLUE (JSQuAD valid, JSTS valid).

Usage (from the worktree root):
    .venv-exp\\Scripts\\python.exe -X utf8 -I experiments\\embeddinggemma2_bench\\bench_retrieval.py

Data (downloaded from github.com/yahoojapan/JGLUE, main branch):
    data/jsquad-valid-v1.3.json  <- datasets/jsquad-v1.3/valid-v1.3.json
    data/jsts-valid-v1.3.json    <- datasets/jsts-v1.3/valid-v1.3.json

JSQuAD retrieval: every unique paragraph context is a document; every
answerable question is a query; documents are ranked by cosine similarity.
Context strings are "title [SEP] body"; they are fed as "title\\nbody".

Writes results/retrieval.json and prints markdown tables.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import common  # noqa: E402

common.prepare_env()

import numpy as np  # noqa: E402

JSQUAD_PATH = common.DATA_DIR / "jsquad-valid-v1.3.json"
JSTS_PATH = common.DATA_DIR / "jsts-valid-v1.3.json"
GEMMA_TRUNC_DIMS = (768, 512, 256)


def load_jsquad(max_questions: int | None, seed: int):
    data = json.loads(JSQUAD_PATH.read_text(encoding="utf-8"))
    doc_index: dict[str, int] = {}
    docs: list[str] = []
    queries: list[str] = []
    gold: list[int] = []
    skipped_impossible = 0
    for art in data["data"]:
        for para in art["paragraphs"]:
            ctx = para["context"]
            if ctx not in doc_index:
                doc_index[ctx] = len(docs)
                docs.append(ctx.replace(" [SEP] ", "\n", 1))
            for qa in para["qas"]:
                if qa.get("is_impossible"):
                    skipped_impossible += 1
                    continue
                queries.append(qa["question"])
                gold.append(doc_index[ctx])
    n_total = len(queries)
    if max_questions and n_total > max_questions:
        rng = np.random.default_rng(seed)
        idx = np.sort(rng.choice(n_total, size=max_questions, replace=False))
        queries = [queries[i] for i in idx]
        gold = [gold[i] for i in idx]
    meta = {
        "n_docs": len(docs),
        "n_questions_total": n_total,
        "n_questions_used": len(queries),
        "skipped_impossible": skipped_impossible,
        "doc_chars_median": float(np.median([len(d) for d in docs])),
        "doc_chars_max": int(max(len(d) for d in docs)),
        "query_chars_median": float(np.median([len(q) for q in queries])),
    }
    return docs, queries, np.array(gold), meta


def load_jsts():
    pairs = []
    for line in JSTS_PATH.read_text(encoding="utf-8").splitlines():
        if line.strip():
            o = json.loads(line)
            pairs.append((o["sentence1"], o["sentence2"], float(o["label"])))
    return pairs


def dist_stats(x: np.ndarray) -> dict:
    return {
        "mean": float(x.mean()),
        "p10": float(np.percentile(x, 10)),
        "p50": float(np.percentile(x, 50)),
        "p90": float(np.percentile(x, 90)),
    }


def retrieval_metrics(q: np.ndarray, d: np.ndarray, gold: np.ndarray) -> dict:
    sims = q @ d.T
    rows = np.arange(len(gold))
    gold_sim = sims[rows, gold]
    rank = (sims > gold_sim[:, None]).sum(axis=1) + 1
    masked = sims.copy()
    masked[rows, gold] = -np.inf
    best_other = masked.max(axis=1)
    return {
        "recall@1": float((rank <= 1).mean()),
        "recall@5": float((rank <= 5).mean()),
        "recall@10": float((rank <= 10).mean()),
        "mrr@10": float(np.where(rank <= 10, 1.0 / rank, 0.0).mean()),
        "gold_sim": dist_stats(gold_sim),
        "best_nongold_sim": dist_stats(best_other),
        "margin_gold_minus_best_other": dist_stats(gold_sim - best_other),
    }


def sts_metrics(e1: np.ndarray, e2: np.ndarray, labels: np.ndarray) -> dict:
    from scipy.stats import pearsonr, spearmanr

    cos = (e1 * e2).sum(axis=1)
    return {
        "spearman": float(spearmanr(cos, labels).statistic),
        "pearson": float(pearsonr(cos, labels).statistic),
        "cos": dist_stats(cos),
    }


def count_over_512_tokens(key: str, model, docs: list[str]) -> dict:
    """Count JSQuAD documents whose tokenised length (with prefix) exceeds 512."""
    try:
        if key == "e5":
            from tokenizers import Tokenizer

            tok = Tokenizer.from_file(str(common.E5_LOCAL_PATH / "tokenizer.json"))
            lens = [len(e.ids) for e in tok.encode_batch(["passage: " + d for d in docs])]
            limit = 512  # fastembed default max_length for this model
        else:
            prompt = model.model.prompts["Document"]
            tk = model.model.tokenizer
            lens = [len(tk(prompt + d)["input_ids"]) for d in docs]
            limit = 512
        lens_a = np.array(lens)
        return {
            "limit": limit,
            "n_over": int((lens_a > limit).sum()),
            "tokens_median": float(np.median(lens_a)),
            "tokens_max": int(lens_a.max()),
        }
    except Exception as exc:  # informational only
        return {"error": repr(exc)}


def run_model(key: str, docs, queries, gold, pairs) -> dict:
    out: dict = {"model": key}
    t = time.perf_counter()
    model = common.load_model(key)
    out["load_s"] = time.perf_counter() - t
    out["info"] = model.info()
    out["doc_token_stats"] = count_over_512_tokens(key, model, docs)

    print(f"[{key}] encoding {len(docs)} documents ...", flush=True)
    t = time.perf_counter()
    d_emb = model.encode_documents(docs)
    out["encode_docs_s"] = time.perf_counter() - t
    print(f"[{key}] encoding {len(queries)} queries ...", flush=True)
    t = time.perf_counter()
    q_emb = model.encode_queries(queries)
    out["encode_queries_s"] = time.perf_counter() - t

    jsq = {}
    if key == "gemma":
        for dim in GEMMA_TRUNC_DIMS:
            jsq[str(dim)] = retrieval_metrics(
                common.truncate_renorm(q_emb, dim), common.truncate_renorm(d_emb, dim), gold
            )
    else:
        jsq[str(d_emb.shape[1])] = retrieval_metrics(q_emb, d_emb, gold)
    out["jsquad"] = jsq

    # JSTS: encode unique sentences once per prompt configuration.
    s1 = [p[0] for p in pairs]
    s2 = [p[1] for p in pairs]
    labels = np.array([p[2] for p in pairs])
    uniq = sorted(set(s1) | set(s2))
    pos = {s: i for i, s in enumerate(uniq)}
    i1 = np.array([pos[s] for s in s1])
    i2 = np.array([pos[s] for s in s2])
    if key == "e5":
        configs = [("query: / query: (primary)", "query"), ("passage: / passage:", "passage")]
    else:
        configs = [("STS / STS (primary)", "STS"), ("Document / Document", "Document"),
                   ("SearchQuery / SearchQuery", "SearchQuery")]
    jsts = {}
    for label, side in configs:
        print(f"[{key}] JSTS {label} ({len(uniq)} unique sentences) ...", flush=True)
        t = time.perf_counter()
        e = model.encode_symmetric(uniq, side=side)
        el = time.perf_counter() - t
        if key == "gemma":
            for dim in GEMMA_TRUNC_DIMS:
                ed = common.truncate_renorm(e, dim)
                r = sts_metrics(ed[i1], ed[i2], labels)
                r["encode_s"] = el
                jsts[f"{label} @{dim}"] = r
        else:
            r = sts_metrics(e[i1], e[i2], labels)
            r["encode_s"] = el
            jsts[f"{label} @{e.shape[1]}"] = r
    out["jsts"] = jsts
    out["jsts_meta"] = {"n_pairs": len(pairs), "n_unique_sentences": len(uniq)}
    return out


def print_tables(res: dict) -> None:
    print("\n### JSQuAD retrieval")
    print("| model | dim | R@1 | R@5 | R@10 | MRR@10 |")
    print("|---|---|---|---|---|---|")
    for key in common.MODEL_KEYS:
        if key not in res["models"]:
            continue
        for dim, m in res["models"][key]["jsquad"].items():
            print(f"| {key} | {dim} | {m['recall@1']:.4f} | {m['recall@5']:.4f} | {m['recall@10']:.4f} | {m['mrr@10']:.4f} |")
    print("\n### JSQuAD similarity distributions")
    print("| model | dim | series | mean | p10 | p50 | p90 |")
    print("|---|---|---|---|---|---|---|")
    for key in common.MODEL_KEYS:
        if key not in res["models"]:
            continue
        for dim, m in res["models"][key]["jsquad"].items():
            for series in ("gold_sim", "best_nongold_sim", "margin_gold_minus_best_other"):
                s = m[series]
                print(f"| {key} | {dim} | {series} | {s['mean']:.4f} | {s['p10']:.4f} | {s['p50']:.4f} | {s['p90']:.4f} |")
    print("\n### JSTS")
    print("| model | config | Spearman | Pearson |")
    print("|---|---|---|---|")
    for key in common.MODEL_KEYS:
        if key not in res["models"]:
            continue
        for cfg, m in res["models"][key]["jsts"].items():
            print(f"| {key} | {cfg} | {m['spearman']:.4f} | {m['pearson']:.4f} |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="e5,gemma")
    ap.add_argument("--max-questions", type=int, default=0, help="0 = use all questions")
    ap.add_argument("--seed", type=int, default=20261012)
    args = ap.parse_args()

    docs, queries, gold, meta = load_jsquad(args.max_questions or None, args.seed)
    pairs = load_jsts()
    print(json.dumps(meta, ensure_ascii=False), flush=True)
    res = {"jsquad_meta": meta, "seed": args.seed, "models": {}}
    for key in args.models.split(","):
        res["models"][key] = run_model(key, docs, queries, gold, pairs)
    common.RESULTS_DIR.mkdir(exist_ok=True)
    (common.RESULTS_DIR / "retrieval.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    print_tables(res)
    for key, m in res["models"].items():
        print(f"\n[{key}] load {m['load_s']:.1f}s, docs {m['encode_docs_s']:.1f}s, queries {m['encode_queries_s']:.1f}s, tokens {m['doc_token_stats']}")


if __name__ == "__main__":
    main()
