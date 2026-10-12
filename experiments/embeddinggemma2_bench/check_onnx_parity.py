"""Parity check: onnx-community/embeddinggemma-2-ONNX variants vs sentence-transformers.

Usage (from the worktree root):
    .venv-exp\\Scripts\\python.exe -X utf8 -I experiments\\embeddinggemma2_bench\\check_onnx_parity.py

For 10 Japanese texts (5 recall-style queries + 5 diary sentences, taken from
bench_speed.py) and each of the three prompts (SearchQuery / Document / STS),
computes the cosine between each ONNX variant's embedding and the
sentence-transformers embedding (bf16 checkpoint default = GemmaWrapper(), and
float32 = GemmaWrapper(fp32=True) as a second reference).

The sentence-transformers outputs are re-normalised in float32 before the
cosine (with bf16 weights ST's own normalisation leaves norms slightly off 1;
the raw norm range is recorded).

Also checks
  - that the tokenizers-library ids used by the ONNX wrapper equal the ids the
    SentenceTransformer tokenizer produces for the same prompt+text,
  - that batched (padded) ONNX output equals one-text-at-a-time output.

Writes results/onnx_parity.json.
"""
from __future__ import annotations

import gc
import json
import sys
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import common  # noqa: E402

common.prepare_env()

import numpy as np  # noqa: E402
from bench_speed import QUERIES, SENTENCES  # noqa: E402

TEXTS = QUERIES[:5] + SENTENCES[:5]
PROMPTS = ("SearchQuery", "Document", "STS")


def encode_all(model) -> dict:
    return {p: model._enc(TEXTS, p) for p in PROMPTS}


def main() -> None:
    res: dict = {"texts": TEXTS, "prompts": {p: common.GEMMA_PROMPTS[p] for p in PROMPTS}}

    refs: dict[str, dict] = {}
    tok_check = None
    for ref_key, kwargs in (("st_bf16", {}), ("st_fp32", {"fp32": True})):
        m = common.GemmaWrapper(**kwargs)
        assert all(m.model.prompts[p] == common.GEMMA_PROMPTS[p] for p in PROMPTS), "prompt mismatch"
        raw = encode_all(m)
        # GemmaWrapper returns ST's normalize_embeddings=True output cast to float32.
        # With bf16 weights the normalisation happens in bf16, so the norms are not
        # exactly 1; record that, then re-normalise in fp32 before taking cosines.
        norms = np.concatenate([np.linalg.norm(raw[p], axis=1) for p in PROMPTS])
        refs[ref_key] = {p: common._l2(raw[p]) for p in PROMPTS}
        res[f"{ref_key}_info"] = {
            "dtype": m.info()["dtype"],
            "max_seq_length": m.model.max_seq_length,
            "raw_output_norm_min": float(norms.min()),
            "raw_output_norm_max": float(norms.max()),
        }
        print(f"{ref_key}: raw output norm min/max = {norms.min():.5f}/{norms.max():.5f}, max_seq_length={m.model.max_seq_length}", flush=True)
        if tok_check is None:
            tk = m.model.tokenizer
            st_ids = {p: [list(tk(common.GEMMA_PROMPTS[p] + t)["input_ids"]) for t in TEXTS] for p in PROMPTS}
            tok_check = st_ids
        del m
        gc.collect()

    variants: dict = {}
    for role in common.GEMMA_ONNX_VARIANTS:
        m = common.GemmaOnnxWrapper(role)
        # tokenizer parity
        tok_equal = all(
            m.tokenize([common.GEMMA_PROMPTS[p] + t for t in TEXTS]) == tok_check[p] for p in PROMPTS
        )
        embs = encode_all(m)
        # padding invariance: batched vs one at a time (SearchQuery prompt)
        single = np.vstack([m._enc([t], "SearchQuery") for t in TEXTS])
        pad_cos = (single * embs["SearchQuery"]).sum(axis=1)
        v: dict = {"tokenizer_ids_equal_st": tok_equal, "batched_vs_single_cos_min": float(pad_cos.min())}
        for ref_key, ref in refs.items():
            per_prompt = {}
            all_cos = []
            for p in PROMPTS:
                cos = (embs[p] * ref[p]).sum(axis=1)
                per_prompt[p] = [float(c) for c in cos]
                all_cos.extend(cos.tolist())
            a = np.array(all_cos)
            v[f"vs_{ref_key}"] = {
                "per_prompt": per_prompt,
                "min": float(a.min()),
                "mean": float(a.mean()),
                "max": float(a.max()),
                "n": int(a.size),
            }
        variants[role] = v
        print(
            f"{role}: tok_equal={tok_equal} pad_min={pad_cos.min():.6f} "
            f"vs_bf16 min/mean={v['vs_st_bf16']['min']:.4f}/{v['vs_st_bf16']['mean']:.4f} "
            f"vs_fp32 min/mean={v['vs_st_fp32']['min']:.4f}/{v['vs_st_fp32']['mean']:.4f}",
            flush=True,
        )
        del m
        gc.collect()

    # bf16 vs fp32 reference spread on the same 30 embeddings
    bf_fp = np.concatenate([(refs["st_bf16"][p] * refs["st_fp32"][p]).sum(axis=1) for p in PROMPTS])
    res["st_bf16_vs_st_fp32"] = {"min": float(bf_fp.min()), "mean": float(bf_fp.mean()), "max": float(bf_fp.max())}
    print(f"st_bf16 vs st_fp32: min/mean={bf_fp.min():.4f}/{bf_fp.mean():.4f}")
    res["variants"] = variants
    common.RESULTS_DIR.mkdir(exist_ok=True)
    (common.RESULTS_DIR / "onnx_parity.json").write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    # per-text table, SearchQuery prompt, vs bf16
    print("\n| # | text | " + " | ".join(variants) + " |")
    for i, t in enumerate(TEXTS):
        cells = " | ".join(f"{variants[r]['vs_st_bf16']['per_prompt']['SearchQuery'][i]:.4f}" for r in variants)
        print(f"| {i} | {t[:20]} | {cells} |")


if __name__ == "__main__":
    main()
