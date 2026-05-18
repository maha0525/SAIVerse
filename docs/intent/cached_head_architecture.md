# Intent Document: Cached Head Architecture

**ステータス**: 起草中 (v0.1, 2026-05-13)
**位置付け**: foundational refactor。Phase 4' (`stackchan_vessel.md` A-3-c) で発覚した「Building 移動による spell 一覧変動が prompt cache を壊す」問題を、SAIVerse 全体の context 構築の原則として物理的に保証する仕組みに昇格させるための設計。
**前提**: `dynamic_state_sync.md` (A/B/C 状態モデル) / `persona_cognition/01_concepts.md` (ライン構造) / `persona_cognition/03_data_model.md` (METABOLISM_ANCHORS の per-model 設計)

---

## 1. これは何か / 何でないか

### これは何か

LLM に送るコンテキストの **head 部分 (= prompt cache が効く先頭領域)** を、構造化された Section の集合として組み立てる仕組み。各 Section は frozen snapshot からのみ render され、live state を直接参照することを型レベルで禁じる。snapshot は明示的な再構築イベント (Metabolism 等) でしか更新されないため、cache の効いている間は head が変動しない。

snapshot の値が live state から逸脱したとき (= 世界状態が変わったが head はまだ古い snapshot のとき)、各 Section が自身の差分検出ロジックで「末尾通知ラベル」を生成し、pipeline がそれを会話履歴末尾に注入する。これによりペルソナは head の古さを認識した上で新しい状態を知ることができる。

### これは何でないか

- 既存の `dynamic_state_sync.md` の置き換えではない。dynamic_state が扱う Building 世界状態 (items / occupants / memopedia / chronicle) は本機構の Section の一つとして取り込まれる
- LLM 呼び出し全体の cache 管理機構ではない。head 部分の構築方法を縛るだけで、conversation history 部分や Metabolism anchor 周りは既存機構をそのまま使う
- 新規の認知モデルではない。`persona_cognition/01_concepts.md` のライン構造の上に乗る実装基盤

### なぜ必要か

`persona_cognition/01_concepts.md` の不変条件 7「キャッシュヒット継続を最優先」を、コード上で**気をつけて守る** のではなく**物理的に違反不能にする** ため。

