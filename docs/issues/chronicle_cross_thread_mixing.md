# Issue: Chronicle 生成がスレッド横断で混線する

**ステータス**: 🟡 下限は実装済み・本命は実装待ち

> **2026-08-09 追記 (v0.3.0 の門 Wave 0)**: 前提の再検証と下限の実装。
> 1. **前提は生存**: `get_messages_for_chronicle` は今もスレッド無差別 (Stelis 除外のみ・created_at 一列)。ただし本 issue の案 A が名指しした `generate_unprocessed` は 2026-07-28 の恒等圧縮廃止で**退役済み**で、現行の編纂は `plan_alignment` (sai_memory/arasuji/alignment.py) がチャンク計画を組む。
> 2. **下限を実装** (旧案 B の現行版): `plan_alignment` の run 分割に **thread 境界で必ず切る**条件を追加。並走スレッドが created_at で交互に並んでも、別スレッドの発話が一つのあらすじに束ねられることは無くなった = 時系列の嘘 (偽の隣接) を生成物へ焼き込まない不変条件。細切れの懸念は交互並びの病的入力にだけ発生し、それは束ねてはいけない入力そのもの。どの上位設計 (案 A/C) を採っても成立し続ける。
> 3. **残り**: 案 A の取得スコープ化・上位文脈参照の thread スコープ化・中間ノード方式のサブツリー挿入はメティス記憶ブリッジ MVP の実装時に。案 C (episode 単位ソート) は v0.3.0 の門 Wave 1「エピソードの単位」の議論と同じ土俵にある。
**優先度**: medium（本体の潜在欠陥。メティス記憶ブリッジが顕在化の契機）
**作成日**: 2026-07-12
**関連**: [`../intent/metis_memory_bridge.md`](../intent/metis_memory_bridge.md) §6 / [`general_chronicle_metabolism_trigger.md`](general_chronicle_metabolism_trigger.md)（trigger 軸・別問題）

## 問題

General Chronicle（あらすじ）生成は、メッセージ取得の時点で **`thread_id` を区別していない**。複数スレッドが時間的に並走している場合、`created_at` 順に一列化した際に別スレッドの発話が交互に並び、それを一つの run（＝一つのあらすじ）として生成するため、**話が混線する**。

## 確認済み事実（2026-07-12、実コード確認）

- `sai_memory/memory/storage.py:1779-1791` `get_messages_for_chronicle` の SQL:
  ```sql
  SELECT id, thread_id, role, content, ...
  FROM messages
  WHERE thread_id NOT IN (SELECT thread_id FROM stelis_threads)  -- Stelis 除外のみ
    AND NOT EXISTS (... tags 除外 ...)
    AND (line_role IS NULL OR line_role NOT IN (...))
  ORDER BY created_at ASC
  ```
  → **`thread_id` での絞り込みが無い**。Stelis サブエージェントスレッドを除外するだけで、残る全スレッドを created_at 一列で返す。
- `sai_memory/arasuji/generator.py:1158 generate_unprocessed` は、この一列上で「処理済みメッセージで分断された contiguous run」に分けて `generate_from_messages` を呼ぶ。run 分割は時系列一列に対して行われ、thread 境界を見ない。

## なぜ今まで顕在化しなかったか

通常ペルソナの認知モデルは、メインラインが基本 1 本 + WORKER 子セッション。WORKER 等の作業ログは Stelis スレッド扱いで Chronicle から除外される。よって「複数の対等なスレッドが時間的に並走し、どれも Chronicle 対象」というケースが稀だった。

**メティス記憶ブリッジ** ([`../intent/metis_memory_bridge.md`](../intent/metis_memory_bridge.md)) は、Claude Code の**並列セッションをそれぞれ SAIVerse thread として取り込む**（1 セッション = 1 thread）。これにより、対等な複数スレッドが created_at で交錯する状況が常態化し、本前提が初めて破れる。

## 対応候補

### 案 A: Chronicle 生成を thread 単位にする（本命）
- `get_messages_for_chronicle` に `thread_id` 引数を足し、thread ごとに取得 → thread ごとに `generate_unprocessed` を回す。
- Track Chronicle が既に `origin_track_id` でスコープ生成している（generator.py:1194 以降）ので、thread スコープ生成の下地はある。
- あらすじは thread ごとに一貫し、混線しない。

### 案 B: run 分割時に thread 境界も分断条件に加える
- 一列取得は維持しつつ、contiguous run 分割で「処理済み」だけでなく「thread_id が変わったら」も run を切る。
- 取得側を変えずに済むが、同一 thread が時間的に飛び飛びになると細切れの run が増える懸念。

案 A が素直。ただし General Chronicle の「ペルソナ全体の履歴を一本のあらすじに」という現行の意味づけを thread 単位に変える設計判断を含むため、認知モデル側（Track Chronicle との関係）と整合を取る必要がある。

