import React, { useState, useEffect, useRef } from 'react';
import { X, Save, Loader2, Settings } from 'lucide-react';
import styles from './SettingsModal.module.css';
import ImageUpload from './common/ImageUpload';
import ModalOverlay from './common/ModalOverlay';
import DebugPanel from './DebugPanel';
import { formatCost } from '@/lib/formatCost';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

interface MetaJudgmentConfig {
    cache_threshold_ratio: number | null;
    periodic_interval_minutes: number | null;
    keep_cache_alive: boolean | null;
    // ライフビュー「作業のテンポ」(persona_activity_view.md §7)。
    // 本モーダルでは編集しないが、保存時に消さないよう保持が必要。
    autonomous_pulse_interval_seconds?: number | null;
}

// 'default' = 設定なし (built-in default を使う)、'on'/'off' = 明示的な値
type TriState = 'default' | 'on' | 'off';

interface AIConfig {
    name: string;
    description: string;
    system_prompt: string;
    default_model: string | null;
    lightweight_model: string | null;
    vision_model: string | null;
    audio_model: string | null;
    video_model: string | null;
    memory_weave_model: string | null;
    autonomy_enabled: boolean;  // 自律行動 (自分から考えて動くこと) の ON/OFF
    chronicle_enabled: boolean;
    autonomous_chronicle_enabled: boolean;
    auto_recall_enabled: boolean;
    memory_weave_context: boolean;
    memopedia_index_enabled: boolean;
    core_memory_char_budget: number | null;  // 記憶アーキv2 ゾーンA 容量目安 (NULL → 既定 2000)
    realtime_info_enabled: boolean;
    avatar_path: string | null;
    appearance_image_path: string | null;  // Visual context appearance image
    linked_user_id: number | null;  // First linked user ID
    meta_judgment_config: MetaJudgmentConfig | null;  // Phase 4-e
    user_conv_timeout_minutes: number | null;  // 2026-05-09 wait_response auto-pause
}

// Built-in defaults — must stay in sync with saiverse/meta_layer.py:_DEFAULT_JUDGMENT_CONFIG
// (リトライ系 max_retries / retry_backoff_seconds は v1 メタ判断の退役で読み手を
//  失った休眠キー。編集 UI は 2026-08-14 に削除した — track_retirement.md §7.4)
const META_JUDGMENT_DEFAULTS = {
    cache_threshold_ratio: 0.3,
    periodic_interval_minutes: 50,
    keep_cache_alive: true,
};

interface ChronicleCostEstimate {
    total_messages: number;
    processed_messages: number;
    unprocessed_messages: number;
    estimated_llm_calls: number;
    estimated_cost_usd: number;
    model_name: string;
    is_free_tier: boolean;
    batch_size: number;
    currency?: string;
}

interface UserChoice {
    id: number;
    name: string;
}

interface ModelChoice {
    id: string;
    name: string;
}

