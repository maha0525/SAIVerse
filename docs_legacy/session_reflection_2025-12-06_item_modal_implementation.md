# セッション振り返り: アイテムモーダル実装 (2025-12-06)

## 実装した機能

右サイドバーのアイテム表示で、document/pictureタイプのアイテムをクリックするとモーダルが開き、ファイルの内容（テキストまたは画像）を表示する機能。

## 遭遇した問題と解決の経緯

### 問題1: JavaScriptが実行されない

**症状:**
- `ITEM_MODAL_JS`をGradioに注入したが、ブラウザコンソールにログが一切出ない
- モーダルのクリックイベントも発火しない

**試したアプローチ:**
1. `gr.HTML(f"<script>{ITEM_MODAL_JS}</script>", visible=False)` → 失敗（スクリプトがDOMに追加されない）
2. `js_auto_refresh`と単純に文字列連結 → 構文エラー（2つのアロー関数を連結できない）

**解決策:**
- `demo.load(None, None, None, js=ITEM_MODAL_JS)`で別々に実行
- `ITEM_MODAL_JS`を`() => { ... }`のアロー関数形式にする

**学び:**
- Gradioの`demo.load()`の`js`パラメータはアロー関数形式`() => { ... }`が必須
- `visible=False`のコンポーネントはDOMに追加されない

---

### 問題2: カスタムAPIエンドポイントが404を返す

**症状:**
- `/api/item/view`エンドポイントをFastAPIに登録
- `demo.app.routes`のリストには表示される
- しかし実際にリクエストすると404が返る
- エンドポイント関数が一度も呼ばれない（ログが出ない）

**試したアプローチ:**
1. `@demo.app.get("/api/item/view")`デコレータ → 404
2. `demo.app.add_api_route()`メソッド → 404
3. Blocksコンテキストの外で登録 → 404
4. `gr.api(api_name="item_view")`（公式方法） → `/api/call/item_view`も404

**判明したこと:**
- カスタムエンドポイントはルートリストに表示されるが、Gradioの内部ルーティングで何かがブロックしている
- Gradioのミドルウェアが先にリクエストを処理してしまう可能性

**最終的な解決策:**
- カスタムAPIエンドポイントを諦める
- **Gradioのネイティブイベントシステム**（Button + Textbox + `.click()`）を使用

---

### 問題3: `visible=False`のコンポーネントがDOMに存在しない

**症状:**
```javascript
const inputElem = document.querySelector('#item_id_input textarea'); // null
```

**原因:**
- `gr.Textbox(visible=False, elem_id="item_id_input")`はDOMに追加されない
- JavaScriptから参照できない

**解決策:**
```python
# visible=True にしてCSSで非表示
with gr.Row(elem_classes=["hidden-api-components"], visible=True):
    item_view_button = gr.Button("API", elem_id="item_view_button", visible=True)
    item_id_hidden = gr.Textbox(label="item_id", elem_id="item_id_hidden", visible=True)
    result_hidden = gr.Textbox(label="result", elem_id="result_hidden", visible=True)
```

```css
.hidden-api-components {
    display: none !important;
}
```

**学び:**
- **Gradioで`visible=False`のコンポーネントはDOMに追加されない**
- JavaScriptからアクセスしたい場合は`visible=True` + CSSで非表示にする

---

### 問題4: セレクタのミス

**症状:**
```javascript
const button = document.querySelector('#item_view_button button'); // null
```

**ログから判明した事実:**
```
Looking for container: <button class="lg secondary svelte-1ixn6qd" id="item_view_button">...</button>
```

→ `#item_view_button`は`<button>`要素そのもの（子要素ではない）

**原因:**
- `#item_view_button button`は「`#item_view_button`の子要素の`<button>`」を探す
- しかし`#item_view_button`自体が`<button>`なので、子要素は存在しない

**解決策:**
```javascript
const button = document.querySelector('#item_view_button'); // 正しい
```

**学び:**
- **ログに書かれている事実を最優先で確認する**
- 推測で実装せず、実際のDOM構造を見る

---

## 最終的な実装

### Python側 (ui/app.py)

