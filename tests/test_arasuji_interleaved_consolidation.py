"""束ねの挟み込み (2026-09-03) の結合テスト — 実物の三点を繋いで回す。

- 実物の ``executor.execute_plan`` (一次あらすじの確定 + ``after_chunk``)
- 実物の ``bands.run_band_overflow`` (並びの予算超過で古い側を 1 個の親に畳む)
- 実物の ``context.get_episode_context_for_timerange`` (各チャンクのプロンプトに
  載る「これまでの流れ」— 新しい側から最大 20 件辿り、近い過去はレベル1、
  MIN_ENTRIES_PER_LEVEL 件読んだら一つ上のレベルへ昇格できる)

固定する性質 (docs/intent/arasuji_levels.md §3-2 の 2026-09-03 の項):

1. 束ねをチャンク確定のたびに挟むと、走行の後半のチャンクのプロンプトには
   レベル2 (「あらすじのあらすじ」) の項目が載る — 直前の一次あらすじは
   レベル1 のまま (近い過去は細かく)。
2. 挟まない (after_chunk=None、2026-07-21〜09-02 の挙動) と、同じ走行の
   どのプロンプトにもレベル2 は現れない — 回帰の固定。

LLM は偽物 (決定論の文字列を返す)。発火は本物の予算 (BAND_CHAR_LIMIT /
BAND_CHAR_KEEP) に対して、一次あらすじの字数を一定 (LV1_CHARS) にすることで
起こす — 上限を下げるノブは使わず、必要なチャンク数を定数から導く。
"""

import unittest

from sai_memory.arasuji.alignment import (
    CHUNK_LLM_BATCH,
    AlignmentPlan,
    PlannedChunk,
)
from sai_memory.arasuji.bands import (
    BAND_CHAR_KEEP,
    BAND_CHAR_LIMIT,
    run_band_overflow,
)
from sai_memory.arasuji.context import MIN_ENTRIES_PER_LEVEL
from sai_memory.arasuji.executor import execute_plan
from sai_memory.arasuji.storage import get_entries_by_level, init_arasuji_tables
from sai_memory.memory.storage import Message, init_db

#: 一次あらすじ 1 件の字数 (偽 LLM の応答長)。
LV1_CHARS = 300
#: レベル2 (束ねの親) 1 件の字数。
LV2_CHARS = 300

#: 「これまでの流れ」の見出しと、レベルの札。
CONTEXT_HEADING = "## これまでの流れ（参考）"
LV1_LABEL = "【あらすじ: "
LV2_LABEL = "【あらすじのあらすじ: "


class _ScriptedClient:
    """プロンプトの種類で応答を変える偽 LLM。

    - 一次あらすじ (executor) のプロンプト → ``C{n:02d}`` + 詰め物 (n はチャンクの
      通し番号、0 始まり)。内容で「どのチャンクの あらすじか」を見分ける。
    - 束ね (bands) のプロンプト → ``P`` + 詰め物。
    受け取ったプロンプトを種類つきで順に記録する。
    """

    def __init__(self):
        self.events = []  # ("chunk", index, prompt) / ("band", index, prompt)
        self._chunks = 0
        self._bands = 0

    def generate(self, messages, tools):
        prompt = messages[0]["content"]
        if "## 統合対象の材料" in prompt:
            self.events.append(("band", self._bands, prompt))
            self._bands += 1
            return "P" + "p" * (LV2_CHARS - 1)
        if "## 今回記録する会話" in prompt:
            idx = self._chunks
            self._chunks += 1
            self.events.append(("chunk", idx, prompt))
            head = f"C{idx:02d}"
            return head + "x" * (LV1_CHARS - len(head))
        raise AssertionError(f"unexpected prompt: {prompt[:80]!r}")

    def consume_usage(self):
        return None

    def chunk_prompts(self):
        return [p for kind, _i, p in self.events if kind == "chunk"]

    def kinds(self):
        return [kind for kind, _i, _p in self.events]


def _msg(idx):
    return Message(
        id=f"m{idx:02d}",
        thread_id="main",
        role="user",
        content="a" * 50,
        resource_id=None,
        created_at=1_000 + idx * 100,
        metadata=None,
    )


def _plan(n_chunks):
    chunks = [
        PlannedChunk(
            kind=CHUNK_LLM_BATCH,
            messages=[_msg(i)],
            episode_refs=[],
            coverage_chars=50,
        )
        for i in range(n_chunks)
    ]
    return AlignmentPlan(chunks=chunks, total_unprocessed=n_chunks)


def _context_items(prompt):
    """プロンプトの「これまでの流れ」から (レベル名, 本文の頭 3 字) を古い順に。"""
    if CONTEXT_HEADING not in prompt:
        return []
    section = prompt.split(CONTEXT_HEADING, 1)[1].split("\n## ", 1)[0]
    lines = section.split("\n")
    out = []
    for i, line in enumerate(lines):
        if line.startswith("【") and ": " in line and line.endswith("】"):
            out.append((line[1:].split(":", 1)[0], lines[i + 1][:3]))
    return out


