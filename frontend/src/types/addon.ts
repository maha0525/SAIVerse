/**
 * アドオン関連の共通型定義。
 *
 * バックエンド `api/routes/addon.py` の Pydantic モデルに対応する。
 * バックエンド変更時は両者を揃えること。
 */

/**
 * 吹き出しに並ぶアドオンのボタン 1 つ。
 *
 * ``show_when: "metadata_exists"`` のボタンは、``metadata_key`` の値が立つまで
 * 回転する待ち表示になる。値が **永久に来ない** 回 (例: 声にする文が無かった
 * 吹き出しの音声ボタン) があるので、アドオンは自分のメタデータに予約キー
 * ``unavailable_keys`` (= この吹き出しではもう立たない鍵の名前の配列) を書いて
 * 知らせる。画面はそれを見て待つのをやめる (``AddonBubbleButtons.tsx``)。
 * 後から実値が立てば、そちらが優先される。
 */
export interface AddonBubbleButton {
    id: string;
    icon: string;
    label: string;
    action?: string;
    tool?: string;
    metadata_key?: string;
    show_when?: string;
}

export interface AddonInputButton {
    id: string;
    icon: string;
    label: string;
    tool?: string;
    behavior?: string;
}

/**
 * SSE イベント受信時にクライアント側で実行するアクション宣言。
 *
 * 本体の action executor registry に `action` 名で登録された関数が実行される。
 * 初期実装は ``play_audio`` のみ対応。
 *
 * 発火条件:
 *  - ``event`` がマッチした SSE イベント
 *  - ``requires_active_tab`` が true ならアクティブクライアントタブのみ
 *  - ``requires_enabled_param`` が指定されていれば、その addon param が truthy のときのみ
 */
export interface AddonClientAction {
    id: string;
    event: string;
    action: string;
    source_metadata_key?: string;
    fallback_metadata_key?: string;
    requires_active_tab?: boolean;
    requires_enabled_param?: string;
    on_failure_endpoint?: string;
}

export interface AddonUiExtensions {
    bubble_buttons?: AddonBubbleButton[];
    input_buttons?: AddonInputButton[];
    client_actions?: AddonClientAction[];
}

export interface AddonInfo {
    addon_name: string;
    display_name: string;
    description: string;
    version: string;
    is_enabled: boolean;
    params_schema: unknown[];
    params: Record<string, unknown>;
    ui_extensions: AddonUiExtensions;
}
