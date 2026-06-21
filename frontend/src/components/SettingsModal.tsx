import React, { useState, useEffect, useRef } from 'react';
import { X, Save, Loader2, Settings } from 'lucide-react';
import styles from './SettingsModal.module.css';
import ImageUpload from './common/ImageUpload';
import ModalOverlay from './common/ModalOverlay';
import DebugPanel from './DebugPanel';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

interface MetaJudgmentConfig {
    cache_threshold_ratio: number | null;
    max_retries: number | null;
    retry_backoff_seconds: number | null;
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
    activity_state: string;  // 'Stop' / 'Sleep' / 'Idle' / 'Active'
    chronicle_enabled: boolean;
    memory_weave_context: boolean;
    realtime_info_enabled: boolean;
    avatar_path: string | null;
    appearance_image_path: string | null;  // Visual context appearance image
    linked_user_id: number | null;  // First linked user ID
    meta_judgment_config: MetaJudgmentConfig | null;  // Phase 4-e
    user_conv_timeout_minutes: number | null;  // 2026-05-09 wait_response auto-pause
}

// Built-in defaults — must stay in sync with saiverse/meta_layer.py:_DEFAULT_JUDGMENT_CONFIG
const META_JUDGMENT_DEFAULTS = {
    cache_threshold_ratio: 0.3,
    max_retries: 1,
    retry_backoff_seconds: 5,
    periodic_interval_minutes: 50,
    keep_cache_alive: true,
};

// 自動発話間隔がこれを超えていてかつ keep_cache_alive が ON だと、cache 維持で
// 高頻度に LLM が走るためコスト警告を出す
const KEEP_CACHE_ALIVE_WARNING_INTERVAL_MINUTES = 60;

interface ChronicleCostEstimate {
    total_messages: number;
    processed_messages: number;
    unprocessed_messages: number;
    estimated_llm_calls: number;
    estimated_cost_usd: number;
    model_name: string;
    is_free_tier: boolean;
    batch_size: number;
}

interface UserChoice {
    id: number;
    name: string;
}

interface ModelChoice {
    id: string;
    name: string;
}

const ACTIVITY_STATES = [
    { value: 'Active', label: '🟢 Active - 活発に自律稼働' },
    { value: 'Idle', label: '🟡 Idle - 起きてるが自発的には行動しない' },
    { value: 'Sleep', label: '🔵 Sleep - 寝てる (ユーザー発言で起きる)' },
    { value: 'Stop', label: '⚫ Stop - 機能停止' },
];

interface AutonomousStatus {
    activity_state: string;
    system_running: boolean;
    is_active: boolean;
}

interface AutonomyStatus {
    persona_id: string;
    state: string;
    interval_minutes: number;
    decision_model: string | null;
    execution_model: string | null;
    stelis_thread_id: string | null;
    current_cycle_id: string | null;
    last_report: {
        cycle_id: string;
        playbook: string | null;
        intent: string;
        status: string;
    } | null;
}

