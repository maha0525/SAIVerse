"""自動想起しきい値の再測定 (EmbeddingGemma 2 q4 ONNX、合成データ)。

0.86 (sea/auto_recall.py) を決めたときの手順を合成データで再現する:
話題に沿ったクエリ / 無関係なクエリ / 短い相づち、それぞれでの top ヒットの分布。

- コーパス: message 層 300 本 (120〜480 字、文書プロンプト) + 要約層 100 本
  (30〜120 字、「タイトル: 本文」= Memopedia / Fragment の埋め込み形式)。
- クエリ: ① 話題 30 本 (正解の message 1 本 + 正解の要約 1 本が分かっている)、
  ② 無関係 20 本、③ 相づち 10 本。クエリプロンプトで埋め込む。
- モデル: gemma = 本番の ``sai_memory.memory.recall.Embedder`` をそのまま。
  e5 = 移行前の経路 (fastembed + sbert/multilingual-e5-small、``query: `` /
  ``passage: ``) を同じ合成データの上で測る較正用 (合成データが元の実データの
  測定を再現できているかの確認と、反射判断の下限の相対位置の算出に使う)。

使い方 (作業ツリーのルートから):
    python -X utf8 experiments/embeddinggemma2_bench/threshold/measure.py --out results/run1.json
    python -X utf8 experiments/embeddinggemma2_bench/threshold/measure.py --compare results/run1.json results/run2.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path
from typing import Dict, List

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))

import corpus_data as cd  # noqa: E402

SEED = 20261012
N_MESSAGES = 300
N_SUMMARIES = 100
MSG_MIN, MSG_MAX = 120, 480
SUM_MIN, SUM_MAX = 30, 120
TOPK = 8  # sea/auto_recall.py get_topk() の既定
E5_DIR_DEFAULT = Path(r"C:\Users\shuhe\workspace\SAIVerse\sbert\multilingual-e5-small")
E5_THREADS = 4


# ---------------------------------------------------------------------------
# コーパスの組み立て
# ---------------------------------------------------------------------------

def _fill(sentence: str, rng: random.Random) -> str:
    return sentence.replace("{when}", rng.choice(cd.WHEN)).replace("{feel}", rng.choice(cd.FEEL))


def _compose_message(rng: random.Random, topic: str, anchor_sentence: str | None) -> str:
    target = rng.randint(MSG_MIN, MSG_MAX)
    pool = list(cd.TOPICS[topic]["sentences"])
    rng.shuffle(pool)
    fillers = list(cd.FILLERS)
    rng.shuffle(fillers)
    parts: List[str] = []
    if anchor_sentence is not None:
        parts.append(anchor_sentence)
    length = sum(len(p) for p in parts)
    while length < target and (pool or fillers):
        use_filler = fillers and (not pool or rng.random() < 0.3)
        nxt = fillers.pop() if use_filler else _fill(pool.pop(), rng)
        if length + len(nxt) > MSG_MAX:
            if length >= MSG_MIN:
                break
            continue
        parts.append(nxt)
        length += len(nxt)
    # 正解の文がいつも先頭だと位置が手掛かりになるので、組み立て後に混ぜる
    rng.shuffle(parts)
    text = "".join(parts)
    assert MSG_MIN <= len(text) <= MSG_MAX, (len(text), text)
    return text


def _compose_summary(rng: random.Random, text: str) -> str:
    # 30 字未満は必ず、それ以外は半分の確率で結びの一文を付ける (長さを 30〜120 字に)
    if len(text) < SUM_MIN or rng.random() < 0.5:
        text = text + rng.choice(cd.SUMMARY_TAILS)
    assert SUM_MIN <= len(text) <= SUM_MAX, (len(text), text)
    return text


def build_corpus(seed: int = SEED) -> Dict:
    rng = random.Random(seed)
    topics = list(cd.TOPICS)
    docs: List[Dict] = []

    # message 層: 正解入り 30 本 + 話題ごとに 15 本 = 300 本
    for a in cd.ANCHORS:
        docs.append({
            "layer": "message", "topic": a["topic"], "anchor": a["id"],
            "text": _compose_message(rng, a["topic"], a["message"]),
        })
    n_generic = N_MESSAGES - len(cd.ANCHORS)
    for i in range(n_generic):
        topic = topics[i % len(topics)]
        docs.append({
            "layer": "message", "topic": topic, "anchor": None,
            "text": _compose_message(rng, topic, None),
        })

    # 要約層: 正解の要約 30 本 + 話題の要約 72 本から 70 本 = 100 本
    for a in cd.ANCHORS:
        docs.append({"layer": "summary", "topic": a["topic"], "anchor": a["id"],
                     "text": _compose_summary(rng, a["summary"])})
    generic = [(t, s) for t in topics for s in cd.TOPICS[t]["summaries"]]
    rng.shuffle(generic)
    for t, s in generic[: N_SUMMARIES - len(cd.ANCHORS)]:
        docs.append({"layer": "summary", "topic": t, "anchor": None, "text": _compose_summary(rng, s)})

    for i, d in enumerate(docs):
        d["id"] = f"{d['layer'][0]}{i:03d}"

    queries: List[Dict] = []
    for a in cd.ANCHORS:
        queries.append({"group": "on_topic", "text": a["query"], "anchor": a["id"]})
    for q in cd.UNRELATED_QUERIES:
        queries.append({"group": "unrelated", "text": q, "anchor": None})
    for q in cd.AIZUCHI_QUERIES:
        queries.append({"group": "aizuchi", "text": q, "anchor": None})
    return {"seed": seed, "docs": docs, "queries": queries}


# ---------------------------------------------------------------------------
# 埋め込み
# ---------------------------------------------------------------------------

class GemmaEncoder:
    name = "gemma_q4"

    def __init__(self) -> None:
        from sai_memory.memory.recall import Embedder
        self.embedder = Embedder(local_model_path=str(REPO / "sbert" / "embeddinggemma-2-q4"))

    def docs(self, texts: List[str]) -> np.ndarray:
        return np.asarray(self.embedder.embed(texts, is_query=False), dtype=np.float32)

    def queries(self, texts: List[str]) -> np.ndarray:
        return np.asarray(self.embedder.embed(texts, is_query=True), dtype=np.float32)


class E5Encoder:
    """移行前の Embedder と同じ組み方 (fastembed の custom model、mean pooling、正規化)。"""

    name = "e5_small"

    def __init__(self, model_dir: Path) -> None:
        from fastembed import TextEmbedding
        from fastembed.common.model_description import ModelSource, PoolingType

        model = "intfloat/multilingual-e5-small"
        extras = [r for r in (
            "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
            "sentencepiece.bpe.model", "vocab.txt", "modules.json", "config.json",
            "1_Pooling/config.json",
        ) if (model_dir / r).exists()]
        TextEmbedding.add_custom_model(
            model=model, pooling=PoolingType.MEAN, normalization=True,
            sources=ModelSource(hf=f"local/{model_dir.name}"), dim=384,
            model_file="onnx/model.onnx", additional_files=extras,
        )
        self.model = TextEmbedding(
            model_name=model, specific_model_path=str(model_dir),
            local_files_only=True, threads=E5_THREADS,
        )

    def _embed(self, texts: List[str]) -> np.ndarray:
        v = np.asarray(list(self.model.embed(texts)), dtype=np.float32)
        return v / np.linalg.norm(v, axis=1, keepdims=True)

    def docs(self, texts: List[str]) -> np.ndarray:
        return self._embed(["passage: " + t for t in texts])

    def queries(self, texts: List[str]) -> np.ndarray:
        return self._embed(["query: " + t for t in texts])


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------

def _dist(values: List[float]) -> Dict[str, float]:
    a = np.asarray(values, dtype=np.float64)
    return {
        "n": int(a.size),
        "mean": float(a.mean()),
        "min": float(a.min()),
        "p10": float(np.percentile(a, 10)),
        "p50": float(np.percentile(a, 50)),
        "p90": float(np.percentile(a, 90)),
        "max": float(a.max()),
    }


def measure(encoder, corpus: Dict) -> Dict:
    docs, queries = corpus["docs"], corpus["queries"]
    t0 = time.perf_counter()
    dv = encoder.docs([d["text"] for d in docs])
    t1 = time.perf_counter()
    qv = encoder.queries([q["text"] for q in queries])
    t2 = time.perf_counter()
    sims = qv @ dv.T  # 両方 L2 正規化済み = cosine

    layers = {"message": [], "summary": []}
    for j, d in enumerate(docs):
        layers[d["layer"]].append(j)
    layer_idx = {k: np.asarray(v) for k, v in layers.items()}

    per_query = []
    for i, q in enumerate(queries):
        row = {"group": q["group"], "text": q["text"], "anchor": q["anchor"]}
        s_all = sims[i]
        top = int(np.argmax(s_all))
        row["top_all"] = {"score": float(s_all[top]), "doc": docs[top]["id"], "layer": docs[top]["layer"]}
        # 上位 TOPK 件 (自動想起の topk=8 相当、層をまたいだ cosine 順)。反射判断の
        # 下限を動かしたときに判断へ回る候補の数を見積もるために残す。
        order = np.argsort(-s_all)[:TOPK]
        row["topk"] = [[float(s_all[j]), docs[j]["layer"]] for j in order]
        for layer, idx in layer_idx.items():
            s = s_all[idx]
            k = int(np.argmax(s))
            entry = {"top": float(s[k]), "top_doc": docs[idx[k]]["id"]}
            if q["anchor"] is not None:
                gold_mask = np.asarray([docs[j]["anchor"] == q["anchor"] for j in idx])
                entry["gold"] = float(s[gold_mask].max())
                entry["best_nongold"] = float(s[~gold_mask].max())
                entry["gold_rank"] = int((s > s[gold_mask].max()).sum()) + 1
            row[layer] = entry
        per_query.append(row)

    summary: Dict = {}
    for group in ("on_topic", "unrelated", "aizuchi"):
        rows = [r for r in per_query if r["group"] == group]
        g: Dict = {"top_all": _dist([r["top_all"]["score"] for r in rows])}
        for layer in ("message", "summary"):
            g[layer] = {"top": _dist([r[layer]["top"] for r in rows])}
            if group == "on_topic":
                g[layer]["gold"] = _dist([r[layer]["gold"] for r in rows])
                g[layer]["best_nongold"] = _dist([r[layer]["best_nongold"] for r in rows])
                g[layer]["margin"] = _dist([r[layer]["gold"] - r[layer]["best_nongold"] for r in rows])
                g[layer]["recall@1"] = float(np.mean([r[layer]["gold_rank"] == 1 for r in rows]))
        summary[group] = g
    # 正解ヒット (= 正解が message / 要約のどちらかで拾えたときの良い方)
    summary["on_topic"]["gold_any"] = _dist(
        [max(r["message"]["gold"], r["summary"]["gold"]) for r in per_query if r["group"] == "on_topic"])

    return {
        "model": encoder.name,
        "encode_docs_s": t1 - t0,
        "encode_queries_s": t2 - t1,
        "summary": summary,
        "per_query": per_query,
    }


def corpus_stats(corpus: Dict) -> Dict:
    out = {}
    for layer in ("message", "summary"):
        lens = [len(d["text"]) for d in corpus["docs"] if d["layer"] == layer]
        out[layer] = {"n": len(lens), "chars_min": min(lens), "chars_median": float(np.median(lens)),
                      "chars_max": max(lens)}
    out["topics"] = len(cd.TOPICS)
    out["queries"] = {g: sum(1 for q in corpus["queries"] if q["group"] == g)
                      for g in ("on_topic", "unrelated", "aizuchi")}
    return out


def compare(a_path: Path, b_path: Path) -> int:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    if a["corpus"] != b["corpus"]:
        print("corpus differs")
        return 1
    worst = 0.0
    for m in a["models"]:
        for ra, rb in zip(a["models"][m]["per_query"], b["models"][m]["per_query"]):
            for layer in ("message", "summary"):
                for k in ("top", "gold", "best_nongold"):
                    if k in ra[layer]:
                        worst = max(worst, abs(ra[layer][k] - rb[layer][k]))
            if ra["message"]["top_doc"] != rb["message"]["top_doc"] or ra["summary"]["top_doc"] != rb["summary"]["top_doc"]:
                print("top doc differs:", m, ra["text"])
                return 1
    print(f"corpus identical; max |diff| of per-query scores = {worst:.3g}")
    return 0 if worst < 1e-6 else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, help="結果 JSON (threshold/ からの相対パス可)")
    ap.add_argument("--models", default="gemma,e5")
    ap.add_argument("--e5-dir", type=Path, default=E5_DIR_DEFAULT)
    ap.add_argument("--compare", nargs=2, type=Path)
    args = ap.parse_args()
    if args.compare:
        return compare(*[p if p.is_absolute() else HERE / p for p in args.compare])

    corpus = build_corpus()
    result = {"seed": SEED, "corpus_stats": corpus_stats(corpus), "corpus": corpus, "models": {}}
    for m in args.models.split(","):
        enc = GemmaEncoder() if m == "gemma" else E5Encoder(args.e5_dir)
        print(f"[{enc.name}] embedding {len(corpus['docs'])} docs + {len(corpus['queries'])} queries ...", flush=True)
        result["models"][enc.name] = measure(enc, corpus)
        print(f"[{enc.name}] docs {result['models'][enc.name]['encode_docs_s']:.1f}s", flush=True)
        del enc

    out = args.out if args.out.is_absolute() else HERE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", out)
    for name, r in result["models"].items():
        s = r["summary"]
        print(f"== {name}")
        for g in ("on_topic", "unrelated", "aizuchi"):
            for layer in ("message", "summary"):
                d = s[g][layer]["top"]
                print(f"  {g:9s} {layer:7s} top  p10={d['p10']:.3f} p50={d['p50']:.3f} p90={d['p90']:.3f} max={d['max']:.3f}")
        for layer in ("message", "summary"):
            gd, nd = s["on_topic"][layer]["gold"], s["on_topic"][layer]["best_nongold"]
            print(f"  on_topic  {layer:7s} gold p10={gd['p10']:.3f} p50={gd['p50']:.3f} min={gd['min']:.3f}"
                  f" | nongold p50={nd['p50']:.3f} max={nd['max']:.3f} | R@1={s['on_topic'][layer]['recall@1']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
