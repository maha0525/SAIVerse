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

    ``prepare_context`` は Playbook の実行 1 回につき一度しか走らないが、
    ひとつの Beat の中で追加の LLM コールを打つ経路がある — スペルループの
    ラウンド。そこでは自分が直前に永続化した行が
    プロンプトの末尾に積まれるので、次のコールの前に
    :func:`append_presented_message_id` で列を伸ばす。伸ばさないと刻印が
    組成時点の履歴末尾を指したままになり、「実際に見ていた最後」でなくなる。

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
帰属が確定できなくなったら :func:`clear_call_tokens` で落として刻印なしへ倒す
— 子 Playbook の出力を親の ``state["last"]`` に中継する ``lg_subplay_node``
がその場面で、子のコールの三つ組は子 state に閉じていて親へは戻らない。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# ---- metadata に刻むキー (message.metadata の直下) ----
PREDECESSOR_META_KEY = "predecessor_message_id"
TOKENS_META_KEY = "llm_tokens"

# ---- 材料を運ぶ state のキー (call-local。_prefix_anchor_id と同じ器) ----
PRESENTED_IDS_STATE_KEY = "_presented_message_ids"
CALL_TOKENS_STATE_KEY = "_last_call_tokens"

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
    (:func:`record_call_tokens` 経由)。② **本文の作り手が変わった**
    直後 — 子 Playbook の出力を親の ``state["last"]`` に中継する
    ``lg_subplay_node`` がこれにあたる。中継された本文を書いたのは子の
    LLM コールで、親 state に残っている三つ組は親の直前コールのもの。
    残したまま親の SPEAK / MEMORIZE が刻むと、他人のコールの数字が
    その本文の事実として記録される。

    落とした後の親が自前の LLM ノードを回せば ``record_call_tokens`` が
    改めて三つ組を載せるので、消えるのは「作り手が確定できない区間」だけ。
    """
    if not isinstance(state, dict):
        return
    state.pop(CALL_TOKENS_STATE_KEY, None)


def clear_presented_message_ids(state: Optional[Dict[str, Any]]) -> None:
    """前駆の材料を落とす (この state からは帰属を確定できない、の印)。

    :func:`clear_call_tokens` と**必ず対で呼ぶ**。理由が同じだから — 本文の
    作り手が変わった区間では、三つ組が他人のコールのものになるのと同じ理屈で、
    前駆も他人が見た列になる。

    具体的には子 Playbook の中継 (``lg_subplay_node``)。子は自前の
    ``_prepare_context`` で別の履歴を見て本文を書くが、親 state に残っている
    のは**親が見せた列**の末尾。落とさずに親の SPEAK / MEMORIZE が刻むと、
    その本文を書いた LLM が**一度も見ていないメッセージ**を前駆として記録する
    — 欠落ではなく捏造になる (2026-08-22 掃討フェーズ 束 5 指摘 1)。

    子の提示列を親へ引き継ぐ案を採らないのは三つ組と同じ理由 — ``last_text``
    は子の出力列の末尾で、子の最後のコールがその本文を書いたとは限らず、
    子の final_state も親へ届かない。確定できないので刻印なしへ倒す。
    """
    if not isinstance(state, dict):
        return
    state.pop(PRESENTED_IDS_STATE_KEY, None)


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


def append_presented_message_id(
    state: Optional[Dict[str, Any]], message_id: Optional[str],
) -> None:
    """同じ Beat の中で永続化した行を「見た列」の末尾に足す。

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
