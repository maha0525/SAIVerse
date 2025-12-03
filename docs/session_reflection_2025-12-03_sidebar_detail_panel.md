# セッション振り返り: サイドバー詳細パネルUI実装 (2025-12-03)

## セッション概要

左サイドバー（ナビゲーション）と右サイドバー（詳細パネル）を実装し、SAIVerseの視認性・操作性・拡張性を大幅に向上させた長期セッション（compact 4回実施）。

**主要な成果**:
- ✅ 左サイドバー: 240px、ナビゲーション用
- ✅ 右サイドバー: 400px、Building/ペルソナ/実行状態の詳細パネル（Accordion形式）
- ✅ PC: 両サイドバー初期表示、中央にチャット領域
- ✅ モバイル: 両サイドバー初期非表示、スワイプジェスチャーで開閉
- ✅ レスポンシブ対応、横スクロール防止、長文URL自動折り返し

---

## 🔴 最重要教訓: Gradioの動的インラインスタイル問題

### 問題の本質

**現象**: CSSファイルにサイドバー幅（340px → 240px、右400px）を書いているのに、ブラウザに反映されない。DevToolsで確認すると、CSSルールに打ち消し線が表示され、`element.style` に `width: 20vw !important; left: calc(-20vw) !important;` がインライン適用されている。

**原因**:
1. **Gradio 5.38.0 の Sidebar コンポーネントは、ページロード後にJavaScriptでインラインスタイルを動的に設定する**
2. **インラインスタイル（`element.style`）はCSSファイルのルールより優先度が高い**
3. **`!important` を使っても、インラインスタイルには勝てない**（インライン側も `!important` が付いている）

### 失敗した解決策

1. **CSS `:global()` セレクター**: Gradio のスコープ付きCSSを回避しようとしたが、結局インラインスタイルに負ける
2. **HEAD_VIEWPORT の inline CSS**: ブラウザには届くが、やはりインラインスタイルに上書きされる
3. **JavaScript `removeProperty()`**: 一度削除しても、Gradio が即座に再適用してくる
4. **MutationObserver で監視・修正**: Gradio との無限ループになり、パフォーマンス悪化

### ✅ 成功した解決策: `setProperty` ハイジャック

**戦略**: Gradio がインラインスタイルを設定する**前に**横取りして、値を置き換える。

**実装** (`/home/maha/SAIVerse/ui/app.py`):

```javascript
function hijackSidebarStyles() {
    const leftSidebar = document.querySelector('.sidebar.saiverse-sidebar:not(.right)');
    const rightSidebar = document.querySelector('.sidebar.saiverse-sidebar.right');

    if (leftSidebar) {
        const leftStyle = leftSidebar.style;
        const originalLeftSet = leftStyle.setProperty.bind(leftStyle);

        // setProperty をオーバーライド
        leftStyle.setProperty = function(prop, value, priority) {
            if (prop === 'width' && value === '20vw') {
                console.log('[SAIVerse] Intercepted left width 20vw -> 240px');
                return originalLeftSet('width', '240px', priority);
            }
            if (prop === 'left' && value.includes('20vw')) {
                console.log('[SAIVerse] Intercepted left position calc(-20vw) -> -240px');
                return originalLeftSet('left', '-240px', priority);
            }
            return originalLeftSet(prop, value, priority);
        };

        // 初期値も設定
        leftStyle.setProperty('width', '240px', 'important');
        leftStyle.setProperty('left', '-240px', 'important');
    }

    // 右サイドバーも同様に 400px に設定
    if (rightSidebar) { /* ... */ }
}
```

**ポイント**:
- `CSSStyleDeclaration.setProperty` メソッド自体を上書き
- Gradio が `setProperty('width', '20vw')` を呼ぶと、横取りして `setProperty('width', '240px')` に置き換える
- 元の関数は `originalLeftSet` として保存し、他のプロパティは通常通り処理
- ページロード時に1回実行すれば、以降すべての動的スタイル変更に適用される

### 教訓まとめ

| 手法 | 効果 | 理由 |
|------|------|------|
| CSS `:global()` | ❌ | インラインスタイルに負ける |
| `!important` in CSS | ❌ | インライン側も `!important` 付き |
| `removeProperty()` | ❌ | Gradio が再適用 |
| MutationObserver | ❌ | 無限ループ |
| **`setProperty` hijack** | ✅ | **設定される前に値を置換** |