export default function SettingsModal({ isOpen, onClose, personaId }: SettingsModalProps) {
    const [config, setConfig] = useState<AIConfig | null>(null);
    const [availableModels, setAvailableModels] = useState<ModelChoice[]>([]);
    const [availableUsers, setAvailableUsers] = useState<UserChoice[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [autonomousStatus, setAutonomousStatus] = useState<AutonomousStatus | null>(null);
    const [autonomyStatus, setAutonomyStatus] = useState<AutonomyStatus | null>(null);
    const [autonomyInterval, setAutonomyInterval] = useState(5);
    const [isAutonomyLoading, setIsAutonomyLoading] = useState(false);
    const [developerMode, setDeveloperMode] = useState(false);

    // Form state
    const [description, setDescription] = useState('');
    const [systemPrompt, setSystemPrompt] = useState('');
    const [defaultModel, setDefaultModel] = useState<string>('');
    const [lightweightModel, setLightweightModel] = useState<string>('');
    const [visionModel, setVisionModel] = useState<string>('');
    const [audioModel, setAudioModel] = useState<string>('');
    const [videoModel, setVideoModel] = useState<string>('');
    const [memoryWeaveModel, setMemoryWeaveModel] = useState<string>('');
    const [activityState, setActivityState] = useState<string>('Idle');
    const [chronicleEnabled, setChronicleEnabled] = useState(true);
    const [memoryWeaveContext, setMemoryWeaveContext] = useState(true);
    const [spellEnabled, setSpellEnabled] = useState(false);
    const [realtimeInfoEnabled, setRealtimeInfoEnabled] = useState(true);
    const [realtimeSpells, setRealtimeSpells] = useState<Array<{binding_id: number; spell_name: string; spell_args_json: string | null; label: string | null; enabled: boolean; priority: number}>>([]);
    const [spellCatalog, setSpellCatalog] = useState<Array<{name: string; description: string; parameters: {properties: Record<string, any>; required: string[]}}>>([]);
    const [newSpellName, setNewSpellName] = useState('');
    const [newSpellArgs, setNewSpellArgs] = useState<Record<string, string>>({});
    const [newSpellLabel, setNewSpellLabel] = useState('');
    // Phase 4-e: empty string = use built-in default (NULL in DB)
    const [metaCacheThresholdRatio, setMetaCacheThresholdRatio] = useState<string>('');
    const [metaMaxRetries, setMetaMaxRetries] = useState<string>('');
    const [metaRetryBackoffSeconds, setMetaRetryBackoffSeconds] = useState<string>('');
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
            fetch('/api/config/developer-mode')
                .then(res => res.ok ? res.json() : null)
                .then(data => { if (data) setDeveloperMode(data.enabled); })
                .catch(() => {});
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
                setActivityState(data.activity_state || 'Idle');
                setChronicleEnabled(data.chronicle_enabled ?? true);
                setMemoryWeaveContext(data.memory_weave_context ?? true);
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
                setMetaMaxRetries(
                    mjc?.max_retries != null ? String(mjc.max_retries) : ''
                );
                setMetaRetryBackoffSeconds(
                    mjc?.retry_backoff_seconds != null ? String(mjc.retry_backoff_seconds) : ''
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

            // Also load autonomous status
            const statusRes = await fetch(`/api/people/${targetPersonaId}/autonomous/status`);
            if (isStale()) return;
            if (statusRes.ok) {
                const statusData = await statusRes.json();
                if (isStale()) return;
                setAutonomousStatus(statusData);
            }

            // Load autonomy manager status
            const autonomyRes = await fetch(`/api/people/${targetPersonaId}/autonomy`);
            if (isStale()) return;
            if (autonomyRes.ok) {
                const autonomyData = await autonomyRes.json();
                if (isStale()) return;
                setAutonomyStatus(autonomyData);
                setAutonomyInterval(autonomyData.interval_minutes);
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
                    activity_state: activityState,
                    chronicle_enabled: chronicleEnabled,
                    memory_weave_context: memoryWeaveContext,
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
                        const retries = metaMaxRetries.trim();
                        const backoff = metaRetryBackoffSeconds.trim();
                        const keepCache = metaKeepCacheAlive;
                        if (ratio) obj.cache_threshold_ratio = parseFloat(ratio);
                        else delete obj.cache_threshold_ratio;
                        if (retries) obj.max_retries = parseInt(retries);
                        else delete obj.max_retries;
                        if (backoff) obj.retry_backoff_seconds = parseInt(backoff);
                        else delete obj.retry_backoff_seconds;
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

    const fetchAutonomyStatus = async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/autonomy`);
            if (res.ok) {
                const data = await res.json();
                setAutonomyStatus(data);
                setAutonomyInterval(data.interval_minutes);
            }
        } catch (e) {
            console.error('Failed to fetch autonomy status:', e);
        }
    };

    const handleAutonomyStart = async () => {
        setIsAutonomyLoading(true);
        try {
            const res = await fetch(`/api/people/${personaId}/autonomy/start`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ interval_minutes: autonomyInterval }),
            });
            if (res.ok) await fetchAutonomyStatus();
        } catch (e) {
            console.error('Failed to start autonomy:', e);
        } finally {
            setIsAutonomyLoading(false);
        }
    };

    const handleAutonomyStop = async () => {
        setIsAutonomyLoading(true);
        try {
            const res = await fetch(`/api/people/${personaId}/autonomy/stop`, {
                method: 'POST',
            });
            if (res.ok) await fetchAutonomyStatus();
        } catch (e) {
            console.error('Failed to stop autonomy:', e);
        } finally {
            setIsAutonomyLoading(false);
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

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>アクティビティ状態</label>
                                <select
                                    className={styles.select}
                                    value={activityState}
                                    onChange={(e) => setActivityState(e.target.value)}
                                >
                                    {ACTIVITY_STATES.map(m => (
                                        <option key={m.value} value={m.value}>{m.label}</option>
                                    ))}
                                </select>
                                {autonomousStatus && (
                                    <div className={styles.description} style={{
                                        marginTop: '0.5rem',
                                        padding: '0.5rem',
                                        background: autonomousStatus.is_active ? 'rgba(0, 200, 0, 0.1)' : 'rgba(100, 100, 100, 0.1)',
                                        borderRadius: '4px'
                                    }}>
                                        {autonomousStatus.is_active ? (
                                            <span>✅ <strong>Active</strong> - このペルソナは自発的に発言します。</span>
                                        ) : autonomousStatus.system_running ? (
                                            <span>⏸️ 自律システムは動作中ですが、このペルソナは {activityState} 状態です。</span>
                                        ) : (
                                            <span>⚠️ 自律システムは動作していません。</span>
                                        )}
                                    </div>
                                )}
                            </div>

                            {/* Autonomy Manager Control */}
                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>自律行動マネージャー</label>
                                <div style={{
                                    padding: '0.75rem',
                                    background: 'rgba(100, 100, 100, 0.1)',
                                    borderRadius: '6px',
                                    display: 'flex',
                                    flexDirection: 'column',
                                    gap: '0.5rem',
                                }}>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
                                        <span style={{
                                            fontSize: '0.85rem',
                                            padding: '2px 8px',
                                            borderRadius: '10px',
                                            background: autonomyStatus?.state === 'stopped'
                                                ? 'rgba(100,100,100,0.2)'
                                                : autonomyStatus?.state === 'waiting'
                                                    ? 'rgba(255,193,7,0.15)'
                                                    : 'rgba(0,200,0,0.15)',
                                            color: autonomyStatus?.state === 'stopped'
                                                ? '#888'
                                                : autonomyStatus?.state === 'waiting'
                                                    ? '#ffd43b'
                                                    : '#69db7c',
                                        }}>
                                            {autonomyStatus?.state || 'stopped'}
                                        </span>

                                        <label style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontSize: '0.85rem' }}>
                                            間隔:
                                            <input
                                                type="number"
                                                min={1}
                                                max={120}
                                                step={1}
                                                value={autonomyInterval}
                                                onChange={(e) => setAutonomyInterval(Number(e.target.value))}
                                                disabled={autonomyStatus?.state !== 'stopped'}
                                                style={{
                                                    width: '4rem',
                                                    padding: '2px 6px',
                                                    borderRadius: '4px',
                                                    border: '1px solid #444',
                                                    background: 'transparent',
                                                    color: 'inherit',
                                                    textAlign: 'center',
                                                }}
                                            />
                                            分
                                        </label>

                                        {(!autonomyStatus || autonomyStatus.state === 'stopped') ? (
                                            <button
                                                onClick={handleAutonomyStart}
                                                disabled={isAutonomyLoading}
                                                style={{
                                                    padding: '4px 12px',
                                                    borderRadius: '4px',
                                                    border: '1px solid #2b8a3e',
                                                    background: 'rgba(43, 138, 62, 0.1)',
                                                    color: '#69db7c',
                                                    cursor: 'pointer',
                                                    fontSize: '0.85rem',
                                                }}
                                            >
                                                開始
                                            </button>
                                        ) : (
                                            <button
                                                onClick={handleAutonomyStop}
                                                disabled={isAutonomyLoading}
                                                style={{
                                                    padding: '4px 12px',
                                                    borderRadius: '4px',
                                                    border: '1px solid #c92a2a',
                                                    background: 'rgba(201, 42, 42, 0.1)',
                                                    color: '#ff6b6b',
                                                    cursor: 'pointer',
                                                    fontSize: '0.85rem',
                                                }}
                                            >
                                                停止
                                            </button>
                                        )}
                                    </div>

                                    {autonomyStatus?.last_report && (
                                        <div style={{ fontSize: '0.8rem', color: '#888' }}>
                                            前回: {autonomyStatus.last_report.playbook || '—'} / {autonomyStatus.last_report.status}
                                            {autonomyStatus.last_report.intent && (
                                                <span> — {autonomyStatus.last_report.intent.slice(0, 50)}</span>
                                            )}
                                        </div>
                                    )}
                                </div>
                            </div>

                            <DebugPanel personaId={personaId} />

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>メタ判断 Pulse 設定</label>
                                {(() => {
                                    // 実効値の解決 (default なら built-in default)
                                    const effectiveKeepCache = metaKeepCacheAlive === 'default'
                                        ? META_JUDGMENT_DEFAULTS.keep_cache_alive
                                        : metaKeepCacheAlive === 'on';
                                    // 自動発話間隔は自律行動マネージャー UI で編集する (autonomyInterval が source of truth)
                                    const intervalNum = autonomyInterval;
                                    const showCostWarning = effectiveKeepCache &&
                                        intervalNum > KEEP_CACHE_ALIVE_WARNING_INTERVAL_MINUTES;
                                    return (
                                        <>
                                            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                                    <span style={{ minWidth: '160px' }}>失敗時リトライ回数</span>
                                                    <input
                                                        type="number"
                                                        step="1"
                                                        min="0"
                                                        placeholder={String(META_JUDGMENT_DEFAULTS.max_retries)}
                                                        value={metaMaxRetries}
                                                        onChange={(e) => setMetaMaxRetries(e.target.value)}
                                                        style={{ width: '7rem' }}
                                                    />
                                                    <span style={{ fontSize: '0.85em', color: '#888' }}>
                                                        (既定: {META_JUDGMENT_DEFAULTS.max_retries})
                                                    </span>
                                                </div>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                                    <span style={{ minWidth: '160px' }}>リトライ待機秒数</span>
                                                    <input
                                                        type="number"
                                                        step="1"
                                                        min="0"
                                                        placeholder={String(META_JUDGMENT_DEFAULTS.retry_backoff_seconds)}
                                                        value={metaRetryBackoffSeconds}
                                                        onChange={(e) => setMetaRetryBackoffSeconds(e.target.value)}
                                                        style={{ width: '7rem' }}
                                                    />
                                                    <span style={{ fontSize: '0.85em', color: '#888' }}>
                                                        (既定: {META_JUDGMENT_DEFAULTS.retry_backoff_seconds}秒)
                                                    </span>
                                                </div>
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
                                                メインモデルのキャッシュ TTL 残り割合が「キャッシュ閾値」を下回ると、メタ判断 Pulse を前倒しで発火します。Pulse が失敗した場合は「失敗時リトライ回数」の上限まで「リトライ待機秒数」を空けて再試行します。「キャッシュ維持」を OFF にすると TTL 接近時の前倒しを行わず、自律行動マネージャーの「間隔」ぴったりに走ります (24 時間間隔等の低頻度ペルソナ向け)。空欄の項目は既定値が適用されます。
                                            </div>
                                            {showCostWarning && (
                                                <div style={{
                                                    marginTop: '0.5rem',
                                                    padding: '0.5rem 0.75rem',
                                                    background: 'rgba(255, 200, 0, 0.12)',
                                                    border: '1px solid rgba(255, 200, 0, 0.4)',
                                                    borderRadius: '4px',
                                                    fontSize: '0.85em',
                                                    color: '#bf8700',
                                                }}>
                                                    ⚠ 自律行動マネージャーの間隔が長く ({intervalNum}分) かつキャッシュ維持が ON のため、TTL ベースで頻繁にメタ判断が走り API コストが増える可能性があります。低頻度運用が目的なら「キャッシュ維持」を OFF にしてください。
                                                </div>
                                            )}
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
                                    対ユーザー会話 Track のような応答待ち型 Track が、最終メッセージからこの分数以上 idle になると自動的に pending に落とし、メタ判断 Pulse を発火します。長期 idle で自律稼働が止まる事故の脱出経路として動作します。軽量モデルなら短く (10〜15 分)、重量級モデルや人間の応答間隔が長い運用なら長く (60 分以上) 設定してください。
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
                                    Metabolism（記憶の整理）時にChronicle（あらすじ）を自動生成します。LLM APIコストが発生します。
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
                                        <div>未処理メッセージ: <strong>{costEstimate.unprocessed_messages.toLocaleString()}</strong>件</div>
                                        <div>
                                            推定コスト: <strong>
                                                {costEstimate.is_free_tier
                                                    ? '$0.00 (Free tier)'
                                                    : costEstimate.estimated_cost_usd < 0.001
                                                        ? `~$${costEstimate.estimated_cost_usd.toFixed(6)}`
                                                        : costEstimate.estimated_cost_usd < 0.01
                                                            ? `~$${costEstimate.estimated_cost_usd.toFixed(4)}`
                                                            : `~$${costEstimate.estimated_cost_usd.toFixed(3)}`
                                                }
                                            </strong>
                                            {' '}({costEstimate.model_name})
                                        </div>
                                        <div>推定LLM呼び出し: {costEstimate.estimated_llm_calls}回</div>
                                    </div>
                                )}
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
                                <label className={styles.label}>リアルタイムスペル</label>
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
                                                Object.entries(newSpellArgs).forEach(([k, v]) => { if (v.trim()) argsObj[k] = v.trim(); });
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
