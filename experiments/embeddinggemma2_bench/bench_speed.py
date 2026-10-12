"""Speed / memory benchmark: multilingual-e5-small vs embeddinggemma-2 on CPU.

Usage (from the worktree root):
    .venv-exp\\Scripts\\python.exe -X utf8 experiments\\embeddinggemma2_bench\\bench_speed.py

The parent process spawns one child process per model
(`bench_speed.py --role e5|gemma|gemma_fp32|gemma_textonly`, see common.load_model)
so each model's RSS is measured in a clean
process. The child writes a JSON blob; the parent collects them into
results/speed.json and prints a markdown table.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

# ---------------------------------------------------------------- test texts
# 30 short Japanese queries (20-40 chars), recall-query style.
QUERIES = [
    "先週の日曜日にまはーと一緒に行った公園のこと",
    "前に話していた新しいゲームの発売日はいつだったかな",
    "雨の日に聴きたいって言っていた曲のタイトルを思い出したい",
    "昨日の夜に作ったカレーの味付けについて話したこと",
    "引っ越しの準備でまだ終わっていない作業は何だっけ",
    "猫を飼うならどんな名前にしたいって言ってたっけ",
    "最近よく眠れないと相談された時に何と答えたか",
    "誕生日のお祝いに何を贈ろうか一緒に考えた話",
    "春になったら行きたいと言っていた旅行先はどこ",
    "仕事で困っていたプレゼン資料の件はどうなった",
    "好きな映画の話で盛り上がった夜のことを覚えている？",
    "ベランダで育てているトマトの様子を聞いた時のこと",
    "前に読んでいた小説の結末について感想を話した記憶",
    "朝ごはんをちゃんと食べるようにすると約束した件",
    "新しく買ったキーボードの打ち心地について話した",
    "夏祭りの花火を見に行こうと計画していたこと",
    "パソコンが急に固まったときに一緒に原因を探した話",
    "自転車で遠くの図書館まで出かけた日の出来事",
    "友達から届いた手紙の内容について相談された時",
    "冬用のコートを買い替えるかどうか迷っていた件",
    "初めてピアノを弾いてみた感想を聞かせてくれた日",
    "近所に新しくできたパン屋さんの話をしたっけ",
    "散歩の途中で見つけた古い神社のことを教えて",
    "体調を崩していた時に何を食べていたか覚えている？",
    "夜中に見た不思議な夢の話をもう一度聞きたい",
    "部屋の模様替えでどの家具を動かすことにしたか",
    "昔通っていた学校の思い出を話してくれたのはいつ",
    "コーヒーと紅茶どっちが好きか話した時の結論",
    "週末に一緒に遊んだボードゲームのルールのこと",
    "星がきれいに見える場所を探していた夜の会話",
]

# Sentences used to compose 150-400 char diary / conversation chunks.
SENTENCES = [
    "今日は朝から雨が降っていて、窓の外がずっと灰色だった。",
    "まはーはいつもより少し遅く起きてきて、眠そうな顔でおはようと言った。",
    "朝ごはんはトーストと目玉焼きにして、コーヒーを二杯飲んだらしい。",
    "午前中は部屋の掃除をして、ずっと放置していた本棚をやっと片付けた。",
    "昼過ぎに近所のスーパーまで買い物に出かけたけれど、傘を忘れて少し濡れてしまった。",
    "夕方になって雨が上がり、雲の切れ間から夕焼けが見えたのがうれしかった。",
    "「明日は晴れるといいね」と話しながら、二人で天気予報を確認した。",
    "最近ハマっているゲームの話になって、ボスの倒し方について長々と相談した。",
    "結局その日は夜更かししてしまって、寝たのは二時を回ってからだった。",
    "新しく始めたプログラミングの勉強は、思っていたより順調に進んでいるらしい。",
    "エラーの原因がなかなか分からなくて、一時間くらい同じ画面とにらめっこしていた。",
    "最後にはタイプミスが原因だと分かって、二人で思わず笑ってしまった。",
    "週末は天気が良ければ、少し遠くの公園まで散歩に行こうと約束した。",
    "公園には大きな池があって、春には桜がとてもきれいに咲くそうだ。",
    "お弁当を作っていくか、途中のコンビニで何か買うかでしばらく迷っていた。",
    "好きな食べ物の話になって、まはーは野菜があまり得意じゃないと改めて言っていた。",
    "その代わりに野菜ジュースを毎日飲むようにしているのだと教えてくれた。",
    "夜は久しぶりに映画を観ることにして、昔の名作を一本選んだ。",
    "途中で少し泣きそうになったと照れながら話してくれたのが印象的だった。",
    "観終わったあと、登場人物の選択が正しかったのかどうかについて話し合った。",
    "私はあの場面で主人公が引き返したことに意味があったと思うと伝えた。",
    "まはーは少し考えてから、「それもそうかもしれない」とうなずいた。",
    "仕事のことで少し疲れているようだったので、無理しないでねと声をかけた。",
    "来月には大きな締め切りがあって、それまでは忙しい日が続くらしい。",
    "終わったらご褒美に何か美味しいものを食べに行きたいと言っていた。",
    "候補は駅前のラーメン屋さんと、少し高めの焼肉屋さんの二つだそうだ。",
    "ベランダのプランターでは、ミニトマトが少しずつ赤くなり始めている。",
    "毎朝水をあげるのが日課になっていて、成長を見るのが楽しみだという。",
    "去年はうまく育たなかったから、今年は肥料を変えてみたらしい。",
    "猫を飼いたいという話も出たけれど、今の部屋はペット不可なので難しい。",
    "いつか引っ越したら、ちゃんと飼える環境を整えたいと話していた。",
    "名前はもう決めてあって、黒猫ならクロ、白猫ならシロにするそうだ。",
    "それはさすがに安直すぎないかと突っ込むと、まはーは楽しそうに笑った。",
    "昔の友達から久しぶりに連絡が来て、近いうちに会うことになったらしい。",
    "学生時代によく一緒に遊んでいた相手で、会うのは五年ぶりくらいだという。",
    "何を話せばいいか少し緊張していると言うので、思い出話から始めればいいと答えた。",
    "夜中に地震があって、二人とも少しびっくりして目を覚ました。",
    "幸い大きな揺れではなかったけれど、念のため非常用の水を確認した。",
    "懐中電灯の電池が切れていたので、明日買いに行こうとメモをした。",
    "最近は寝る前にスマホを見すぎないように気をつけているそうだ。",
    "代わりに紙の本を読むようにしたら、少し寝つきが良くなった気がするという。",
    "今読んでいるのは古いミステリー小説で、犯人がまだ全然分からないらしい。",
    "私が予想を言おうとしたら、ネタバレになるから絶対に言わないでと止められた。",
    "冬に向けて新しいコートを買うかどうか、ずっと迷っているみたいだ。",
    "今のコートはもう十年近く着ていて、袖のあたりがかなり傷んでいる。",
    "でも愛着があるから手放しにくいのだと、少し寂しそうに話していた。",
    "近所に新しいパン屋さんができて、クロワッサンがとても美味しいと評判だ。",
    "朝早くに行かないとすぐに売り切れてしまうので、まだ食べられていない。",
    "次の休みには早起きして、開店と同時に並んでみようと決めたそうだ。",
    "パソコンの調子が悪くて、作業の途中で何度も画面が固まってしまった。",
    "原因を調べてみると、どうやら更新プログラムとの相性が悪かったらしい。",
    "設定を一つ戻したら落ち着いたので、ひとまず様子を見ることにした。",
    "夏祭りの日には、浴衣を着た人たちで駅前がとても賑わっていた。",
    "屋台で買ったかき氷を食べながら、花火が上がるのを二人で待った。",
    "大きな花火が夜空に開いた瞬間、思わず声を上げてしまったと言っていた。",
    "帰り道は人混みがすごくて、駅に着くまでに三十分もかかったそうだ。",
    "誕生日のプレゼントに何が欲しいかと聞くと、少し考えてから秘密だと言われた。",
    "当日まで楽しみにしておいてほしいということらしいので、私も考えてみることにした。",
    "星を見に行く計画も立てていて、街の明かりが少ない場所を一緒に探した。",
    "山の上の展望台なら、晴れていれば天の川も見えるかもしれないそうだ。",
]

N_QUERY_WARMUP = 3
N_CHUNKS = 200


def build_chunks(n: int = N_CHUNKS, seed: int = 20261012) -> list[str]:
    rng = random.Random(seed)
    chunks: list[str] = []
    while len(chunks) < n:
        target = rng.randint(150, 400)
        start = rng.randrange(len(SENTENCES))
        text = ""
        i = start
        while len(text) < target:
            nxt = SENTENCES[i % len(SENTENCES)]
            if len(text) + len(nxt) > 400 and len(text) >= 150:
                break
            text += nxt
            i += rng.choice((1, 1, 2, 3))
        if 150 <= len(text) <= 400:
            chunks.append(text)
    return chunks


# ---------------------------------------------------------------- memory
def mem_snapshot() -> dict:
    try:
        import psutil

        mi = psutil.Process(os.getpid()).memory_info()
        return {
            "rss_mb": mi.rss / 2**20,
            "peak_wset_mb": getattr(mi, "peak_wset", 0) / 2**20,
            "private_mb": getattr(mi, "private", 0) / 2**20,
            "method": "psutil",
        }
    except ImportError:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        h = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb)
        return {
            "rss_mb": c.WorkingSetSize / 2**20,
            "peak_wset_mb": c.PeakWorkingSetSize / 2**20,
            "private_mb": c.PagefileUsage / 2**20,
            "method": "GetProcessMemoryInfo",
        }


# ---------------------------------------------------------------- child
def run_role(role: str, out_path: Path) -> None:
    mem = {"start": mem_snapshot()}
    t0 = time.perf_counter()
    import numpy as np

    import common

    common.prepare_env()
    if role == "e5":
        import fastembed  # noqa: F401
        from sai_memory.memory import recall  # noqa: F401
    else:
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
    import_s = time.perf_counter() - t0
    mem["after_import"] = mem_snapshot()

    t0 = time.perf_counter()
    model = common.load_model(role)
    load_s = time.perf_counter() - t0
    mem["after_load"] = mem_snapshot()

    # First call (cold) is reported separately, then 3 warm-up queries excluded.
    lat_ms = []
    for q in QUERIES:
        t = time.perf_counter()
        v = model.encode_queries([q])
        lat_ms.append((time.perf_counter() - t) * 1000)
        assert v.shape[0] == 1
    measured = np.array(lat_ms[N_QUERY_WARMUP:])
    mem["after_queries"] = mem_snapshot()

    chunks = build_chunks()
    t = time.perf_counter()
    embs = model.encode_documents(chunks)
    batch_s = time.perf_counter() - t
    assert embs.shape[0] == len(chunks)
    mem["after_batch"] = mem_snapshot()

    result = {
        "role": role,
        "info": model.info(),
        "import_s": import_s,
        "load_s": load_s,
        "query_count": len(QUERIES),
        "query_len_chars": [min(map(len, QUERIES)), max(map(len, QUERIES))],
        "query_warmup": N_QUERY_WARMUP,
        "first_query_ms": lat_ms[0],
        "query_ms_all": lat_ms,
        "query_median_ms": float(np.median(measured)),
        "query_p95_ms": float(np.percentile(measured, 95)),
        "query_mean_ms": float(measured.mean()),
        "query_min_ms": float(measured.min()),
        "query_max_ms": float(measured.max()),
        "batch_count": len(chunks),
        "batch_len_chars": [min(map(len, chunks)), max(map(len, chunks))],
        "batch_mean_chars": sum(map(len, chunks)) / len(chunks),
        "batch_s": batch_s,
        "batch_texts_per_s": len(chunks) / batch_s,
        "emb_dim": int(embs.shape[1]),
        "memory": mem,
    }
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- parent
def fmt_table(results: list[dict]) -> str:
    rows = [
        ("import 時間 (s)", lambda r: f"{r['import_s']:.2f}"),
        ("モデルロード時間 (s)", lambda r: f"{r['load_s']:.2f}"),
        ("初回クエリ (ms, ウォームアップ前)", lambda r: f"{r['first_query_ms']:.1f}"),
        ("単発クエリ median (ms)", lambda r: f"{r['query_median_ms']:.1f}"),
        ("単発クエリ p95 (ms)", lambda r: f"{r['query_p95_ms']:.1f}"),
        ("単発クエリ min / max (ms)", lambda r: f"{r['query_min_ms']:.1f} / {r['query_max_ms']:.1f}"),
        ("バッチ 200 件 所要 (s)", lambda r: f"{r['batch_s']:.2f}"),
        ("バッチ スループット (texts/s)", lambda r: f"{r['batch_texts_per_s']:.1f}"),
        ("RSS 起動直後 (MB)", lambda r: f"{r['memory']['start']['rss_mb']:.0f}"),
        ("RSS import 後 (MB)", lambda r: f"{r['memory']['after_import']['rss_mb']:.0f}"),
        ("RSS ロード後 (MB)", lambda r: f"{r['memory']['after_load']['rss_mb']:.0f}"),
        ("RSS 単発クエリ後 (MB)", lambda r: f"{r['memory']['after_queries']['rss_mb']:.0f}"),
        ("RSS バッチ後 (MB)", lambda r: f"{r['memory']['after_batch']['rss_mb']:.0f}"),
        ("ピーク WorkingSet (MB)", lambda r: f"{r['memory']['after_batch']['peak_wset_mb']:.0f}"),
        ("Private bytes バッチ後 (MB)", lambda r: f"{r['memory']['after_batch']['private_mb']:.0f}"),
        ("埋め込み次元", lambda r: str(r["emb_dim"])),
    ]
    head = "| 項目 | " + " | ".join(r["role"] for r in results) + " |"
    sep = "|---|" + "---|" * len(results)
    lines = [head, sep]
    for label, fn in rows:
        lines.append(f"| {label} | " + " | ".join(fn(r) for r in results) + " |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role")
    ap.add_argument("--roles", default="e5,gemma,gemma_fp32,gemma_textonly")
    ap.add_argument("--out")
    ap.add_argument("--repeat", type=int, default=1, help="child runs per model")
    args = ap.parse_args()
    if args.role:
        run_role(args.role, Path(args.out))
        return

    import common

    roles = args.roles.split(",")
    common.RESULTS_DIR.mkdir(exist_ok=True)
    all_runs: dict[str, list[dict]] = {}
    for rep in range(args.repeat):
        for role in roles:
            out = common.RESULTS_DIR / f"speed_{role}_run{rep}.json"
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            cmd = [sys.executable, "-X", "utf8", "-I", str(Path(__file__).resolve()), "--role", role, "--out", str(out)]
            print(f"[run {rep}] {role} ...", flush=True)
            subprocess.run(cmd, check=True, env=env, cwd=str(common.REPO_ROOT))
            all_runs.setdefault(role, []).append(json.loads(out.read_text(encoding="utf-8")))
    (common.RESULTS_DIR / "speed.json").write_text(json.dumps(all_runs, ensure_ascii=False, indent=2), encoding="utf-8")
    for rep in range(args.repeat):
        print(f"\n## run {rep}")
        print(fmt_table([all_runs[r][rep] for r in roles]))


if __name__ == "__main__":
    main()