**一般化**: **フレームワークが動的にインラインスタイルを適用する場合、CSSだけでは勝てない。JavaScript API レベルで横取り（monkey patching）するのが最終手段。**

---

## 教訓2: ジェスチャーハンドラーの状態管理

### 問題

**現象**: 左サイドバーが開いている状態で左スワイプすると、左が閉じると同時に右が開く無限ループ。しかし、右が開いている状態で右スワイプすると、右が正しく閉じるだけ。

**原因の誤診**: 最初「タッチのタイミング問題」と考え、`touchstart` で状態をキャプチャする実装を追加した。しかし、**左だけ問題が起きて右は正常**という非対称性が説明できない。

### 論理的な気づき

ユーザーからの指摘:
> 「もしタイミングの問題だったら、右でも同じことが起きるはずでしょ？でも実際はそうじゃない、右が開いてるときはちゃんと閉じるだけになってるんだよ。」

**真の原因**:
- **左ハンドラー**: `touchstart` 時の状態をキャプチャして使用（修正済み）
- **右ハンドラー**: `touchend` 時の**現在の状態**をチェック（未修正）

**何が起きていたか**:
1. 左が開いている状態で左スワイプ
2. **左ハンドラー**が先に実行: `leftWasOpenAtStart === true` なので左を閉じる
3. **右ハンドラー**が後に実行: **この時点で両方閉じている**のを見て、「両方閉じてる + 左スワイプ = 右を開く」判定が成立
4. 結果: 左が閉じると同時に右が開く

**解決策**: 右ハンドラーも `touchstart` 時に状態をキャプチャするように統一。

```javascript
let rightWasOpenAtStart = false;
let leftWasOpenAtStart = false;

const handleTouchStart = (e) => {
    // タッチ開始時に状態を記録
    rightWasOpenAtStart = rightSidebar.classList.contains("open");
    leftWasOpenAtStart = leftSidebar && leftSidebar.classList.contains("open");
};

const handleTouchEnd = (e) => {
    // キャプチャした状態を使用（現在の状態ではなく）
    if (!rightWasOpenAtStart && !leftWasOpenAtStart && isSwipingLeft ...) {
        rightSidebar.classList.add("open");
    }
    else if (rightWasOpenAtStart && isSwipingRight ...) {
        rightSidebar.classList.remove("open");
    }
};
```

### 教訓

**非対称な不具合は実装の不一致が原因**: 「Aでは起きるがBでは起きない」という現象は、タイミングやレースコンディションではなく、**実装パターンの違い**を疑うべき。

**デバッグのヒント**:
- 「同じ処理のはずなのに片方だけおかしい」→ コードを並べて比較
- 「なぜこちらは正常？」を考えると、逆に問題側の原因が見える

---

## 教訓3: 長文URL/文字列の折り返し制御

### 問題

一部のBuildingで、長い画像URL（例: `saiverse/image/20251113_001642_4Sc4Bb77e2a149f79e8c7c25c3858c42.png`）がバブルの幅を超えて、横スクロールバーが出現。

### 原因

- `overflow-wrap: break-word` だけでは不十分
- Markdownのproseクラスや、Gradio内部の要素に幅制限がない
- `#my_chat` 自体に `overflow-x: hidden` がない

### 解決策（多層防御）

```css
/* 1. バブル本体 */
.message-block .bubble {
  overflow-wrap: break-word;
  word-break: break-word;  /* 追加: 長い単語を強制折り返し */
  overflow: hidden;        /* 追加: はみ出し防止 */
  max-width: min(640px, 90vw);
}

/* 2. バブル内容 */
.message-block .bubble .bubble-content {
  word-break: break-word;
  overflow-wrap: anywhere;  /* より柔軟な折り返し */
}

/* 3. Chatbot全体に強制適用 */
#my_chat,
#my_chat * {
  word-break: break-word !important;
  overflow-wrap: anywhere !important;
}

/* 4. コンテナの横スクロール防止 */
#my_chat {
  overflow-x: hidden !important;
}

#chat_scroll_area {
  overflow-x: hidden;
}

/* 5. Markdown/Prose要素 */
#my_chat .prose,
#my_chat [class*="prose"] {
  max-width: 100% !important;
  word-break: break-word !important;
  overflow-wrap: anywhere !important;
}
```