export default function SettingsModal({ isOpen, onClose, personaId }: SettingsModalProps) {
    const [config, setConfig] = useState<AIConfig | null>(null);
    const [availableModels, setAvailableModels] = useState<ModelChoice[]>([]);
    const [availableUsers, setAvailableUsers] = useState<UserChoice[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);

    // Form state
    const [description, setDescription] = useState('');
    const [systemPrompt, setSystemPrompt] = useState('');
    const [defaultModel, setDefaultModel] = useState<string>('');
    const [lightweightModel, setLightweightModel] = useState<string>('');
    const [visionModel, setVisionModel] = useState<string>('');
    const [audioModel, setAudioModel] = useState<string>('');
    const [videoModel, setVideoModel] = useState<string>('');
    const [memoryWeaveModel, setMemoryWeaveModel] = useState<string>('');
    // ⚠ 自律行動の ON/OFF は v0.3 で UI から隠した (autonomous_behavior_v3.md
    // §11「運転 UI は隠す」)。state だけ残すのは、ロードした値をそのまま保存へ
    // 往復させるため — 送らないと PATCH が既存の設定を既定値で塗り潰す。
    const [autonomyEnabled, setAutonomyEnabled] = useState<boolean>(true);
    const [chronicleEnabled, setChronicleEnabled] = useState(true);
    const [autonomousChronicleEnabled, setAutonomousChronicleEnabled] = useState(true);
    const [autoRecallEnabled, setAutoRecallEnabled] = useState(true);
    const [memoryWeaveContext, setMemoryWeaveContext] = useState(true);
    const [memopediaIndexEnabled, setMemopediaIndexEnabled] = useState(true);
    const [coreMemoryCharBudget, setCoreMemoryCharBudget] = useState<string>('');
    const [spellEnabled, setSpellEnabled] = useState(false);
    const [realtimeInfoEnabled, setRealtimeInfoEnabled] = useState(true);
    const [realtimeSpells, setRealtimeSpells] = useState<Array<{binding_id: number; spell_name: string; spell_args_json: string | null; label: string | null; enabled: boolean; priority: number}>>([]);
    const [spellCatalog, setSpellCatalog] = useState<Array<{name: string; description: string; parameters: {properties: Record<string, any>; required: string[]}}>>([]);
    const [newSpellName, setNewSpellName] = useState('');
    const [newSpellArgs, setNewSpellArgs] = useState<Record<string, string>>({});
    const [newSpellLabel, setNewSpellLabel] = useState('');
    // Phase 4-e: empty string = use built-in default (NULL in DB)
    const [metaCacheThresholdRatio, setMetaCacheThresholdRatio] = useState<string>('');
    // 自動発話間隔は「自律行動マネージャー」の interval 入力に統合済 (Phase 4-e)。
    // META_JUDGMENT_CONFIG.periodic_interval_minutes は autonomy API 経由で永続化される。
    const [metaKeepCacheAlive, setMetaKeepCacheAlive] = useState<TriState>('default');
    // ロード時の META_JUDGMENT_CONFIG 全体。update_ai は config を丸ごと置換するため、
    // 本モーダルが編集しないキー (periodic_interval_minutes /
    // autonomous_pulse_interval_seconds 等、autonomy / activity API が永続化したもの)
    // を保存時に巻き込んで消さないよう、ここからマージして送る。
    const [loadedMetaConfig, setLoadedMetaConfig] = useState<Record<string, unknown> | null>(null);
    // 2026-05-09: ユーザー会話 Track の wait_response 自動 pause 閾値 (分)。
    // 空文字列 = 既定値 (30 分) を使う (DB は NULL)。
    const [userConvTimeoutMinutes, setUserConvTimeoutMinutes] = useState<string>('');
    const [costEstimate, setCostEstimate] = useState<ChronicleCostEstimate | null>(null);
    const [avatarPath, setAvatarPath] = useState('');
    const [appearanceImagePath, setAppearanceImagePath] = useState('');
    const [linkedUserId, setLinkedUserId] = useState<string>('');

    // 2026-04-30 エリス上書き事故の再発防止 (feedback_modal_id_integrity.md):
    // ロード元 personaId と保存時 personaId の整合性を検証するための state。
    // - loadedPersonaId: loadConfig で実際にフォームへ展開できた最後の personaId。
    //   不一致なら handleSave は拒否する。
    // - personaIdRef: 非同期 fetch の race-condition ガード用 (常に最新 prop を保持)。
    const [loadedPersonaId, setLoadedPersonaId] = useState<string | null>(null);
    const personaIdRef = useRef<string>(personaId);
    personaIdRef.current = personaId;

    useEffect(() => {
        if (isOpen) {
            loadModels();
            loadUsers();
        }
    }, [isOpen]);

    useEffect(() => {
        if (isOpen && personaId) {
            // personaId 変更時はまず loadedPersonaId をクリアして「未ロード」状態に。
            // これで handleSave がロード完了前の保存を拒否できる。
            setLoadedPersonaId(null);
            loadConfig();
        }
    }, [isOpen, personaId, availableModels]); // dependent on availableModels to safely set default

    const loadModels = async () => {
        try {
            const res = await fetch('/api/info/models');
            if (res.ok) {
                const data = await res.json();
                setAvailableModels(data);
            }
        } catch (e) {
            console.error("Failed to load models", e);
        }
    };

    const loadUsers = async () => {
        try {
            const res = await fetch('/api/user/list');
            if (res.ok) {
                const data = await res.json();
                setAvailableUsers(data);
            }
        } catch (e) {
            console.error("Failed to load users", e);
        }
    };

    const loadConfig = async () => {
        setIsLoading(true);
        // Race-condition guard: capture the personaId at the start.
        // 非同期 fetch 中に personaId が切り替わった場合、stale な結果で setter
        // を呼ばないようにする (フォーム state が新旧混在するのを防ぐ)。
        const targetPersonaId = personaIdRef.current;
        const isStale = () => targetPersonaId !== personaIdRef.current;

        try {
            const res = await fetch(`/api/people/${targetPersonaId}/config`);
            if (isStale()) {
                console.warn(
                    `[SettingsModal] loadConfig stale (${targetPersonaId} -> ${personaIdRef.current}); discarding /config response`
                );
                return;
            }
            if (res.ok) {
                const data = await res.json();
                if (isStale()) {
                    console.warn(
                        `[SettingsModal] loadConfig stale post-parse (${targetPersonaId} -> ${personaIdRef.current}); not applying setters`
                    );
                    return;
                }
                setConfig(data);
                setDescription(data.description);
                setSystemPrompt(data.system_prompt);
                setDefaultModel(data.default_model || '');
                setLightweightModel(data.lightweight_model || '');
                setVisionModel(data.vision_model || '');
                setAudioModel(data.audio_model || '');
                setVideoModel(data.video_model || '');
                setMemoryWeaveModel(data.memory_weave_model || '');
                setAutonomyEnabled(data.autonomy_enabled ?? true);
                setChronicleEnabled(data.chronicle_enabled ?? true);
                setAutonomousChronicleEnabled(data.autonomous_chronicle_enabled ?? true);
                setAutoRecallEnabled(data.auto_recall_enabled ?? true);
                setMemoryWeaveContext(data.memory_weave_context ?? true);
                setMemopediaIndexEnabled(data.memopedia_index_enabled ?? false);
                setCoreMemoryCharBudget(
                    data.core_memory_char_budget != null
                        ? String(data.core_memory_char_budget)
                        : ''
                );
                setSpellEnabled(data.spell_enabled ?? false);
                setRealtimeInfoEnabled(data.realtime_info_enabled ?? true);
                // Load realtime spell bindings + catalog
                try {
                    const [spellRes, catalogRes] = await Promise.all([
                        fetch(`/api/people/${personaId}/realtime-spell`),
                        fetch('/api/people/realtime-spell-catalog'),
                    ]);
                    if (spellRes.ok) setRealtimeSpells(await spellRes.json());
                    if (catalogRes.ok) setSpellCatalog(await catalogRes.json());
                } catch (e) { /* ignore */ }
                // Phase 4-e: NULL → empty string で「既定値を使う」を表現
                const mjc: MetaJudgmentConfig | null = data.meta_judgment_config ?? null;
                setLoadedMetaConfig(mjc ? { ...mjc } : null);
                setMetaCacheThresholdRatio(
                    mjc?.cache_threshold_ratio != null ? String(mjc.cache_threshold_ratio) : ''
                );
                setMetaKeepCacheAlive(
                    mjc?.keep_cache_alive == null ? 'default' :
                        (mjc.keep_cache_alive ? 'on' : 'off')
                );
                setUserConvTimeoutMinutes(
                    data.user_conv_timeout_minutes != null
                        ? String(data.user_conv_timeout_minutes)
                        : ''
                );
                setAvatarPath(data.avatar_path || '');
                setAppearanceImagePath(data.appearance_image_path || '');
                setLinkedUserId(data.linked_user_id ? String(data.linked_user_id) : '');
                // フォーム state が targetPersonaId のもので埋まったので、ここで「ロード成功」マーク。
                // handleSave はこの値が現 prop と一致することを確認する。
                setLoadedPersonaId(targetPersonaId);
            } else {
                console.error("Failed to load config");
            }

            // Load Chronicle cost estimate
            try {
                const costRes = await fetch(`/api/people/${targetPersonaId}/arasuji/cost-estimate`);
                if (isStale()) return;
                if (costRes.ok) {
                    const costData = await costRes.json();
                    if (isStale()) return;
                    setCostEstimate(costData);
                }
            } catch {
                // Non-critical: cost estimate is informational only
            }
        } catch (error) {
            console.error(error);
        } finally {
            // stale な loadConfig が isLoading を勝手に false にすると、
            // 真っ先に走った最新 loadConfig の進行が見えなくなる。
            // 最新の呼び出しのみが isLoading をクリアする。
            if (!isStale()) {
                setIsLoading(false);
            }
        }
    };


    const handleSave = async () => {
        // 整合性ガード: ロード元 personaId と保存先 personaId が一致しないと、
        // 別ペルソナのフォーム内容で別レコードを上書きする事故が起きる
        // (2026-04-30 エリス上書き事故の再発防止)。
        if (isLoading) {
            alert('読み込み中のため保存できません。少し待ってから再度お試しください。');
            return;
        }
        if (!loadedPersonaId || loadedPersonaId !== personaId) {
            alert(
                `安全のため保存を拒否しました。\n` +
                `表示中のフォームは "${loadedPersonaId ?? '(未読み込み)'}" のもので、\n` +
                `現在の保存先は "${personaId}" です。\n` +
                `モーダルを一度閉じてから開き直してください。`
            );
            console.error(
                `[SettingsModal] handleSave rejected: loadedPersonaId=${loadedPersonaId} != personaId=${personaId}`
            );
            return;
        }

        setIsSaving(true);
        try {
            const res = await fetch(`/api/people/${personaId}/config`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    description: description,
                    system_prompt: systemPrompt,
                    default_model: defaultModel,
                    lightweight_model: lightweightModel,
                    vision_model: visionModel,
                    audio_model: audioModel,
                    video_model: videoModel,
                    memory_weave_model: memoryWeaveModel,
                    autonomy_enabled: autonomyEnabled,
                    chronicle_enabled: chronicleEnabled,
                    autonomous_chronicle_enabled: autonomousChronicleEnabled,
                    auto_recall_enabled: autoRecallEnabled,
                    memory_weave_context: memoryWeaveContext,
                    memopedia_index_enabled: memopediaIndexEnabled,
                    // 記憶アーキv2 ゾーンA 容量目安: 空文字列 = 既定値 (0 を送って NULL に倒す)、
                    // それ以外は parseInt 結果。NaN は 0 扱いで既定値 (2000) 復帰。
                    core_memory_char_budget: (() => {
                        const trimmed = coreMemoryCharBudget.trim();
                        if (!trimmed) return 0;
                        const parsed = parseInt(trimmed);
                        return Number.isNaN(parsed) ? 0 : parsed;
                    })(),
                    spell_enabled: spellEnabled,
                    realtime_info_enabled: realtimeInfoEnabled,
                    avatar_path: avatarPath || null,
                    appearance_image_path: appearanceImagePath || null,
                    linked_user_id: linkedUserId ? parseInt(linkedUserId) : 0,  // 0 = clear link
                    // Phase 4-e: 各値が空文字列なら null = 既定値使用。
                    // 本モーダルが編集しないキー (periodic_interval_minutes 等) は
                    // ロード時の値からマージして保持する (update_ai は丸ごと置換のため、
                    // フォーム項目だけで再構築すると autonomy / activity API が永続化した
                    // 値が消える)。結果が空オブジェクトなら null で DB 側を NULL に戻す。
                    meta_judgment_config: (() => {
                        const obj: Record<string, unknown> = { ...(loadedMetaConfig ?? {}) };
                        // null 値 (既定値使用) のキーは落とす
                        for (const key of Object.keys(obj)) {
                            if (obj[key] == null) delete obj[key];
                        }
                        const ratio = metaCacheThresholdRatio.trim();
                        const keepCache = metaKeepCacheAlive;
                        if (ratio) obj.cache_threshold_ratio = parseFloat(ratio);
                        else delete obj.cache_threshold_ratio;
                        if (keepCache !== 'default') obj.keep_cache_alive = (keepCache === 'on');
                        else delete obj.keep_cache_alive;
                        return Object.keys(obj).length > 0 ? obj : null;
                    })(),
                    // 2026-05-09: 空文字列 = 既定値 (= 0 を送って NULL に倒す)、
                    // それ以外は parseInt 結果。NaN は 0 扱いで既定値復帰。
                    user_conv_timeout_minutes: (() => {
                        const trimmed = userConvTimeoutMinutes.trim();
                        if (!trimmed) return 0;
                        const parsed = parseInt(trimmed);
                        return Number.isNaN(parsed) ? 0 : parsed;
                    })()
                })
            });

            if (res.ok) {
                const data = await res.json();
                if (data.warning) {
                    alert(`設定は保存されましたが、警告があります:\n${data.warning}`);
                }
                onClose();
            } else {
                const err = await res.json();
                alert(`保存に失敗しました: ${err.detail}`);
            }
        } catch (error) {
            console.error(error);
            alert("設定の保存中にエラーが発生しました");
        } finally {
            setIsSaving(false);
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2><Settings size={22} /> ペルソナ設定</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {isLoading ? (
                        <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}>
                            <Loader2 className="spin" size={32} />
                        </div>
                    ) : (
                        <>
                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>名前</label>
                                <div className={styles.input} style={{ background: 'rgba(0,0,0,0.05)', color: '#888' }}>
                                    {config?.name}
                                </div>
                                <div className={styles.description}>名前はここでは変更できません。</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>デフォルトモデル</label>
                                <select
                                    className={styles.select}
                                    value={defaultModel}
                                    onChange={(e) => setDefaultModel(e.target.value)}
                                >
                                    <option value="">システムデフォルトを使用</option>
                                    {defaultModel && !availableModels.some(m => m.id === defaultModel) && (
                                        <option value={defaultModel}>⚠️ 不明: {defaultModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>軽量モデル（任意）</label>
                                <select
                                    className={styles.select}
                                    value={lightweightModel}
                                    onChange={(e) => setLightweightModel(e.target.value)}
                                >
                                    <option value="">なし（デフォルトを使用）</option>
                                    {lightweightModel && !availableModels.some(m => m.id === lightweightModel) && (
                                        <option value={lightweightModel}>⚠️ 不明: {lightweightModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div className={styles.description}>該当する場合、より高速で安価なレスポンスに使用されます。</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Memory Weaveモデル（任意）</label>
                                <select
                                    className={styles.select}
                                    value={memoryWeaveModel}
                                    onChange={(e) => setMemoryWeaveModel(e.target.value)}
                                >
                                    <option value="">グローバル設定を使用</option>
                                    {memoryWeaveModel && !availableModels.some(m => m.id === memoryWeaveModel) && (
                                        <option value={memoryWeaveModel}>⚠️ 不明: {memoryWeaveModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div className={styles.description}>クロニクル・メモペディア生成に使用するモデル。</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>画像要約モデル（任意）</label>
                                <select
                                    className={styles.select}
                                    value={visionModel}
                                    onChange={(e) => setVisionModel(e.target.value)}
                                >
                                    <option value="">グローバル設定を使用</option>
                                    {visionModel && !availableModels.some(m => m.id === visionModel) && (
                                        <option value={visionModel}>⚠️ 不明: {visionModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div className={styles.description}>画像・ドキュメントの要約生成に使用するモデル。</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>音声要約モデル（任意）</label>
                                <select
                                    className={styles.select}
                                    value={audioModel}
                                    onChange={(e) => setAudioModel(e.target.value)}
                                >
                                    <option value="">グローバル設定を使用</option>
                                    {audioModel && !availableModels.some(m => m.id === audioModel) && (
                                        <option value={audioModel}>⚠️ 不明: {audioModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div className={styles.description}>音声ファイルの要約生成に使用するモデル（Gemini系推奨）。</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>動画要約モデル（任意）</label>
                                <select
                                    className={styles.select}
                                    value={videoModel}
                                    onChange={(e) => setVideoModel(e.target.value)}
                                >
                                    <option value="">グローバル設定を使用</option>
                                    {videoModel && !availableModels.some(m => m.id === videoModel) && (
                                        <option value={videoModel}>⚠️ 不明: {videoModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div className={styles.description}>動画ファイルの要約生成に使用するモデル（Gemini系推奨）。</div>
                            </div>

                            <DebugPanel personaId={personaId} />

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>キャッシュ維持の設定</label>
                                {(() => {
                                    // 実効値の解決 (default なら built-in default)
                                    const effectiveKeepCache = metaKeepCacheAlive === 'default'
                                        ? META_JUDGMENT_DEFAULTS.keep_cache_alive
                                        : metaKeepCacheAlive === 'on';
                                    return (
                                        <>
                                            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                                    <span style={{ minWidth: '160px' }}>キャッシュ維持</span>
                                                    <select
                                                        className={styles.select}
                                                        value={metaKeepCacheAlive}
                                                        onChange={(e) => setMetaKeepCacheAlive(e.target.value as TriState)}
                                                        style={{ width: '14rem' }}
                                                    >
                                                        <option value="default">既定 ({META_JUDGMENT_DEFAULTS.keep_cache_alive ? 'ON' : 'OFF'})</option>
                                                        <option value="on">ON (TTL 接近で前倒し)</option>
                                                        <option value="off">OFF (TTL 無視 / 低頻度向け)</option>
                                                    </select>
                                                </div>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                                    <span style={{ minWidth: '160px' }}>キャッシュ閾値 (0.0–1.0)</span>
                                                    <input
                                                        type="number"
                                                        step="0.05"
                                                        min="0"
                                                        max="1"
                                                        placeholder={String(META_JUDGMENT_DEFAULTS.cache_threshold_ratio)}
                                                        value={metaCacheThresholdRatio}
                                                        onChange={(e) => setMetaCacheThresholdRatio(e.target.value)}
                                                        style={{ width: '7rem' }}
                                                        disabled={!effectiveKeepCache}
                                                        title={effectiveKeepCache ? '' : 'キャッシュ維持が OFF のため無効'}
                                                    />
                                                    <span style={{ fontSize: '0.85em', color: '#888' }}>
                                                        (既定: {META_JUDGMENT_DEFAULTS.cache_threshold_ratio})
                                                    </span>
                                                </div>
                                            </div>
                                            <div className={styles.description}>
                                                会話のたびに送り直す前置き（プロンプト）は、モデル側のキャッシュに一定時間だけ残ります。その残り時間が「キャッシュ閾値」の割合を切ると、極小の呼び出しを入れてキャッシュを温め直します（応答を作るわけではありません）。「キャッシュ維持」を OFF にすると温め直しを行いません（低頻度運用向け。キャッシュが切れることを許容します）。空欄の項目は既定値が適用されます。
                                            </div>
                                        </>
                                    );
                                })()}
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>応答待ち Track 自動 pause 閾値</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <input
                                        type="number"
                                        step="1"
                                        min="0"
                                        placeholder="30"
                                        value={userConvTimeoutMinutes}
                                        onChange={(e) => setUserConvTimeoutMinutes(e.target.value)}
                                        style={{ width: '7rem' }}
                                    />
                                    <span>分</span>
                                    <span style={{ fontSize: '0.85em', color: '#888' }}>
                                        (既定: 30 分 / 0 で既定に戻す)
                                    </span>
                                </div>
                                <div className={styles.description}>
                                    対ユーザー会話 Track のような応答待ち型 Track が、最終メッセージからこの分数以上 idle になると、会話の区切りとして扱い、ふりかえりの判断を行います（ペルソナの状態は変わりません）。長期 idle で自律稼働が止まる事故の脱出経路として動作します。軽量モデルなら短く (10〜15 分)、重量級モデルや人間の応答間隔が長い運用なら長く (60 分以上) 設定してください。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Chronicle 自動生成</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={chronicleEnabled}
                                            onChange={(e) => setChronicleEnabled(e.target.checked)}
                                        />
                                        <span>{chronicleEnabled ? '有効' : '無効'}</span>
                                    </label>
                                </div>
                                <div className={styles.description}>
                                    会話が一定量を超えたとき、古い部分を自動的にあらすじ（Chronicle）へ畳みます。生成の瞬間にLLM APIコストが発生します。無効にすると自動生成は止まり、メモリー画面の「Chronicle」タブからの手動生成もできなくなります。
                                </div>
                                {costEstimate && costEstimate.unprocessed_messages > 0 && (
                                    <div className={styles.description} style={{
                                        marginTop: '0.5rem',
                                        padding: '0.5rem',
                                        background: costEstimate.unprocessed_messages > 500
                                            ? 'rgba(255, 150, 0, 0.1)'
                                            : 'rgba(100, 100, 100, 0.1)',
                                        borderRadius: '4px',
                                        fontSize: '0.85rem',
                                    }}>
                                        <div>あらすじになっていない過去メッセージ: <strong>{costEstimate.unprocessed_messages.toLocaleString()}</strong>件（自動ではあらすじ化されません）</div>
                                        <div>
                                            まとめてあらすじ化した場合の推定コスト: <strong>
                                                {costEstimate.is_free_tier
                                                    ? `${formatCost(0, costEstimate.currency)} (Free tier)`
                                                    : formatCost(costEstimate.estimated_cost_usd, costEstimate.currency)
                                                }
                                            </strong>
                                            {' '}({costEstimate.model_name})
                                        </div>
                                        <div>推定LLM呼び出し: {costEstimate.estimated_llm_calls}回</div>
                                    </div>
                                )}
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>自律行動中も記憶整理（Chronicle生成）を行う</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={autonomousChronicleEnabled}
                                            onChange={(e) => setAutonomousChronicleEnabled(e.target.checked)}
                                        />
                                        <span>{autonomousChronicleEnabled ? '有効' : '無効'}</span>
                                    </label>
                                </div>
                                <div className={styles.description}>
                                    自律稼働中（ユーザーとの会話以外のタイミング）に記憶の整理が起きた際も、確認なしでChronicleを自動生成します。無効にすると、自律稼働中に溜まった会話はChronicle化されず、次にユーザーと会話するまで長期記憶に残りません。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>自動想起（ふと浮かんだ記憶）</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={autoRecallEnabled}
                                            onChange={(e) => setAutoRecallEnabled(e.target.checked)}
                                        />
                                        <span>{autoRecallEnabled ? '有効' : '無効'}</span>
                                    </label>
                                </div>
                                <div className={styles.description}>
                                    会話の内容に関連する記憶を自動的に思い出し、応答に反映します。無効にすると、記憶の想起はスペルによる手動想起のみになります。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Memory Weave コンテキスト</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={memoryWeaveContext}
                                            onChange={(e) => setMemoryWeaveContext(e.target.checked)}
                                        />
                                        <span>{memoryWeaveContext ? '有効' : '無効'}</span>
                                    </label>
                                </div>
                                <div className={styles.description}>
                                    会話時にChronicle・Memopediaの情報をLLMに提供します。無効にするとコンテキスト量が減りますが、長期記憶を参照できなくなります。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Memopedia索引の常時表示</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={memopediaIndexEnabled}
                                            onChange={(e) => setMemopediaIndexEnabled(e.target.checked)}
                                        />
                                        <span>{memopediaIndexEnabled ? '有効' : '無効'}</span>
                                    </label>
                                </div>
                                <div className={styles.description}>
                                    Memopediaの全ページ一覧（タイトルと概要）を、常にペルソナのコンテキストへ読み込みます。ペルソナが自分の記憶の全体像を把握しやすくなる反面、トークン消費が増えます。当面は有効のままをおすすめします。設定の変更は、ペルソナメニューの「記憶を整理」を押すとすぐに反映されます（押さなくても、次に記憶の整理が起きたときに反映されます）。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>コア記憶の文字数目安</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <input
                                        type="number"
                                        step="100"
                                        min="0"
                                        placeholder="2000"
                                        value={coreMemoryCharBudget}
                                        onChange={(e) => setCoreMemoryCharBudget(e.target.value)}
                                        style={{ width: '7rem' }}
                                    />
                                    <span>字</span>
                                    <span style={{ fontSize: '0.85em', color: '#888' }}>
                                        (既定: 2000 字 / 空で既定に戻す)
                                    </span>
                                </div>
                                <div className={styles.description}>
                                    ペルソナが自分で刻む「コア記憶」（常に携えておく恒常知識）の合計文字数の目安です。この文字数を超えると、コア記憶を編集するスペルの結果に整理を促す通知が添えられます。目安を超えても内容が切り詰められることはありません（通知のみ）。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>スペル</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={spellEnabled}
                                            onChange={(e) => setSpellEnabled(e.target.checked)}
                                        />
                                        <span>{spellEnabled ? '有効' : '無効'}</span>
                                    </label>
                                </div>
                                <div className={styles.description}>
                                    発言中に /spell コマンドを使って、Memopediaやチャットログを直接参照できるようにします。ツール定義を使わないため、キャッシュ効率に影響しません。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>リアルタイム情報</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={realtimeInfoEnabled}
                                            onChange={(e) => setRealtimeInfoEnabled(e.target.checked)}
                                        />
                                        <span>{realtimeInfoEnabled ? '有効' : '無効'}</span>
                                    </label>
                                </div>
                                <div className={styles.description}>
                                    発言の直前に現在時刻・前回発言時刻・空間情報などの動的コンテキストを提供します。無効にすると、これらをこのペルソナには一切送りません（時刻を気にして会話が成立しなくなる場合などに）。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>事前実行スペル</label>
                                <div className={styles.description} style={{ marginBottom: '0.5rem' }}>
                                    会話のたびに自動実行し、結果をリアルタイム情報に追加するスペルを設定します。
                                </div>
                                {realtimeSpells.length > 0 && (
                                    <div style={{ marginBottom: '0.75rem' }}>
                                        {realtimeSpells.map((spell) => (
                                            <div key={spell.binding_id} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem', padding: '0.4rem 0.6rem', background: 'var(--bg-secondary, #f5f5f5)', borderRadius: '4px' }}>
                                                <span style={{ flex: 1, fontSize: '0.85rem' }}>
                                                    <strong>{spell.label || spell.spell_name}</strong>
                                                    {spell.spell_args_json && <span style={{ opacity: 0.6, marginLeft: '0.5rem', fontSize: '0.8rem' }}>{spell.spell_args_json}</span>}
                                                </span>
                                                <button
                                                    type="button"
                                                    style={{ padding: '0.15rem 0.4rem', fontSize: '0.75rem', cursor: 'pointer' }}
                                                    onClick={async () => {
                                                        await fetch(`/api/people/${personaId}/realtime-spell/${spell.binding_id}`, { method: 'DELETE' });
                                                        setRealtimeSpells(prev => prev.filter(s => s.binding_id !== spell.binding_id));
                                                    }}
                                                >
                                                    削除
                                                </button>
                                            </div>
                                        ))}
                                    </div>
                                )}
                                <div style={{ border: '1px solid var(--border-color, #ddd)', borderRadius: '6px', padding: '0.75rem' }}>
                                    <div style={{ marginBottom: '0.5rem' }}>
                                        <select
                                            value={newSpellName}
                                            onChange={(e) => { setNewSpellName(e.target.value); setNewSpellArgs({}); }}
                                            style={{ width: '100%', padding: '0.3rem 0.5rem', fontSize: '0.85rem' }}
                                        >
                                            <option value="">スペルを選択...</option>
                                            {spellCatalog.map(s => (
                                                <option key={s.name} value={s.name}>{s.name} — {s.description.slice(0, 60)}</option>
                                            ))}
                                        </select>
                                    </div>
                                    {newSpellName && (() => {
                                        const selected = spellCatalog.find(s => s.name === newSpellName);
                                        if (!selected) return null;
                                        const props = selected.parameters.properties;
                                        const required = selected.parameters.required || [];
                                        return (
                                            <div style={{ marginBottom: '0.5rem' }}>
                                                {Object.entries(props).map(([key, spec]: [string, any]) => (
                                                    <div key={key} style={{ marginBottom: '0.35rem' }}>
                                                        <label style={{ fontSize: '0.8rem', display: 'block', marginBottom: '0.1rem' }}>
                                                            {key}{required.includes(key) ? ' *' : ''}
                                                            {spec.description && <span style={{ opacity: 0.6, marginLeft: '0.5rem' }}>{spec.description.slice(0, 50)}</span>}
                                                        </label>
                                                        <input
                                                            type="text"
                                                            value={newSpellArgs[key] || ''}
                                                            onChange={(e) => setNewSpellArgs(prev => ({ ...prev, [key]: e.target.value }))}
                                                            placeholder={spec.default != null ? `default: ${spec.default}` : ''}
                                                            style={{ width: '100%', padding: '0.25rem 0.5rem', fontSize: '0.85rem' }}
                                                        />
                                                    </div>
                                                ))}
                                            </div>
                                        );
                                    })()}
                                    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                                        <input
                                            type="text"
                                            placeholder="ラベル (表示名)"
                                            value={newSpellLabel}
                                            onChange={(e) => setNewSpellLabel(e.target.value)}
                                            style={{ flex: 1, padding: '0.25rem 0.5rem', fontSize: '0.85rem' }}
                                        />
                                        <button
                                            type="button"
                                            disabled={!newSpellName}
                                            style={{ padding: '0.3rem 0.75rem', fontSize: '0.85rem', cursor: newSpellName ? 'pointer' : 'not-allowed' }}
                                            onClick={async () => {
                                                if (!newSpellName) return;
                                                const argsObj: Record<string, any> = {};
                                                Object.entries(newSpellArgs).forEach(([k, v]) => {
                                                    if (!v.trim()) return;
                                                    try { argsObj[k] = JSON.parse(v.trim()); } catch { argsObj[k] = v.trim(); }
                                                });
                                                const argsJson = Object.keys(argsObj).length > 0 ? JSON.stringify(argsObj) : null;
                                                const res = await fetch(`/api/people/${personaId}/realtime-spell`, {
                                                    method: 'POST',
                                                    headers: { 'Content-Type': 'application/json' },
                                                    body: JSON.stringify({
                                                        spell_name: newSpellName,
                                                        spell_args_json: argsJson,
                                                        label: newSpellLabel.trim() || null,
                                                    }),
                                                });
                                                if (res.ok) {
                                                    const data = await res.json();
                                                    setRealtimeSpells(prev => [...prev, {
                                                        binding_id: data.binding_id,
                                                        spell_name: newSpellName,
                                                        spell_args_json: argsJson,
                                                        label: newSpellLabel.trim() || null,
                                                        enabled: true,
                                                        priority: 0,
                                                    }]);
                                                    setNewSpellName('');
                                                    setNewSpellArgs({});
                                                    setNewSpellLabel('');
                                                }
                                            }}
                                        >
                                            追加
                                        </button>
                                    </div>
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>リンクユーザー</label>
                                <select
                                    className={styles.select}
                                    value={linkedUserId}
                                    onChange={(e) => setLinkedUserId(e.target.value)}
                                >
                                    <option value="">なし（「ユーザー」と表示）</option>
                                    {availableUsers.map(u => (
                                        <option key={u.id} value={u.id}>{u.name}</option>
                                    ))}
                                </select>
                                <div className={styles.description}>
                                    このペルソナがリンクするユーザー。システムプロンプトに名前が表示されます。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>アバター</label>
                                <ImageUpload
                                    value={avatarPath}
                                    onChange={setAvatarPath}
                                    circle={true}
                                />
                                <div className={styles.description}>
                                    新しいアバター画像をアップロードします。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>外見画像（ビジュアルコンテキスト）</label>
                                <ImageUpload
                                    value={appearanceImagePath}
                                    onChange={setAppearanceImagePath}
                                />
                                <div className={styles.description}>
                                    LLMのビジュアルコンテキスト用の詳細な外見画像。アバターとは別です。
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>説明</label>
                                <input
                                    className={styles.input}
                                    value={description}
                                    onChange={(e) => setDescription(e.target.value)}
                                    placeholder="ペルソナの短い説明"
                                />
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>システムプロンプト</label>
                                <textarea
                                    className={styles.textarea}
                                    value={systemPrompt}
                                    onChange={(e) => setSystemPrompt(e.target.value)}
                                    placeholder="あなたは..."
                                />
                                <div className={styles.description}>
                                    行動、性格、能力を定義するコアな指示。
                                </div>
                            </div>

                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>キャンセル</button>
                    <button
                        className={styles.saveBtn}
                        onClick={handleSave}
                        disabled={isLoading || isSaving || !loadedPersonaId || loadedPersonaId !== personaId}
                        title={
                            isLoading ? '読み込み中…'
                                : !loadedPersonaId ? '読み込み未完了'
                                : loadedPersonaId !== personaId ? `表示中 (${loadedPersonaId}) と保存先 (${personaId}) が不一致のため無効`
                                : undefined
                        }
                    >
                        {isSaving ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                        保存
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
