# あらすじ / Memopedia の書き込み関数が「トランザクションの所有」を検査していない

**状態**: 未着手 (2026-08-22 起票)。**現に壊れている経路は確認していない** — 罠が開いているだけ。手帳側で同じ形を直したときの残り。

関連: [`sai_memory/arasuji/storage.py`](../../sai_memory/arasuji/storage.py) / [`sai_memory/memopedia/storage.py`](../../sai_memory/memopedia/storage.py)
出自: 2026-08-22、手帳側の同じ欠陥 ([解決済み](archive/pocketbook_commit_flag_is_not_ownership_check.md)) を直した際に隣として発見し、同日の Codex 横断走査が独立に同じ 6 箇所を指摘した。

## 何が起きうるか

これらの書き込み関数は `commit=True` を既定に持ち、これは「**この関数がトランザクションを所有し、自分で確定させる**」の意味で使われている。ところが**呼び出し元が既にトランザクションを開いているかを検査していない**。

開いている状態で既定のまま呼ぶと、その `conn.commit()` は**呼び出し元の未確定の書き込みまで巻き込んで確定させる**。その後で呼び出し元が失敗して `rollback()` しても、巻き込まれた分は既に確定済みなので巻き戻せない。中途半端な状態が永続する。

## 対象 (6 箇所)

| ファイル | 関数 |
|---|---|
| `sai_memory/memopedia/storage.py` | `create_page` / `update_page` / `record_page_edit` / `create_fragment` |
| `sai_memory/arasuji/storage.py` | `create_entry` / `mark_consolidated` |

手帳側より**踏む確率は高いと見ている** (未検証の見立て): `create_page` はコア記憶・Chronicle・実体ページの生成が通る共通の口で、呼び出し元が多い。

## 直し方 (手帳側で確定済みの形をそのまま使える)

`sai_memory/memory/pocketbook.owns_transaction(conn, commit)` が既にある。各関数が**最初の execute より前**に一度だけこれを取り、成功時の commit と失敗時の rollback の**両方**をその結果で判定する。

⚠ **commit 側だけ直してはいけない** — 失敗時に呼び出し元のトランザクションを巻き戻す経路が残る。巻き込んで確定させるより、巻き込んで捨てる方が実害が大きい。詳細は手帳側 issue の「⚠ 素朴な一行修正は、直す前より悪くなる」節。

ヘルパの置き場は要検討: いま `pocketbook.py` にあるのは、同モジュールの `validate_epoch` を `recall_edges` / `continuity` が import している前例に合わせたため。memopedia / arasuji から手帳を import するのは向きとして不自然なので、共有の置き場 (`sai_memory/memopedia/storage.py` 側か、両者が既に依存している下位モジュール) へ移すかを決める必要がある。

## 判断が要る点

- **今やるか**: v0.3 のリリース範囲外。現に踏んでいる経路は確認していないので、リリースを止める理由にはならない
- **回帰テストの範囲**: 手帳側と同じ二本 (①既定のまま呼んでも呼び出し元の未確定分を確定させない ②失敗時に呼び出し元の束を巻き戻さない) を 6 関数ぶん書くか、代表 2 関数に絞るか