class InterleavedConsolidationTest(unittest.TestCase):
    def setUp(self):
        self.conn = init_db(":memory:")
        init_arasuji_tables(self.conn)
        self.addCleanup(self.conn.close)

        # 発火に要るチャンク数を本物の予算から導く。
        # 並びの字数 > 上限 で発火 → ceil を 1 件超えたところ。
        self.n_to_overflow = BAND_CHAR_LIMIT // LV1_CHARS + 1        # 17
        # 畳み後に残す新しい側の件数 (残す量に収まる分)。
        self.n_keep = BAND_CHAR_KEEP // LV1_CHARS                    # 8
        # 最古から畳まれる件数 = 親 1 件が覆う子の数。
        self.n_folded = self.n_to_overflow - self.n_keep              # 9
        # 親の後ろにレベル1 が MIN 件並んで初めて昇格でき、その次のチャンクの
        # プロンプトで親 (レベル2) が読まれる。
        self.first_lv2_prompt = self.n_folded + MIN_ENTRIES_PER_LEVEL  # 19
        self.n_chunks = self.first_lv2_prompt + 1                     # 20
        self.assertGreater(self.n_folded, 1, "畳みは 2 件以上でしか起きない")

    def _run(self, *, interleave: bool):
        """generate_chronicle と同じ配線で実物を回す。

        after_chunk ごとに run_band_overflow を「残り予算」つきで呼び、走行の
        最後にもう一度呼ぶ (interleave=False は挟み込み前の挙動 = 最後だけ)。
        """
        client = _ScriptedClient()
        budget = 5                      # 確認ゲートで承認された dry 件数の代替
        consolidated = [0]              # 走行全体の累計
        failures, unrecorded = [], []

        def _consolidate():
            if consolidated[0] >= budget:
                return
            folded = run_band_overflow(
                self.conn, client,
                persona_id=None,
                cancel_check=None,
                excluded_entry_ids=None,
                batch_callback=None,
                max_folds=budget - consolidated[0],
                extraction_failures=failures,
                db_lock=None,
                extraction_failures_unrecorded=unrecorded,
                progress_callback=None,
            )
            consolidated[0] += int(folded or 0)

        result = execute_plan(
            _plan(self.n_chunks), client, self.conn,
            after_chunk=(lambda done, total: _consolidate()) if interleave else None,
        )
        _consolidate()
        self.assertEqual(result.created_count, self.n_chunks)
        self.assertEqual(failures, [])
        self.assertEqual(unrecorded, [])
        return client, consolidated[0]

    def test_later_chunks_see_the_hierarchy_and_near_past_stays_lv1(self):
        client, folded = self._run(interleave=True)
        prompts = client.chunk_prompts()
        self.assertEqual(len(prompts), self.n_chunks)

        # 束ねは n_to_overflow 件目のチャンクの確定直後に 1 回だけ走った
        # (以後の並びは上限に届かない)。
        self.assertEqual(folded, 1)
        kinds = client.kinds()
        self.assertEqual(kinds.count("band"), 1)
        self.assertEqual(kinds.index("band"), self.n_to_overflow,
                         "束ねは n_to_overflow 件目のチャンクの直後")
        lv2 = get_entries_by_level(self.conn, 2, order_by_time=True)
        self.assertEqual(len(lv2), 1)
        self.assertEqual(len(lv2[0].source_ids), self.n_folded)

        # 先頭のチャンクには「これまでの流れ」が無い。
        self.assertNotIn(CONTEXT_HEADING, prompts[0])
        # 束ねより前のチャンクは、まだ階層を見られない (レベル1 だけ)。
        for i in range(1, self.n_to_overflow):
            self.assertIn(LV1_LABEL, prompts[i])
            self.assertNotIn(LV2_LABEL, prompts[i])

        # レベル2 が初めて載るのは、親の後ろにレベル1 が MIN 件並んだ次のチャンク。
        first = next(
            i for i, p in enumerate(prompts) if LV2_LABEL in p
        )
        self.assertEqual(first, self.first_lv2_prompt)

        # 最後のチャンクのプロンプト: 古い順に レベル2 の親 P が 1 件、続いて
        # 近い過去 (直前 MIN 件) がレベル1 のまま個別に載る。
        items = _context_items(prompts[-1])
        expected = [("あらすじのあらすじ", "P" + "pp")] + [
            ("あらすじ", f"C{i:02d}")
            for i in range(self.n_folded, self.n_chunks - 1)
        ]
        self.assertEqual(items, expected)
        # 直前のチャンクのあらすじが、レベル1 の札で載っている (近い過去は細かく)。
        self.assertEqual(items[-1], ("あらすじ", f"C{self.n_chunks - 2:02d}"))
        # 畳まれた子 (C00..C08) は個別には載らず、P が代弁する。
        for i in range(self.n_folded):
            self.assertNotIn(f"\nC{i:02d}", prompts[-1])

    def test_without_interleaving_no_prompt_sees_the_hierarchy(self):
        """挟み込み前 (束ねは走行の最後だけ) の回帰固定 — 同じ走行で、どの
        チャンクのプロンプトにもレベル2 は現れない。"""
        client, folded = self._run(interleave=False)
        prompts = client.chunk_prompts()
        self.assertEqual(len(prompts), self.n_chunks)
        # 束ねは最後に 1 回だけ (チャンクの合間には無い)。
        kinds = client.kinds()
        self.assertEqual(kinds[:self.n_chunks], ["chunk"] * self.n_chunks)
        self.assertGreaterEqual(folded, 1)
        for p in prompts:
            self.assertNotIn(LV2_LABEL, p)
        # 最後のチャンクは、それまでの一次あらすじを全部レベル1 のまま並べて
        # 見ている (階層が無いので 20 件を超えた瞬間から最古が落ちる形)。
        items = _context_items(prompts[-1])
        self.assertEqual(
            items,
            [("あらすじ", f"C{i:02d}") for i in range(self.n_chunks - 1)][-20:],
        )
        self.assertIn(LV1_LABEL, prompts[-1])


if __name__ == "__main__":
    unittest.main()