現状、`sea/runtime_context.py` は live state (`SPELL_TOOL_SCHEMAS` dict、`persona.current_building_id`、addon load 状態、building system instruction の DB 直読み等) を直接参照して system prompt の各 section をリクエスト毎に組み立てている。各 section の入力が偶然安定しているから cache が効いているだけで、誰かが「現在の building を反映してこう」と修正を加えると即 cache 破壊につながる (Phase 4' / A-3-c で実際にやらかした)。

ドキュメントや memory に「気をつけろ」と書くだけでは、新規 section 追加時に必ず守られる保証はない。section の関数シグネチャレベルで live state アクセスを不可能にし、差分通知の責務もインターフェース契約に組み込むのが本機構の目的。

---

## 2. 設計原則

### head は snapshot 経由でのみ構築される

各 Section は `SectionSnapshot` (構造化された frozen 値) を入力にとり、`RenderedSection` (テキスト + media のセット) を出力する `render` 関数を持つ。`render` の引数は snapshot のみ。live state (`SPELL_TOOL_SCHEMAS` 等の global、persona 属性、DB) には触れない。

これにより「render 関数のシグネチャを見るだけで live state 依存があるかどうかが分かる」状態になる。新規 section 追加時に live state を取り出そうとすると、そもそも引数に渡されてないので物理的に書けない。

### snapshot は明示的なイベントでしか更新されない

`SectionSnapshot` は `capture(LineHeadInput)` で 1 回作られ、その後は frozen。snapshot 更新タイミングは以下のみ:

- **Metabolism** (= 既存の anchor 更新タイミング、最も基本的なリセット点)
- **Section が `refresh_on_events` で明示宣言したイベント** (例: visual context section が building entry を宣言する等)

それ以外の場面では snapshot は変更不可。section の `render` を何回呼んでも同じ snapshot を見るので同じ文字列が返る = head が安定して cache hit が継続する。

### 差分通知は Section interface に畳み込む

各 Section は `diff_to_notifications(old_snapshot, new_snapshot) -> list[NotificationLabel]` を必ず実装する。これがない section は registry に登録できない。これにより「render は書いたが差分通知忘れた」を型レベルで防ぐ。

pipeline は定期的に各 section の `capture(live)` を呼んで「もし今 snapshot を更新するならどうなるか」を見て、saved snapshot との diff から `diff_to_notifications` でラベルを得る。空 list が返れば通知不要 (= 内部実装の変動など見せる必要がないやつ)、何か返れば末尾に 1 メッセージとして注入する。

### ライン単位で snapshot を持つ

`persona_cognition/01_concepts.md` の通り、メインライン / サブラインは別モデルで動き、独立した prompt cache を持つ。head snapshot もライン単位で持たないと整合が取れない。

key は最低限 `(persona_id, line_id)`。入れ子ラインは親の snapshot を派生として持つ (= 親をコピーして子のモデルで動かす設計に合わせる)。

---

## 3. アーキテクチャ

### 3.1. Section interface

```python
@dataclass(frozen=True)
class MediaRef:
    path: str
    mime_type: str
    role: str  # "image" | "audio" | ...


@dataclass
class RenderedSection:
    text: str | None
    media: list[MediaRef] = field(default_factory=list)


@dataclass
class NotificationLabel:
    kind: str   # "building_image_changed" 等の機械可読ラベル
    label: str  # ペルソナに見せる文 (例: 「この場所の内装が変わりました」)
    media: list[MediaRef] = field(default_factory=list)  # 必要なら新コンテンツを tail に attach


class HeadSection(Protocol):
    name: str
    refresh_on_events: frozenset[EventType]  # 既定 = frozenset() (= Metabolism のみ)

    def capture(self, ctx: LineHeadInput) -> SectionSnapshot:
        """live state を snapshot に焼く。
        ctx 経由でしか world にアクセスできない (= live state 参照は ctx の interface で限定される)。
        """

    def render(self, snapshot: SectionSnapshot) -> RenderedSection | None:
        """snapshot から head に乗せる内容を作る。snapshot 以外参照不可。"""

    def diff_to_notifications(
        self, old: SectionSnapshot, new: SectionSnapshot,
    ) -> list[NotificationLabel]:
        """snapshot 間の差分を末尾通知のラベル列に変換する。"""
```

`SectionSnapshot` は Section ごとの構造化型 (各 Section が自前で定義)。文字列強制しない。media identity (path / hash / mime_type) や、occupants list 等の構造化データをそのまま持てる。

### 3.2. LineHeadInput

`capture` に渡される引数。world への参照を限定的に提供する。

```python
@dataclass
class LineHeadInput:
    persona_id: str
    line_id: str
    line_role: str           # "main_line" | "sub_line" | ...
    model_key: str           # キャッシュ key 識別 (per-model anchor 紐付け)
    current_building_id: str | None
    # ... 他、各 Section が必要とする scoped accessor 群

    # 内部的には manager / db への参照を保持するが、外部に直接漏らさない
    # (Section は ctx の method 経由でしか world にアクセスできない)
```

ここに渡される情報の粒度が「各 Section が capture 時に見ていい live state」を定義する。

### 3.3. LineHeadSnapshot

ラインごとの head snapshot 全体。

```python
@dataclass
class LineHeadSnapshot:
    persona_id: str
    line_id: str
    line_role: str
    model_key: str
    captured_at: float
    snapshot_version: int               # 監査用、bump で世代識別
    sections: dict[str, SectionSnapshot]  # section.name -> snapshot
```

永続化は `(persona_id, line_id)` 単位。再起動を跨いで cache を維持するため DB に保存する (= 既存 `PersonaBuildingState` テーブルと類似のレイヤー、ただしライン単位)。

### 3.4. Section registry

```python
class HeadSectionRegistry:
    def register(self, section: HeadSection, order: int) -> None: ...
    def all_sections(self) -> list[HeadSection]: ...  # order 順
    def by_name(self, name: str) -> HeadSection | None: ...
```

Section の登録は startup 時に集中させる (アドオン由来 Section は addon_loader が登録)。order で head 内の出現順を制御。

### 3.5. Pipeline 挙動

```
[snapshot 構築タイミング]
  - 初回 (Pulse 開始時に snapshot が存在しない)
  - Metabolism 発火 (anchor 更新)
  - Section の refresh_on_events に該当するイベント発生
  ↓
  for section in registry:
      snapshot.sections[section.name] = section.capture(ctx)
  permaに save (DB)

[各 Pulse 起動時の差分チェック]
  for section in registry:
      if section.dirty or pipeline.periodic_backstop_due:
          new = section.capture(ctx)
          labels = section.diff_to_notifications(saved.sections[section.name], new)
          if labels:
              accumulated_labels.extend(labels)
              saved.sections[section.name] = new  # B 相当の更新
  if accumulated_labels:
      inject_tail_notification(accumulated_labels)

[head 構築 (= LLM 呼び出し直前)]
  rendered_parts: list[RenderedSection] = []
  for section in registry.all_sections():  # order 順
      r = section.render(saved.sections[section.name])
      if r is not None:
          rendered_parts.append(r)
  head_messages = compose_messages(rendered_parts)  # text + media を message dict 列に
```

### 3.6. refresh_on_events

各 Section は cache を切る権利を持つイベントを `refresh_on_events` で明示宣言する。デフォルトは空 (= Metabolism のみ)。

宣言可能なイベント例:

| EventType | 意味 |
|---|---|
| `building_entered` | ペルソナが Building に入った |
| `system_prompt_edited` | ペルソナ / Building の SYSTEM_INSTRUCTION が UI で編集された |
| `addon_loaded` / `addon_unloaded` | アドオン load / unload で tool registry が変動 |
| `model_changed` | ペルソナが使うモデルが切り替わった |
| `appearance_changed` | ペルソナの外見画像が差し替わった |

「全 Section が `refresh_on_events` を空にする」のがデフォルト挙動 = 平時は Metabolism までキャッシュが効き続ける。例外的に refresh が必要な Section だけ明示的に宣言する。

これにより「addon load で全部 cache 切れた」のような無自覚な破壊を、宣言の有無で監査できる。Section 設計時に「これは Metabolism まで待っていいか、それとも即 refresh が必要か」を必ず考えさせる。

---

## 4. ライン単位の扱い

### 4.1. メインライン (ペルソナ単位 1 本)

`persona_cognition/01_concepts.md` 不変条件: メインキャッシュはペルソナ全体で 1 本、Track 横断連続。

→ メインラインの `LineHeadSnapshot` は `(persona_id, line_id="main")` で一意。Track 切り替えで head は変わらない (Track 文脈は会話履歴本体 + 末尾通知で表現)。

### 4.2. サブライン (起点ライン単位)

起点サブラインは Track 内のサブキャッシュを連続蓄積する (= 起点が複数並走しうる場合は複数本)。

→ 起点サブラインごとに `LineHeadSnapshot` を持つ。`line_id` は起点識別子。Track 内に複数並走する起点があれば snapshot も並走する。

### 4.3. 入れ子ライン

入れ子ラインは親のコンテキストをコピーして開始、子の寿命中だけ存在、`report_to_parent` で完了して消える。

→ 入れ子ラインの snapshot は親 snapshot をコピーして start。子の寿命中は frozen のまま (子のモデルが違っても snapshot 内容は基本不変、ただし render 時に model-specific 加工は許容)。子完了で snapshot も破棄。

入れ子の中で更に入れ子を呼ぶ場合は再帰的にコピー派生する。

### 4.4. モデル切替 (per-model anchor との接続)

既存の `AI.METABOLISM_ANCHORS` は `{"model": {"anchor_id": ..., "updated_at": ...}}` の per-model 設計。本機構はこれと組み合わせて、ペルソナがメイン / サブ両方のモデルを持つ場合に**ライン × モデル**の組で snapshot を保存する。

`LineHeadSnapshot.model_key` がこの紐付けを担う。同一ライン内でモデルが切り替わる (= 通常起きないが、設定変更で発生しうる) 場合は `model_changed` イベントで snapshot 再構築。

---

## 5. 既存資産との関係

### 5.1. dynamic_state_sync

既存の `DynamicStateManager` は Building 世界状態 (items / occupants / memopedia / chronicle) を扱う。

→ これは本機構の **Section 群** として再解釈される。例えば `BuildingItemsSection` / `BuildingOccupantsSection` / `MemopediaIndexSection` / `ChronicleIndexSection` の 4 Section に分解、各々が capture / render / diff を持つ。

既存の `BuildingStateSnapshot` / `compute_diff` / `format_event_message` のロジックは Section 内に移植される。`PersonaBuildingState` テーブルは `LineHeadSnapshot` の永続化先に統合 or 並走 (移行戦略は実装時に判断)。

### 5.2. visual_context cache

既存の `runtime_context.py` の visual_context cache (anchor キー) は、`VisualContextSection` (refresh_on_events: `{building_entered, appearance_changed}`) として本機構に乗せ替えられる。

`_visual_context_cache` / `_visual_context_anchor` の persona 属性は廃止、snapshot に統合。

### 5.3. system prompt の各 section

`runtime_context.py` の以下を Section 化:

| 既存 section | 移行後の Section | refresh_on_events |
|---|---|---|
| 1. common_prompt 展開 | `CommonPromptSection` | (なし、Metabolism のみ) |
| 2. ## あなたについて | `PersonaSelfSection` | `{system_prompt_edited, appearance_changed}` |
| 3. ## Building 名 | `BuildingSection` | `{building_entered, system_prompt_edited}` |
| 4. ## 利用可能な能力 | `AvailablePlaybooksSection` | `{addon_loaded, addon_unloaded}` |
| 6. ## スペル | `SpellListSection` | `{addon_loaded, addon_unloaded}` |
| (visual_context) | `VisualContextSection` | `{building_entered, appearance_changed}` |
| (memory_weave) | `MemoryWeaveSection` | (Metabolism のみ) |

Phase 4' で問題になった「spell list が building 移動で変わる」は本機構では `SpellListSection.refresh_on_events` に `building_entered` を含めない限り起きない。building 単位 visibility は実行時 gate (MCP tool wrapper) で enforce、spell 一覧自体は Metabolism まで凍結。

### 5.4. METABOLISM_ANCHORS / metabolism_anchor_message_id

既存の per-model anchor 機構はそのまま使う。本機構は anchor 更新を「Metabolism イベント」として受け取り、全 Section に capture を走らせて snapshot を更新する。

---

## 6. 不変条件

### C1. Head section は snapshot 経由でのみ構築される

`HeadSection.render(snapshot)` 以外の経路で head 文字列を組み立てることを禁止。`runtime_context.py` を本機構経由に書き換える際、live state を直接読む既存実装は順次撤去する。

### C2. Section 登録には capture / render / diff_to_notifications の 3 つが揃っている必要がある

`HeadSectionRegistry.register` は Protocol を満たす実装のみ受け付ける。1 つでも欠ければ型エラー / 登録拒否。これにより「差分通知忘れ」を物理的に防ぐ。

### C3. snapshot 更新は明示イベント経由のみ

Metabolism または `refresh_on_events` に列挙されたイベントでしか snapshot は再構築されない。これ以外の経路で `LineHeadSnapshot` を mutate するコードを書かない。

### C4. LineHeadSnapshot はライン単位で独立

メインライン / サブライン / 入れ子ラインそれぞれが独立した snapshot を持つ。あるラインの snapshot 更新が他ラインの cache を破壊しない。

### C5. 差分通知は末尾注入 (= cache 安定帯の外)

`diff_to_notifications` の結果は会話履歴の末尾に message として注入される。head の中に混ぜない。これにより head 不変条件を保ったまま「世界状態は変わってる」事実をペルソナに届ける。

### C6. デフォルトは「Metabolism まで cache を維持」

Section の `refresh_on_events` 未指定 = 空 frozenset = Metabolism のみで更新。例外的に refresh する場合は明示宣言、レビュー時にも理由がわかる。

---

## 7. 実装方針 (Phase 分け)

実装規模が大きいため段階的に進める。Intent 確定後に詳細スケジュールを立てる前提で、現時点の粗いフェーズ分けのみ記す。

本機構は既存 `runtime_context.py` を**置き換える**形で導入する。長期の並走期間は持たない (= 並走は移植作業中の数セッションに限定、機能フラグや旧/新比較経路を恒久的に残さない)。

### Phase 1: 基盤導入

- `HeadSection` Protocol + `LineHeadInput` / `LineHeadSnapshot` / `RenderedSection` / `NotificationLabel` の型定義
- `HeadSectionRegistry` 実装
- pipeline (capture / diff チェック / render) の skeleton
- `LineHeadSnapshot` の DB 永続化 (新テーブル or 既存 `PersonaBuildingState` 拡張)
- Metabolism / 各 `refresh_on_events` の dispatch 経路

この時点ではまだ `runtime_context.py` を本機構経由に切り替えない (= 型と pipeline の準備のみ)。

### Phase 2: 既存 section の移植 + 切替

`runtime_context.py` の各 section を `HeadSection` 実装に置き換え、pipeline 経由で head を構築するように切り替える。優先順:

1. `SpellListSection` (Phase 4' / A-3-c で問題化した本命)
2. `BuildingSection` (system_instruction 直読みを snapshot 経由に)
3. `PersonaSelfSection`
4. `AvailablePlaybooksSection`
5. `VisualContextSection` (既存 anchor cache を吸収)
6. `MemoryWeaveSection`
7. `CommonPromptSection`

優先順に Section を実装しつつ、最後に `runtime_context.py` の head 構築箇所を一気に pipeline 経由に差し替える。`runtime_context.py` の live state 直読み経路は削除して旧/新の二重実装を残さない。

### Phase 3: dynamic_state の Section 化

`DynamicStateManager` の Building 世界状態を Section 群に分解、本機構に乗せ替え。既存 `PersonaBuildingState` テーブルは `LineHeadSnapshot` の永続化に統合 (= テーブル併存しない)。

### Phase 4: Phase 4' / A-3-c の再実装

`SpellListSection` の `building_ids` フィルタを「実行時 gate (MCP tool wrapper) + diff_to_notifications で『使える/使えなくなったスペル』を末尾通知」の形で実装。head は building 不変。

---

## 8. 未確定事項 / 検討事項

### 8.1. `LineHeadInput` の interface 粒度

`capture` が world state にアクセスするための ctx をどこまで提供するか。あまり広くすると live state 直参照が可能になり原則が崩れる。あまり狭くすると section が表現できる内容が限られる。

→ 実装着手前に既存 `runtime_context.py` の section 群が何を参照しているか棚卸しして、最小限の accessor set を決める。

### 8.2. 差分チェックの周期

各 Pulse 起動時に全 Section の `capture` を走らせると重い。dirty flag + event-driven invalidation で抑える。

→ pipeline に `mark_dirty(section_name)` を持たせ、refresh_on_events の hook がこれを叩く。periodic backstop も持つが頻度は控えめ (例: 10 Pulse に 1 回)。

### 8.3. 入れ子ラインの snapshot 派生コスト

入れ子ラインが大量に生まれる場合、親 snapshot のコピーコストが効く。

→ 実用上、入れ子ラインの寿命は短く同時数も限定的なので問題視しない見込み。実装時に measure。

### 8.4. テストでの snapshot 安定性検証

snapshot が「変わるべきでないタイミングで変わってない」ことを自動テストできるようにしたい。

→ Section 単位の "snapshot stability test" を pattern として整備。同じ input で複数回 capture して snapshot 同値性を確認、関係ないイベント発火で snapshot が変わらないことも確認。

---

## 9. 関連ドキュメント

- [`dynamic_state_sync.md`](dynamic_state_sync.md) — A/B/C 状態モデル (本機構が一般化する)
- [`persona_cognition/01_concepts.md`](persona_cognition/01_concepts.md) — ライン構造 (本機構が乗る基盤)
- [`persona_cognition/03_data_model.md`](persona_cognition/03_data_model.md) — METABOLISM_ANCHORS / per-model anchor
- [`stackchan_vessel.md`](stackchan_vessel.md) — Phase 4' で本問題が顕在化した経緯 (A-3-c)
- `feedback_generic_foundation_first.md` — 汎用基盤を先に作る設計哲学

---

## 改訂履歴

- v0.1 (2026-05-13): 起草。Phase 4' / A-3-c の cache 破壊問題を契機に foundational refactor として整理。Section interface + LineHeadSnapshot + refresh_on_events の 3 点セットで物理的な不変条件強制を狙う。
