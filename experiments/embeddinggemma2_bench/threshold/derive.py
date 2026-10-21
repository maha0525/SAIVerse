"""measure.py の結果 JSON から、しきい値の導出に使う数字を出す (埋め込みは計算しない)。

    python -X utf8 experiments/embeddinggemma2_bench/threshold/derive.py results/run1.json

出すもの:
1. 層ごとのノイズ (無関係・相づち) の top の最大と、message 層 − 要約層の差
2. 候補しきい値ごとの通過数 (正解・正解以外・ノイズ)
3. 反射判断の下限: e5 の帯 (0.78〜0.86) の幅を正解分布の p10〜p90 幅で割った倍率と、
   それを gemma の分布に掛けた下限。照合として、上位 8 件のうち下限以上の候補数
   (判断に回る件数) が e5 の 0.78 と揃う gemma の下限。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
E5_T, E5_OFFSET, E5_FLOOR = 0.86, 0.02, 0.78
# JSQuAD の正解文書との cosine (results.md B 節 e5 / C 節 q4) の p10 / p90
JSQUAD = {"e5_small": (0.8438, 0.9299), "gemma_q4": (0.7198, 0.8547)}


def pct(a, q):
    return float(np.percentile(np.asarray(a, dtype=np.float64), q))


def main() -> int:
    path = Path(sys.argv[1])
    if not path.is_absolute():
        path = HERE / path
    r = json.loads(path.read_text(encoding="utf-8"))
    models = r["models"]

    print("## 1. ノイズの top の最大 (層別) と message − 要約")
    for m, d in models.items():
        pq = d["per_query"]
        for g in ("unrelated", "aizuchi"):
            rows = [x for x in pq if x["group"] == g]
            mm = [x["message"]["top"] for x in rows]
            ss = [x["summary"]["top"] for x in rows]
            print(f"{m:9s} {g:9s} message max={max(mm):.3f} p50={pct(mm, 50):.3f} | summary max={max(ss):.3f}"
                  f" p50={pct(ss, 50):.3f} | diff max={max(mm) - max(ss):+.3f} p50={pct(mm, 50) - pct(ss, 50):+.3f}")

    print("\n## 2. しきい値ごとの通過数 (on_topic 30 本 / ノイズ 30 本)")
    for m, d in models.items():
        pq = d["per_query"]
        ot = [x for x in pq if x["group"] == "on_topic"]
        noise = [x for x in pq if x["group"] != "on_topic"]
        cands = [(E5_T, E5_OFFSET)] if m == "e5_small" else [
            (t, o) for t in (0.76, 0.77, 0.78, 0.79, 0.80) for o in (0.0, 0.02)]
        for t, o in cands:
            tm, ts = t + o, t
            gold_any = sum(1 for x in ot if x["message"]["gold"] >= tm or x["summary"]["gold"] >= ts)
            gold_m = sum(1 for x in ot if x["message"]["gold"] >= tm)
            gold_s = sum(1 for x in ot if x["summary"]["gold"] >= ts)
            ng_m = sum(1 for x in ot if x["message"]["best_nongold"] >= tm)
            ng_s = sum(1 for x in ot if x["summary"]["best_nongold"] >= ts)
            nz = sum(1 for x in noise if x["message"]["top"] >= tm or x["summary"]["top"] >= ts)
            print(f"{m:9s} T={t:.2f} msg+{o:.2f}: 正解(どちらかの層)={gold_any:2d}/30 正解msg={gold_m:2d} 正解要約={gold_s:2d}"
                  f" | 正解以外の最高が通過 msg={ng_m:2d} 要約={ng_s:2d} | ノイズの通過={nz}/30")

    print("\n## 3. 反射判断の下限")
    spreads = {}
    for m, d in models.items():
        ot = [x for x in d["per_query"] if x["group"] == "on_topic"]
        bases = {
            "pooled": [x[L]["gold"] for x in ot for L in ("message", "summary")],
            "gold_any": [max(x["message"]["gold"], x["summary"]["gold"]) for x in ot],
            "message": [x["message"]["gold"] for x in ot],
            "summary": [x["summary"]["gold"] for x in ot],
        }
        spreads[m] = {k: pct(v, 90) - pct(v, 10) for k, v in bases.items()}
        spreads[m]["jsquad"] = JSQUAD[m][1] - JSQUAD[m][0]
        below = {k: float(np.mean(np.asarray(v) < E5_FLOOR)) for k, v in bases.items()}
        print(f"{m:9s} 正解分布の p10〜p90 幅: " + " ".join(f"{k}={w:.4f}" for k, w in spreads[m].items()))
        print(f"{m:9s} 0.78 未満の正解の割合: " + " ".join(f"{k}={b:.2f}" for k, b in below.items()))
    return_t = float(sys.argv[2]) if len(sys.argv) > 2 else 0.78
    for k in spreads["e5_small"]:
        ratio = (E5_T - E5_FLOOR) / spreads["e5_small"][k]
        floor = return_t - ratio * spreads["gemma_q4"][k]
        print(f"  基準={k:8s} 倍率 (0.08 / e5 幅)={ratio:.3f} gemma 幅={spreads['gemma_q4'][k]:.4f}"
              f" -> 下限 = {return_t:.2f} - {ratio * spreads['gemma_q4'][k]:.4f} = {floor:.4f}")

    print("\n### 照合: 上位 8 件のうち下限以上の候補の平均件数 (判断に回る件数)")
    def load(m, f, groups):
        rows = [x for x in models[m]["per_query"] if x["group"] in groups]
        return float(np.mean([sum(1 for s, _ in x["topk"] if s >= f) for x in rows]))
    for groups in (("on_topic",), ("unrelated",), ("aizuchi",), ("on_topic", "unrelated", "aizuchi")):
        e5_load = load("e5_small", E5_FLOOR, groups)
        grid = np.arange(0.50, 0.80, 0.005)
        loads = [load("gemma_q4", f, groups) for f in grid]
        best = min(range(len(grid)), key=lambda i: abs(loads[i] - e5_load))
        print(f"  {'+'.join(groups):28s} e5@0.78 = {e5_load:.2f} 件 | gemma で同じ件数になる下限 ≈ {grid[best]:.3f}"
              f" ({loads[best]:.2f} 件) | gemma@0.62 = {load('gemma_q4', 0.62, groups):.2f} 件"
              f" gemma@0.65 = {load('gemma_q4', 0.65, groups):.2f} 件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
