/**
 * アドオン関連の共通型定義。
 *
 * バックエンド `api/routes/addon.py` の Pydantic モデルに対応する。
 * バックエンド変更時は両者を揃えること。
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
