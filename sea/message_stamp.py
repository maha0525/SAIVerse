"""書き込み時の機械刻印 — 生成メッセージに機械が確定的に知る事実だけを刻む。

正典: ``docs/intent/autonomous_behavior_v3.md`` §7.1 (「書き込み時 (機械)」) と
``docs/issues/memory_continuity_graph.md`` の決着節 ①。

刻むのは二つ。どちらも **LLM を呼ばない**。生成を始める瞬間・終えた瞬間に機械が
知っている値をそのまま写すだけで、解釈は一切しない。

``predecessor_message_id`` (前駆刻印)
    この生成が **実際に見ていた最後のメッセージの ID**。出どころは
    ``prepare_context`` が書き戻す ``context_meta["presented_message_ids"]``
    (= 実際にプロンプトへ組み込んだ履歴メッセージの ID 列、束 3 の契約) の末尾。
    順調な一列では刻印は物理的にひとつ前のメッセージと一致するが、ティックの
    生成中に会話が割り込むとズレ、「ログの並びでは後ろに見えるが、実際は見て
    いない」が記録に残る。

    **定義の段階**: ``predecessor_message_id`` は「SAIVerse がプロンプトとして
    組んだ `messages` (クライアントへ渡す前) の中で、記録の行 id を持つ最後の
    もの」。各クライアントが送る直前に行う変換 (本文の空な行を落とす、音声・
    動画の説明を本文に足す、tool_calls をプロバイダの形へ変換する) は対象外。
    クライアントの変換が行を落とす場合 (例: Anthropic は本文の空な tool_calls
    だけの assistant を送らない) も、``predecessor_message_id`` は SAIVerse の
    `messages` の段階で決まる。

    ``prepare_context`` は Playbook の実行 1 回につき一度しか走らないが、
    ひとつの Beat の中で追加の LLM コールを打つ経路がある — スペルループの
    ラウンド。そこでは自分が直前に永続化した行が
    プロンプトの末尾に積まれるので、次のコールの前に
    :func:`append_presented_message_id` で列を伸ばす。伸ばさないと刻印が
    組成時点の履歴末尾を指したままになり、「実際に見ていた最後」でなくなる。
    同じ Playbook の LLM ノードが続く場合も同じで、前のノードが記録へ書いた
    返事は送る `messages`に積まれ、次のノードはそれを見て書く。次のノードが送る直前に
    :func:`sync_presented_message_ids_with_sent` がその行 id を `_presented_message_ids` の末尾に足す。
    前のノードが返事を記録へ書かず、後ろの MEMORIZE / SPEAK / THINK ノードが
    ``state["last"]`` から書いた場合も、そのノードが返事の dict に行の id を載せる
    (:func:`attach_stored_id_to_unstored_reply`) ので、同じように足される。

    材料が落ちた後 (``state["last"]`` を生成以外の本文に書き換えうるノードの後で
    ``compile_playbook`` が :func:`drop_stamp_material` で落とした後) に親が
    自前の LLM ノードで送るときは、送る直前に
    :func:`sync_presented_message_ids_with_sent` が**これから送る `messages`**から
    材料を作り直す。送る `messages`で記録の行 id を持つのは履歴の行と、同じ Pulse の
    中で記録へ書いた自分の返事・スペルの周の発言 (sea/runtime_llm.py が
    保存した行 id を列の dict に載せる) なので、「親が返事 A を保存 → 子 →
    親が返事 B」でも B の前駆は A になる。

    **消費 (ズレをどう扱うか) はまだ定義しない** — まず記録する (2026-08-19
    まはー裁定)。導入前のメッセージは刻印なしで、読み手はそれを物理順とみなす
    (無害の縮退)。

``llm_tokens`` (トークン三つ組)
    その生成を作った LLM コール 1 回分の ``input`` / ``cached_input`` /
    ``output``。コール単位の詳細は中央 DB の LLMUsageLog が持ち続けるが、
    「このメッセージがどれだけ温まったコンテキストから生まれたか」は
    メッセージ自身の欄に無いと後から辿れない (v3 §9-2 の決着)。

**欠落は 0 と偽らない。** 提示ゼロの生成 (履歴なしの初手) には前駆刻印を付けず、
使用量を返さないコール (プロバイダによる) にはトークン刻印を付けない。刻印が
無いことは「そういう生成だった」という事実であって、欠測ではない。

**他人のコールの数字を自分の事実として刻まない。** 材料は「直近のコール」を
指す call-local な器なので、1 ノードの中で作り手が変わる経路では材料を動かす
責任が呼び出し元にある。三つ組は使用量を消費するたびに
:func:`record_call_tokens` で更新し (継続生成や spell の再呼び出しも例外なく)、
帰属が確定できなくなったら落として刻印なしへ倒す。

``state["last"]`` を生成以外の本文 (ツールの結果、子 Playbook の出力、エラー文)
に書き換えうるノード — ``NodeType.TOOL`` / ``TOOL_CALL`` / ``EXEC`` /
``SUBPLAY`` と、``assignments`` に ``"last"`` を持つ ``SET`` と、``action`` が
``{last}`` だけでない ``MEMORY`` (MEMORIZE) — の後では、
``compile_playbook`` (sea/langgraph_runner.py) がノード関数を包み、ノードが
どう終わっても (正常終了・早期 return・例外) :func:`drop_stamp_material` で
``_last_call_tokens`` / ``_presented_message_ids`` / ``_unstored_reply`` を
落とす。経路ごとに落とす書き方をしないのは、早期 return のエラー経路で落とし
忘れが起きたため (Codex 三巡目 4)。

落とした後に親が自前の LLM ノードを回せば、三つ組は ``record_call_tokens``
が、前駆は :func:`sync_presented_message_ids_with_sent` が載せ直すので、
刻印が消えるのは「作り手が確定できない区間」(書き換えた後の本文) だけ。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

# ---- metadata に刻むキー (message.metadata の直下) ----
PREDECESSOR_META_KEY = "predecessor_message_id"
TOKENS_META_KEY = "llm_tokens"

# ---- 材料を運ぶ state のキー (call-local。_prefix_anchor_id と同じ器) ----
PRESENTED_IDS_STATE_KEY = "_presented_message_ids"
CALL_TOKENS_STATE_KEY = "_last_call_tokens"
#: LLM ノードが ``state["_messages"]`` に足したが ``"id"`` を載せなかった返事の dict。
#: :func:`note_unstored_reply` を参照。
UNSTORED_REPLY_STATE_KEY = "_unstored_reply"
#: 直前の LLM コールの応答のメッセージごとの分け方 (sea/runtime_llm.py の
#: ``_store_output_phases_in_state`` が書く)。SPEAK ノードが消費し、THINK ノードと
#: action が ``{last}`` の MEMORIZE ノードは読むだけ (SAY ノードは触らない)。
OUTPUT_PHASES_STATE_KEY = "_output_phases"

# ---- 三つ組の欄名 ----
TOKENS_INPUT_KEY = "input"
TOKENS_CACHED_INPUT_KEY = "cached_input"
TOKENS_OUTPUT_KEY = "output"


def _as_token_count(value: Any) -> Optional[int]:
    """トークン数として読めるなら int で返す。読めなければ None。

    bool は int の派生だが個数ではないので弾く。負値も弾く (プロバイダが
    「不明」を -1 で返した場合に 0 件と偽らないため)。
    """
    if isinstance(value, bool):
        return None
    if not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def normalize_token_triple(usage: Any) -> Optional[Dict[str, int]]:
    """``UsageInfo`` から正規化した三つ組を作る。取れなければ None。

    三つとも読めたときだけ返す — 一つでも欠けた三つ組は、欠けた欄を 0 と
    読まれて「キャッシュが効かなかった」の顔をしてしまう。
    """
    if usage is None:
        return None
    input_tokens = _as_token_count(getattr(usage, "input_tokens", None))
    output_tokens = _as_token_count(getattr(usage, "output_tokens", None))
    cached_tokens = _as_token_count(getattr(usage, "cached_tokens", None))
    if input_tokens is None or output_tokens is None or cached_tokens is None:
        return None
    return {
        TOKENS_INPUT_KEY: input_tokens,
        TOKENS_CACHED_INPUT_KEY: cached_tokens,
        TOKENS_OUTPUT_KEY: output_tokens,
    }


def clear_call_tokens(state: Optional[Dict[str, Any]]) -> None:
    """三つ組の材料を落とす (この state からは帰属を確定できない、の印)。

    使うのは二場面。① 使用量を返さなかったコールの直後
    (:func:`record_call_tokens` 経由)。② ``state["last"]`` を生成以外の本文に
    書き換えうるノードの後 — ``compile_playbook`` が :func:`drop_stamp_material`
    経由で呼ぶ。例えば子 Playbook の出力を親の ``state["last"]`` に中継する
    ``lg_subplay_node`` の後で、中継された本文を書いたのは子の LLM コール、
    親 state に残っている三つ組は親の直前コールのもの。残したまま親の SPEAK /
    MEMORIZE が刻むと、他人のコールの数字がその本文の事実として記録される。

    落とした後の親が自前の LLM ノードを回せば ``record_call_tokens`` が
    改めて三つ組を載せるので、消えるのは「作り手が確定できない区間」だけ。
    """
    if not isinstance(state, dict):
        return
    state.pop(CALL_TOKENS_STATE_KEY, None)


def clear_presented_message_ids(state: Optional[Dict[str, Any]]) -> None:
    """前駆の材料を落とす (この state からは帰属を確定できない、の印)。

    :func:`clear_call_tokens` と**必ず対で呼ぶ** (対にしたものが
    :func:`drop_stamp_material` で、``compile_playbook`` が呼ぶ)。理由が同じ
    だから — 本文の作り手が変わった区間では、三つ組が他人のコールのものになる
    のと同じ理屈で、前駆も他人の `_presented_message_ids` になる。

    例えば子 Playbook の中継 (``lg_subplay_node``)。子は自前の
    ``_prepare_context`` で別の履歴を見て本文を書くが、親 state に残っている
    のは**親が見せた列**の末尾。落とさずに親の SPEAK / MEMORIZE が刻むと、
    その本文を書いた LLM が**一度も見ていないメッセージ**を前駆として記録する
    — 欠落ではなく捏造になる (2026-08-22 掃討フェーズ 束 5 指摘 1)。

    子の提示列を親へ引き継ぐ案を採らないのは三つ組と同じ理由 — ``last_text``
    は子の出力列の末尾で、子の最後のコールがその本文を書いたとは限らず、
    子の final_state も親へ届かない。確定できないので刻印なしへ倒す。

    落とした後に親が自前の LLM ノードで送るときは、送る直前に
    :func:`sync_presented_message_ids_with_sent` が送る `messages`から材料を作り
    直す — :func:`clear_call_tokens` の三つ組を ``record_call_tokens`` が
    載せ直すのと対になる。だから消えるのは「作り手が確定できない区間」
    (中継された本文を親の SPEAK / MEMORIZE が刻む場面) だけで、親が自分で
    書いた次の返事には前駆が付く。
    """
    if not isinstance(state, dict):
        return
    state.pop(PRESENTED_IDS_STATE_KEY, None)


def drop_stamp_material(state: Optional[Dict[str, Any]]) -> None:
    """``_last_call_tokens`` / ``_presented_message_ids`` / ``_unstored_reply`` /
    ``_output_phases`` を落とす。

    呼ぶのは ``compile_playbook`` (sea/langgraph_runner.py) だけ。
    ``state["last"]`` を生成以外の本文に書き換えうるノード (``NodeType.TOOL`` /
    ``TOOL_CALL`` / ``EXEC`` / ``SUBPLAY`` と、``assignments`` に ``"last"`` を
    持つ ``SET`` と、``action`` が ``{last}`` だけでない ``MEMORY``) の関数を包み、ノードが終わった後 (正常終了・早期 return・例外の
    どれでも) に呼ぶ。

    - ``_last_call_tokens`` / ``_presented_message_ids``: 後ろの MEMORIZE /
      SPEAK / THINK が書き換えた後の本文に、直前の LLM コールの三つ組と前駆を
      刻まないため (:func:`clear_call_tokens` / :func:`clear_presented_message_ids`)。
    - ``_unstored_reply``: 後ろのノードが書き換えた後の本文を保存したとき、その
      行の id を前の LLM ノードの返事の dict に載せないため
      (:func:`unstored_reply_for_last`)。書き換えた後の本文が返事と同じ文字列
      でも、それは返事の本文ではない。
    - ``_output_phases``: 直前の LLM コールの応答のメッセージごとの分け方
      (llm_clients.output_phases)。後ろの SPEAK は ``state["last"]`` と結合が
      一致すれば記憶に載せるので、書き換えた後の本文が偶然同じ文字列だと、
      別の生成の本文に直前のコールの分け方が付く。
    """
    clear_call_tokens(state)
    clear_presented_message_ids(state)
    clear_unstored_reply(state)
    if isinstance(state, dict):
        state.pop(OUTPUT_PHASES_STATE_KEY, None)


def record_call_tokens(state: Optional[Dict[str, Any]], usage: Any) -> None:
    """LLM コール 1 回分の三つ組を state に残す (直近のコール = この生成)。

    **usage が取れなかったときは前の値を消す。** 消さずに残すと、使用量を
    返さなかったコールの生成メッセージに一つ前のコールの数字が乗り、他人の
    記帳を自分の事実として刻むことになる (fail-closed 側へ倒す)。

    そのため呼び出し側は「usage の有無に関わらず必ず一度呼ぶ」こと。
    """
    if not isinstance(state, dict):
        return
    triple = normalize_token_triple(usage)
    if triple is None:
        clear_call_tokens(state)
        return
    state[CALL_TOKENS_STATE_KEY] = triple


def record_presented_message_ids(
    state: Optional[Dict[str, Any]], context_meta: Optional[Dict[str, Any]],
) -> None:
    """``prepare_context`` の out-param から実提示 ID 列を state に移す。

    ``presented_message_ids`` キー自体が無いのは「履歴組成が失敗した」の印
    (束 3 の契約)。その場合も空の場合も刻印なしに倒す — 見ていたものを
    確定できない生成に前駆を書くことはできない。
    """
    if not isinstance(state, dict):
        return
    presented = None
    if isinstance(context_meta, dict):
        presented = context_meta.get("presented_message_ids")
    if isinstance(presented, list) and presented:
        state[PRESENTED_IDS_STATE_KEY] = [str(mid) for mid in presented if mid]
    else:
        state.pop(PRESENTED_IDS_STATE_KEY, None)


def replace_presented_message_ids(
    state: Optional[Dict[str, Any]], message_ids: Any,
) -> None:
    """実際に送る列から作り直した ID 列で、前駆の材料を置き換える。

    使うのは、ターンの最初の送信を記録に揃えた列 (リアルタイム情報と自動想起を
    残すモデルの、トップレベルの残すパルス) の送信ごとの一点
    (sea/runtime_llm._refresh_presented_ids_for_aligned_send。docs/intent/
    persist_realtime_and_recall.md §5)。揃えると、``prepare_context`` の後に
    記録へ書かれた行 (事前スペル・TOOL の結果・MEMORIZE・状況変化の記録) が
    送る列に入るので、組成時点の列のままでは「実際に見ていた最後」ではなく
    なる。空なら材料を落とす (提示ゼロの生成には前駆を付けない)。
    """
    if not isinstance(state, dict):
        return
    ids = [str(mid) for mid in (message_ids or []) if mid]
    if ids:
        state[PRESENTED_IDS_STATE_KEY] = ids
    else:
        state.pop(PRESENTED_IDS_STATE_KEY, None)


def append_presented_message_id(
    state: Optional[Dict[str, Any]], message_id: Optional[str],
) -> None:
    """同じ Beat の中で永続化した行を `_presented_message_ids`の末尾に足す。

    ``prepare_context`` は Playbook の実行 1 回につき一度しか走らないので、
    ひとつの Beat の中で追加の LLM コールを打つ経路 — スペルループの
    ラウンド — では、直前に永続化した自分の行が
    プロンプトの末尾に足されているのに、state の列は組成時点の履歴末尾を
    指したままになる。前駆刻印は「この生成が実際に見ていた最後のメッセージ
    の ID」なので、次のコールの前にここで足す。

    **足してよいのは永続 ID を持つ行だけ。** スペル結果の整形テキストや
    知覚の注入行のようにプロンプトへ入っても行 ID を持たない挿入物は
    対象外で、その場合の前駆は「ID を持つ最後の行」= 直前に足した自分の
    行になる。
    """
    if not isinstance(state, dict) or not message_id:
        return
    presented = state.get(PRESENTED_IDS_STATE_KEY)
    if not isinstance(presented, list):
        presented = []
    state[PRESENTED_IDS_STATE_KEY] = [*presented, str(message_id)]


def sync_presented_message_ids_with_sent(
    state: Optional[Dict[str, Any]], messages: Optional[Sequence[Dict[str, Any]]],
) -> None:
    """これから送る `messages`に合わせて `_presented_message_ids`を整える (作り直す / 末尾を伸ばす)。

    使うのは LLM ノードが送る直前 (送る `messages`が確定した後の一点)。この生成が見る
    のはまさにこれから送る `messages`なので、そこにある記録の行 id
    (:func:`sea.runtime_context.sent_message_ids`) を材料に使う。

    **`messages` は SAIVerse がプロンプトとして組んだもの (クライアントへ渡す前)。**
    ``predecessor_message_id`` は「この `messages` の中で記録の行 id を持つ最後の
    もの」と定義する。``prepare_context`` が組んだ行の一覧
    (``context_meta["presented_message_ids"]``) から取っていた定義と同じ段階で、
    各クライアントが送る直前に行う変換 — 本文の空な行を落とす、音声・動画の説明を
    本文に足す (``_inject_unsupported_media_summaries``)、tool_calls をプロバイダの
    形へ変換する — は対象外。クライアントの変換がある行を落とす場合でも
    (例: Anthropic の ``_prepare_anthropic_messages`` と xAI の
    ``_build_xai_messages`` は本文の空な tool_calls だけの assistant を送らない)、
    ``predecessor_message_id`` は SAIVerse の `messages` の段階で決まり、その行を
    指しうる。

    - **材料が無い** (キー不在・空・list でない) とき: 送る `messages`の記録の行 id を
      並び順に材料にする。``state["last"]`` を生成以外の本文に書き換えうる
      ノードの後で ``compile_playbook`` が :func:`drop_stamp_material` で落とした
      回 — 子 Playbook の中継の後に親が自前の LLM ノードで生成する回がこれに
      あたる。三つ組を ``record_call_tokens`` が載せ直すのと対になる口。
    - **材料がある** とき: 書き換えず、送る `messages`で材料の末尾より**後ろ**にある
      記録の行だけを末尾に足す。同じ Playbook の前の LLM ノードが記録へ書いた
      返事 (sea/runtime_llm.py が送る `messages`のその行に記録の id を載せる) がこれで、
      この生成はそれを見て書く。足すのは送る時点だけ — 保存の時点で足すと、
      間に挟まる MEMORIZE / SPEAK / THINK (その返事の本文を書き直す) の前駆が
      生成自身の行を指す。``prepare_context`` の提示列や、スペルの周で
      :func:`append_presented_message_id` が伸ばした列の中身は変えない。
    - **材料の末尾が送る `messages`に無い** とき: 材料はこの生成が見るものを表して
      いないので、送る `messages`から作り直す (残すと見ていない行を前駆に刻む)。

    **本文が空の tool_calls だけの返事**: ``_finalize_beat`` (sea/runtime_llm.py)
    は、この返事にも記録の行 id があれば ``"id"`` を載せる。この dict は SAIVerse の
    `messages` に入っているので、送る先のクライアントに関わらず数える (上の定義)。

    LLM ノードが記録へ書かなかった返事を、後ろの MEMORIZE / SPEAK / THINK ノードが
    ``state["last"]`` から記録へ書いた場合は、そのノードが書いた行の id を返事の
    dict に ``"id"`` として載せる (:func:`note_unstored_reply` /
    :func:`attach_stored_id_to_unstored_reply`)。載せるのは ``_unstored_reply`` が
    残っていて (間に ``state["last"]`` を書き換えうるノードが走っていない —
    走ると ``compile_playbook`` が :func:`drop_stamp_material` で消す)、
    ``state["last"]`` が返事の dict の ``content`` と値で等しいときだけ。
    ``action`` が ``{last}`` だけでない MEMORIZE (例: ``{other}``、
    ``"前置き: {last}"``) も ``state["last"]`` を作り直すノードなので、その後でも
    ``compile_playbook`` が ``_unstored_reply`` を消し、載らない (作り直した値が
    返事と同じ文字列でも)。載らないとき、次の生成の前駆はそれより前の記録の行を
    指す (見ていない行ではなく、手前の行)。

    `messages` が空、または記録の行 id が一つも無いときは、既存の
    `_presented_message_ids` を :func:`clear_presented_message_ids` で消す (提示ゼロの
    生成は刻印なし)。残すと、この生成が見ていない行を前駆に刻む。``state`` が
    dict でないときは何もしない。
    """
    if not isinstance(state, dict):
        return
    from sea.runtime_context import sent_message_ids

    sent = sent_message_ids(messages) if messages else []
    if not sent:
        clear_presented_message_ids(state)
        return
    current = state.get(PRESENTED_IDS_STATE_KEY)
    if not isinstance(current, list) or not current:
        state[PRESENTED_IDS_STATE_KEY] = sent
        return
    tail = str(current[-1])
    if tail not in sent:
        # 材料の末尾がこれから送る `messages`に無い = 材料はこの生成が見るものを表して
        # いない (例: 親の state を受け取って走った子 Playbook の `_presented_message_ids`が
        # 残っている)。残すと見ていない行を前駆に刻むので、送る `messages`から作り直す。
        state[PRESENTED_IDS_STATE_KEY] = sent
        return
    # 末尾は送る `messages`の中で最後に現れた位置で取る (同じ id が二度並ぶことは
    # 無いはずだが、あっても「見た最後」から先だけを足す)。
    after = sent[len(sent) - 1 - sent[::-1].index(tail) + 1:]
    if after:
        state[PRESENTED_IDS_STATE_KEY] = [*current, *after]


def note_unstored_reply(
    state: Optional[Dict[str, Any]], reply_message: Dict[str, Any],
) -> None:
    """``"id"`` を載せずに ``state["_messages"]`` に足した返事の dict を書いておく。

    呼ぶのは ``sea/runtime_llm._finalize_beat`` だけ — その LLM ノードが返事を
    記憶に保存しなかった (``"id"`` を載せなかった) が、本文は空でなく、
    ``state["last"]`` と同じ文字列のとき。``reply_message`` は
    ``state["_messages"]`` に足した dict そのもの。

    後ろの MEMORIZE / SPEAK / THINK ノードが ``state["last"]`` を記憶に保存したら、
    :func:`attach_stored_id_to_unstored_reply` がこの dict に行の id を載せる。
    載せないと、次の LLM ノードが送る ``messages`` でこの dict が記録の行として
    数えられず (:func:`sync_presented_message_ids_with_sent`)、次の返事の
    ``predecessor_message_id`` が保存された行より前の行になる。

    ``state["last"]`` を生成以外の本文に書き換えうるノードが間に走ったら、
    ``compile_playbook`` が :func:`drop_stamp_material` でこれを消す。
    """
    if not isinstance(state, dict) or not isinstance(reply_message, dict):
        return
    state[UNSTORED_REPLY_STATE_KEY] = reply_message


def clear_unstored_reply(state: Optional[Dict[str, Any]]) -> None:
    """:func:`note_unstored_reply` で書いたものを消す。

    ``_finalize_beat`` が返事に ``"id"`` を載せたとき・本文が空のとき、
    ``lg_llm_node`` の入り口 (前のノードの返事を次の返事と取り違えない)、
    :func:`drop_stamp_material` (``compile_playbook`` が呼ぶ) で呼ぶ。
    """
    if not isinstance(state, dict):
        return
    state.pop(UNSTORED_REPLY_STATE_KEY, None)


def unstored_reply_for_last(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """``state["last"]`` が :func:`note_unstored_reply` で書いた返事の本文なら、
    その返事の dict を返す。そうでなければ None。

    条件は三つ。① ``_unstored_reply`` が残っている — 間に ``state["last"]`` を
    生成以外の本文に書き換えうるノード (TOOL / TOOL_CALL / EXEC / SUBPLAY /
    ``"last"`` を代入する SET / ``action`` が ``{last}`` だけでない MEMORIZE) が
    走っていない (走ると ``compile_playbook`` が消す)。② ``state["last"]`` が返事の dict の ``content`` と値で等しい。
    ③ 返事の dict にまだ ``"id"`` が無い。

    オブジェクトの同一性 (``is``) は使わない。独立に返された同じ短い文字列
    (例: ``"1"``) は Python では同じオブジェクトになりうるので、同一性は
    生成元が同じことの証明にならない (Codex 三巡目 1)。
    """
    if not isinstance(state, dict):
        return None
    reply = state.get(UNSTORED_REPLY_STATE_KEY)
    if not isinstance(reply, dict) or "id" in reply:
        return None
    content = reply.get("content")
    if not isinstance(content, str) or state.get("last") != content:
        return None
    return reply


def attach_stored_id_to_unstored_reply(
    state: Optional[Dict[str, Any]], message_id: Any,
) -> bool:
    """``state["last"]`` を記憶に保存した行の id を、その返事の dict に ``"id"`` として載せる。

    呼ぶのは ``state["last"]`` をそのまま本人の発言として記憶に保存したノード
    (MEMORIZE の ``action`` が ``{last}`` だけのとき / SPEAK / THINK) が、保存に
    成功した直後 (``state["last"]`` を書き換える前)。載せたら
    ``UNSTORED_REPLY_STATE_KEY`` を消す。``message_id`` が空でない文字列でないとき、
    :func:`unstored_reply_for_last` が None のときは何もしない。戻り値は載せたか。

    ``_presented_message_ids`` には足さない — 足すのは次の LLM ノードが送る直前
    (:func:`sync_presented_message_ids_with_sent`)。ここで足すと、同じ
    ``state["last"]`` を続けて保存する別のノードの ``predecessor_message_id`` が、
    その本文の行自身を指す。
    """
    if not isinstance(message_id, str) or not message_id:
        return False
    reply = unstored_reply_for_last(state)
    if reply is None:
        return False
    reply["id"] = message_id
    clear_unstored_reply(state)
    return True


def resolve_predecessor_message_id(state: Optional[Dict[str, Any]]) -> Optional[str]:
    """この生成が実際に見ていた最後のメッセージ ID。無ければ None。"""
    if not isinstance(state, dict):
        return None
    presented = state.get(PRESENTED_IDS_STATE_KEY)
    if not isinstance(presented, list) or not presented:
        return None
    last = presented[-1]
    if not last:
        return None
    return str(last)


def resolve_token_triple(state: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """この生成を作ったコールの三つ組。無ければ None。"""
    if not isinstance(state, dict):
        return None
    triple = state.get(CALL_TOKENS_STATE_KEY)
    if not isinstance(triple, dict) or not triple:
        return None
    return dict(triple)


def build_generation_stamp(state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """state から刻印 dict を組む。刻めるものが無ければ空 dict。"""
    stamp: Dict[str, Any] = {}
    predecessor = resolve_predecessor_message_id(state)
    if predecessor:
        stamp[PREDECESSOR_META_KEY] = predecessor
    triple = resolve_token_triple(state)
    if triple:
        stamp[TOKENS_META_KEY] = triple
    return stamp


def stamp_generation_metadata(
    metadata: Optional[Dict[str, Any]], state: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """metadata dict に刻印を足して返す (元の dict は変更しない)。

    刻むものが無ければ引数をそのまま返す (``None`` は ``None`` のまま —
    「metadata 無し」の呼び出しを空 dict に変えない)。既に同じキーがある
    場合は上書きしない: 呼び出し元が明示した値の方が具体的な事情を知っている。
    """
    stamp = build_generation_stamp(state)
    if not stamp:
        return metadata
    merged: Dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}
    for key, value in stamp.items():
        merged.setdefault(key, value)
    return merged