```python
# 1. visible=True + CSS非表示でコンポーネント作成
with gr.Row(elem_classes=["hidden-api-components"], visible=True):
    item_view_button = gr.Button("API", elem_id="item_view_button", visible=True)
    item_id_hidden = gr.Textbox(label="item_id", elem_id="item_id_hidden", visible=True)
    result_hidden = gr.Textbox(label="result", elem_id="result_hidden", visible=True)

# 2. Python関数
def get_item_content(item_id: str) -> str:
    # アイテム情報を取得してJSON文字列を返す
    return json.dumps({"success": True, "content": "..."})

# 3. イベント接続
item_view_button.click(
    fn=get_item_content,
    inputs=[item_id_hidden],
    outputs=[result_hidden]
)

# 4. result_hiddenの変更を監視してPromiseを解決
result_hidden.change(
    fn=None,
    inputs=[result_hidden],
    outputs=None,
    js="""
    (result) => {
        if (window.__item_content_resolver) {
            window.__item_content_resolver(result);
            window.__item_content_resolver = null;
        }
        return null;
    }
    """
)

# 5. グローバル関数を作成
demo.load(None, None, None, js="""
() => {
    window.get_item_content_js = async function(item_id) {
        const button = document.querySelector('#item_view_button');
        const itemIdInput = document.querySelector('#item_id_hidden textarea');

        itemIdInput.value = item_id;
        itemIdInput.dispatchEvent(new Event('input', {bubbles: true}));

        return new Promise((resolve) => {
            window.__item_content_resolver = resolve;
            setTimeout(() => button.click(), 50);
        });
    };
}
""")
```

### JavaScript側 (ui/item_modal.py)

```javascript
// モーダル表示関数内
window.get_item_content_js(itemId).then(result => {
    const data = JSON.parse(result);
    if (data.success) {
        // コンテンツを表示
        if (itemType === 'document') {
            contentHtml += `<pre>${data.content}</pre>`;
        } else if (itemType === 'picture') {
            contentHtml += `<img src="/gradio_api/file=${data.file_path}" />`;
        }
        body.innerHTML = contentHtml;
    }
});
```

### 仕組み

1. JavaScriptから`window.get_item_content_js(item_id)`を呼ぶ
2. 非表示のTextbox (`item_id_hidden`)にitem_idをセット
3. 非表示のButton (`item_view_button`)をクリック
4. Python関数`get_item_content()`が実行される
5. 結果が`result_hidden`に設定される
6. `result_hidden.change`イベントでPromiseを解決
7. JavaScriptが結果を受け取り、モーダルに表示

---

## 重要な学び

### 1. Gradioでカスタムエンドポイントは避ける

- FastAPIのカスタムエンドポイントはGradioのルーティングと競合する
- Gradioのネイティブイベントシステム（Button/Textbox + .click()）を使うべき

### 2. visible=Falseの挙動

- **`visible=False`のコンポーネントはDOMに追加されない**
- JavaScriptからアクセスする場合は`visible=True` + CSSで非表示

### 3. デバッグのアプローチ

**❌ 悪い例:**
1. 推測で「これが原因かも」と判断
2. 別のアプローチを試す
3. 失敗したらまた別のアプローチ
4. 無限ループ

**✅ 良い例:**
1. **ログ・コンソール出力を最優先で確認**
2. 「何が分からないか」を明確にする
3. 分かるようにするための調査（DOM確認、ネットワークタブ、公式ドキュメント）
4. 事実に基づいて実装

### 4. 具体的なデバッグ手順

#### JavaScriptが実行されない場合:
```javascript
// 1. スクリプトがロードされているか確認
console.log('Script loaded!'); // 最初の行に追加

// 2. 関数が定義されているか確認
console.log('Function exists:', typeof window.myFunction);

// 3. DOM要素が存在するか確認
console.log('Element:', document.querySelector('#my-element'));
```

#### APIエンドポイントが404の場合:
```python
# 1. ルートリストに含まれているか確認
for route in demo.app.routes:
    LOGGER.info(f"Route: {route.path}")

# 2. 関数が呼ばれているか確認（関数の最初の行）
LOGGER.info("[API] ===== FUNCTION CALLED =====")

# 3. ブラウザのネットワークタブで実際のリクエストURLを確認
```

#### DOM要素が見つからない場合:
```javascript
// 1. ブラウザの開発者ツールで要素を検査
// 2. コンソールで直接セレクタを試す
document.querySelector('#my-element')

// 3. 親要素から確認
document.querySelector('#parent')
document.querySelector('#parent #child')
```

---

## 今後の改善点

1. **ログを最優先で見る習慣**
   - エラーメッセージやコンソール出力に書かれている事実を最初に確認
   - 推測は最小限にする

2. **一つの問題に集中する**
   - 「動かない」を分解: JS実行？DOM存在？関数呼び出し？
   - 一つずつ確認してから次へ

3. **公式ドキュメントを先に読む**
   - Gradioの仕様が分からない場合、試行錯誤より先にドキュメント確認
   - claude-code-guideエージェントを活用

4. **ブラウザ開発者ツールの活用**
   - コンソール（ログ、エラー、直接実行）
   - 要素検査（DOM構造、CSS）
   - ネットワークタブ（リクエスト/レスポンス）

---

## まとめ

今回の実装で最も重要だったのは、「推測ではなく事実に基づいてデバッグする」ことでした。

特にセレクタの問題では、ログに`<button id="item_view_button">`と明確に表示されていたにも関わらず、それを見ずに`'#item_view_button button'`というセレクタを使ってしまいました。

**ログに書かれている事実を最優先で確認し、推測を最小限にする**

これが今回の最大の学びです。