### 教訓

**CSSの折り返し設定は多層防御が必要**:
- `overflow-wrap: break-word` だけでは不十分（古いブラウザ対策）
- `word-break: break-word` を併用（強制折り返し）
- `overflow-wrap: anywhere` でさらに柔軟に
- コンテナに `overflow-x: hidden` で最終防衛線
- `max-width: 100%` で親を超えないように制限

**フレームワーク利用時の注意**: Gradio などのフレームワークは内部で多くの要素を生成するため、**ユニバーサルセレクター (`*`) で全子要素に適用**するのが確実。

---

## ファイル変更履歴

### `/home/maha/SAIVerse/ui/app.py`
- 左サイドバー: 340px → 240px に変更
- 右サイドバー: Tabs → Accordion に変更（全セクションopen=True）
- `hijackSidebarStyles()` 関数追加（setProperty ハイジャック）
- 左右のスワイプジェスチャーハンドラー実装
- 状態キャプチャ（`leftWasOpenAtStart`, `rightWasOpenAtStart`）
- PC: 両サイドバー初期表示、モバイル: 両方非表示
- 詳細パネル更新処理を全UIイベントに追加（move, summon, refresh など）

### `/home/maha/SAIVerse/main.py`
- `HEAD_VIEWPORT` の CSS を更新（240px/400px）
- PC版でのパディング調整（`padding-left: 240px; padding-right: 400px;`）

### `/home/maha/SAIVerse/assets/css/chat.css`
- ヘッダー幅: `width: calc(100vw - 240px - 400px);`
- Composer幅: 固定700px → `calc(100vw - 240px - 400px)` に変更
- サイドバー幅: 240px（左）、400px（右）
- モバイル: 左60vw、右70vw
- バブル・Markdown・prose要素に `word-break: break-word` と `overflow-wrap: anywhere` 追加
- `#my_chat` と `#chat_scroll_area` に `overflow-x: hidden` 追加
- 多層防御で横スクロール完全防止

### `/home/maha/SAIVerse/ui/chat.py`
- `manager.item_registry` → `manager.items` に修正（AttributeError 対応）

---

## 次回以降への提言

### デバッグ時の心構え

1. **観察可能なデータを先に集める**:
   - ログ追加、ターミナル出力確認、ブラウザコンソール確認
   - **推測で修正しない**

2. **動いているケースを理解する**:
   - 「なぜAは失敗してBは成功？」→ **差分**を調べる
   - 非対称な不具合は実装の不一致が原因

3. **フレームワークの挙動を理解する**:
   - Gradio の Sidebar が動的にインラインスタイルを設定することを知らなかった
   - 公式ドキュメント + DevTools で挙動を確認

4. **一度に一つの変更**:
   - 複数の推測的修正を同時に入れない
   - 検証可能な単位で変更

### CSS/JavaScript の知識

- **CSS優先度**: インラインスタイル > `!important` in CSS
- **JavaScript monkey patching**: フレームワークのメソッドを上書きする最終手段
- **状態管理**: イベントハンドラーは状態を**いつ**読むかが重要（touchstart vs touchend）
- **折り返し**: `overflow-wrap`, `word-break`, `overflow` の組み合わせが必要

### ドキュメント更新

- `CLAUDE.md` の「Common Pitfalls」に追加:
  - **Gradio 動的インラインスタイル問題と setProperty hijack パターン**
  - **ジェスチャーハンドラーの状態キャプチャパターン**
  - **長文折り返しの多層防御パターン**

---

## 成果

- 🎉 **視認性**: 左右サイドバーで画面が整理され、中央のチャットが見やすくなった
- 🎉 **操作性**: モバイルでのジェスチャー操作、PCでの常時表示で使いやすさ向上
- 🎉 **拡張性**: 右パネルで Building/ペルソナ/実行状態を表示、今後の情報追加も容易
- 🎉 **堅牢性**: 横スクロール完全防止、長文URL自動折り返し

**compact 4回を挟む長期セッションだったが、大きな成果を得られた。**