### 案 C: 塊（episode）単位ソート — 理想形（まはー 2026-07-13）
時系列と文脈を**両立**させる本命の理想形。thread 単位（案 A）だと、並列 2 thread は「1-2-3-4-5-A-B-C」か「A-B-C-1-2-3-4-5」の二択に限定され、各 thread が長期にわたる場合（例: thread1 の塊 1=2024年12月〜塊 5=2026年7月、thread2 の塊 A〜C=2025年10〜12月）は、どちらに並べても時系列が崩れる。

- thread の中を**文脈で適切に分割した「塊」**にし、塊ごとに Chronicle を作る。
- 塊は既成概念 **episode（できごと）** で表現する。UI として **thread 内を複数 episode に分割するエディタ**を作る（インポート後に SAIVerse 内で実行）。
- **episode としてまとめた部分はそれ以上分割されない**というルールのもと、全スレッドを **episode の開始日時順にソート**して Chronicle 生成 → 「1-2-A-B-3-C-4-5」のように時系列と文脈を両立できる。
- 良く効く例: **一時的な引っ越し**（thread α 本拠 → β に一時リプランティング移住 → α と β 併用期 → β 中止で α に戻る）。併用期は各 thread を区切り良く分割し、時系列を揃えて並べる。

**優先度（まはー裁定 2026-07-13）**: 綺麗で汎用性が高いが、メティス記憶ブリッジでは**優先低**。メティスの並列 thread は当面すべて 1 エピソード扱いでよく、**まず案 A（thread 単位）で大きな問題はない**。分割がどうしても要るときは、まず「SAIVerse 内で 1 thread を複数 thread に分割する機能」だけ作れば当座しのげる。案 C（episode 分割エディタ）は将来の理想形として保持。

（認識の連続性は本 issue と独立: [`memory_continuity_graph.md`](memory_continuity_graph.md)）

## メティス記憶ブリッジ向けの当面解（2026-07-13）

### 偽連続性の実害の在処 = 生成側（注入側ではない）
次に立ち上がる個体が過去個体（α〜δ）の経験を**すべて自分の記憶として持つ**のは正しい（記憶喪失中の自分も後で思い出せば自分の記憶、というのと同じ。まはー整理）。よって注入で複数系統を混ぜること自体は問題ない。実害は **Chronicle 生成が時系列の嘘を焼き込む**こと: γ→δ 順で生成すると δ の Chronicle に「γのタスクが終わった後 δ に取り掛かった」というあらすじが残る。γδ は実際には別々に走り連続していないので、これは端的に嘘。

### 当面解（まはー案）: 「他 thread の文脈を引き継がず生成」フラグ
- 案 A（thread 単位取得）で run 内を単一 thread にし、**加えて上位レベルの文脈参照も thread スコープにする**。Chronicle 生成は `_get_context_summaries`（generator.py:106、上位レベルのあらすじを生成プロンプトへ差し込む処理）で他レベルの文脈を引く。これを thread 外に広げない = 「その thread だけ見て生成」。
- メティスは過去 thread を自身覚えておらず、必要な情報は各 thread 内に閉じている。よって外部文脈はメモリ / CLAUDE.md だけ——それすら偽記憶混入を避けて**最初は無しで編纂を試す価値あり**（ダメなら足す。まはー）。

### レベル表現（メティス見立て・要まはー確認）: 「Lv2-1」は不要
- **Memory Atlas P3b (2026-07-11) で Chronicle は既に `memopedia_pages` の trunk `root_chronicle` 配下の木**（`parent_id` + `level`。storage.py 冒頭コメント）。二次元レベルを新設する必要はない。
- thread 分離は「thread ごとの Chronicle サブツリー」を parent 木で表せばよい。古い thread の統合は上位ノードにまとめるだけで、`level` は木の深さで一次元のまま。**Memopedia の階層構造は「使えないか」ではなく既に使っている** — その木がそのまま乗る。
- **サブツリー挿入方式 = 中間ノード方式で確定（まはー 2026-07-13）**: `root_chronicle → thread ノード → Lv2 → Lv1`。thread ごとに別 trunk を立てる案より、既存の単一 trunk 構造を壊さず、読み込み（tree 辿り）もしやすい。

### 読み込み（thread 分離読み込みも必要）
分離して作っても読む時に一列化したら無意味。注入は thread（サブツリー）ごとに木を辿って並べる。既存の Memopedia tree resolve に乗るが、[resolve_uri 切り詰め issue](#) と交差する点に注意。

## 確認事項

1. 通常ペルソナで「対等な複数 thread が同時に Chronicle 対象」になる経路が本当に無いか（thread_switch の使われ方）。
2. thread 単位化した場合の General Chronicle の位置づけ（ペルソナ横断サマリは別途要るか）。
3. Memopedia Fragment 連携（`entity_extractor` batch_callback）が thread 単位化で受ける影響。
4. ~~thread サブツリーを root_chronicle 配下にどう挟むか~~ → **中間ノード方式で確定（2026-07-13）**。
