
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
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
    language?: "ja" | "en" | null;
    home_city_language?: string | null;
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
    auto_recall_enhanced: boolean;
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
    useLocale();
    const [config, setConfig] = useState<AIConfig | null>(null);
    const [availableModels, setAvailableModels] = useState<ModelChoice[]>([]);
    const [availableUsers, setAvailableUsers] = useState<UserChoice[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);

    // Form state
    const [description, setDescription] = useState('');
    const [systemPrompt, setSystemPrompt] = useState('');
    const [language, setLanguage] = useState<string>('');
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
    const [autoRecallEnhanced, setAutoRecallEnhanced] = useState(false);
    const [memoryWeaveContext, setMemoryWeaveContext] = useState(true);
    const [memopediaIndexEnabled, setMemopediaIndexEnabled] = useState(true);
    const [coreMemoryCharBudget, setCoreMemoryCharBudget] = useState<string>('');
    const [chronicleCharBudget, setChronicleCharBudget] = useState<string>('');
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
            const res = await apiFetch('/api/info/models');
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
            const res = await apiFetch('/api/user/list');
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
            const res = await apiFetch(`/api/people/${targetPersonaId}/config`);
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
                setLanguage(data.language ?? '');
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
                setAutoRecallEnhanced(data.auto_recall_enhanced ?? false);
                setMemoryWeaveContext(data.memory_weave_context ?? true);
                setMemopediaIndexEnabled(data.memopedia_index_enabled ?? false);
                setChronicleCharBudget(
                    data.chronicle_char_budget != null
                        ? String(data.chronicle_char_budget)
                        : ''
                );
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
                        apiFetch(`/api/people/${personaId}/realtime-spell`),
                        apiFetch('/api/people/realtime-spell-catalog'),
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
                const costRes = await apiFetch(`/api/people/${targetPersonaId}/arasuji/cost-estimate`);
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
            alert(uiText("components.SettingsModal.text001"));
            return;
        }
        if (!loadedPersonaId || loadedPersonaId !== personaId) {
            alert(
                uiText("components.SettingsModal.text002") +
                uiText("components.SettingsModal.text003", { p1: loadedPersonaId ?? uiText("common.extra004") }) +
                uiText("components.SettingsModal.text004", { p1: personaId }) +
                uiText("components.SettingsModal.text005")
            );
            console.error(
                `[SettingsModal] handleSave rejected: loadedPersonaId=${loadedPersonaId} != personaId=${personaId}`
            );
            return;
        }

        setIsSaving(true);
        try {
            const res = await apiFetch(`/api/people/${personaId}/config`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    description: description,
                    system_prompt: systemPrompt,
                    language,
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
                    auto_recall_enhanced: autoRecallEnhanced,
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
                    // Chronicle 帯の読み込み文字数: コア記憶と同じ流儀 (空 = 0 を
                    // 送って NULL に倒し、既定 20,000 字の運用へ戻す)。
                    chronicle_char_budget: (() => {
                        const trimmed = chronicleCharBudget.trim();
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
                    alert(uiText("components.SettingsModal.text006", { p1: data.warning }));
                }
                onClose();
            } else {
                const err = await res.json();
                alert(uiText("components.SettingsModal.text007", { p1: err.detail }));
            }
        } catch (error) {
            console.error(error);
            alert(uiText("components.SettingsModal.text008"));
        } finally {
            setIsSaving(false);
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 data-i18n="components.SettingsModal.text009"><Settings size={22} />{uiText("components.SettingsModal.text009")}</h2>
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
                                <label data-i18n="components.SettingsModal.text010" className={styles.label}>{uiText("components.SettingsModal.text010")}</label>
                                <div className={styles.input} style={{ background: 'rgba(0,0,0,0.05)', color: '#888' }}>
                                    {config?.name}
                                </div>
                                <div data-i18n="components.SettingsModal.text011" className={styles.description}>{uiText("components.SettingsModal.text011")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text012" className={styles.label}>{uiText("components.SettingsModal.text012")}</label>
                                <select
                                    className={styles.select}
                                    value={defaultModel}
                                    onChange={(e) => setDefaultModel(e.target.value)}
                                >
                                    <option data-i18n="components.SettingsModal.text013" value="">{uiText("components.SettingsModal.text013")}</option>
                                    {defaultModel && !availableModels.some(m => m.id === defaultModel) && (
                                        <option data-i18n="components.SettingsModal.text014" value={defaultModel}>{uiText("components.SettingsModal.text014")}{defaultModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text015" className={styles.label}>{uiText("components.SettingsModal.text015")}</label>
                                <select
                                    className={styles.select}
                                    value={lightweightModel}
                                    onChange={(e) => setLightweightModel(e.target.value)}
                                >
                                    <option data-i18n="components.SettingsModal.text016" value="">{uiText("components.SettingsModal.text016")}</option>
                                    {lightweightModel && !availableModels.some(m => m.id === lightweightModel) && (
                                        <option data-i18n="components.SettingsModal.text017" value={lightweightModel}>{uiText("components.SettingsModal.text017")}{lightweightModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div data-i18n="components.SettingsModal.text018" className={styles.description}>{uiText("components.SettingsModal.text018")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text019" className={styles.label}>{uiText("components.SettingsModal.text019")}</label>
                                <select
                                    className={styles.select}
                                    value={memoryWeaveModel}
                                    onChange={(e) => setMemoryWeaveModel(e.target.value)}
                                >
                                    <option data-i18n="components.SettingsModal.text020" value="">{uiText("components.SettingsModal.text020")}</option>
                                    {memoryWeaveModel && !availableModels.some(m => m.id === memoryWeaveModel) && (
                                        <option data-i18n="components.SettingsModal.text021" value={memoryWeaveModel}>{uiText("components.SettingsModal.text021")}{memoryWeaveModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div data-i18n="components.SettingsModal.text022" className={styles.description}>{uiText("components.SettingsModal.text022")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text023" className={styles.label}>{uiText("components.SettingsModal.text023")}</label>
                                <select
                                    className={styles.select}
                                    value={visionModel}
                                    onChange={(e) => setVisionModel(e.target.value)}
                                >
                                    <option data-i18n="components.SettingsModal.text024" value="">{uiText("components.SettingsModal.text024")}</option>
                                    {visionModel && !availableModels.some(m => m.id === visionModel) && (
                                        <option data-i18n="components.SettingsModal.text025" value={visionModel}>{uiText("components.SettingsModal.text025")}{visionModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div data-i18n="components.SettingsModal.text026" className={styles.description}>{uiText("components.SettingsModal.text026")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text027" className={styles.label}>{uiText("components.SettingsModal.text027")}</label>
                                <select
                                    className={styles.select}
                                    value={audioModel}
                                    onChange={(e) => setAudioModel(e.target.value)}
                                >
                                    <option data-i18n="components.SettingsModal.text028" value="">{uiText("components.SettingsModal.text028")}</option>
                                    {audioModel && !availableModels.some(m => m.id === audioModel) && (
                                        <option data-i18n="components.SettingsModal.text029" value={audioModel}>{uiText("components.SettingsModal.text029")}{audioModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div data-i18n="components.SettingsModal.text030" className={styles.description}>{uiText("components.SettingsModal.text030")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text031" className={styles.label}>{uiText("components.SettingsModal.text031")}</label>
                                <select
                                    className={styles.select}
                                    value={videoModel}
                                    onChange={(e) => setVideoModel(e.target.value)}
                                >
                                    <option data-i18n="components.SettingsModal.text032" value="">{uiText("components.SettingsModal.text032")}</option>
                                    {videoModel && !availableModels.some(m => m.id === videoModel) && (
                                        <option data-i18n="components.SettingsModal.text033" value={videoModel}>{uiText("components.SettingsModal.text033")}{videoModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div data-i18n="components.SettingsModal.text034" className={styles.description}>{uiText("components.SettingsModal.text034")}</div>
                            </div>

                            <DebugPanel personaId={personaId} />

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text035" className={styles.label}>{uiText("components.SettingsModal.text035")}</label>
                                {(() => {
                                    // 実効値の解決 (default なら built-in default)
                                    const effectiveKeepCache = metaKeepCacheAlive === 'default'
                                        ? META_JUDGMENT_DEFAULTS.keep_cache_alive
                                        : metaKeepCacheAlive === 'on';
                                    return (
                                        <>
                                            <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                                    <span data-i18n="components.SettingsModal.text036" style={{ minWidth: '160px' }}>{uiText("components.SettingsModal.text036")}</span>
                                                    <select
                                                        className={styles.select}
                                                        value={metaKeepCacheAlive}
                                                        onChange={(e) => setMetaKeepCacheAlive(e.target.value as TriState)}
                                                        style={{ width: '14rem' }}
                                                    >
                                                        <option data-i18n="components.SettingsModal.text037" value="default">{uiText("components.SettingsModal.text037")}{META_JUDGMENT_DEFAULTS.keep_cache_alive ? 'ON' : 'OFF'})</option>
                                                        <option data-i18n="components.SettingsModal.text038" value="on">{uiText("components.SettingsModal.text038")}</option>
                                                        <option data-i18n="components.SettingsModal.text039" value="off">{uiText("components.SettingsModal.text039")}</option>
                                                    </select>
                                                </div>
                                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                                    <span data-i18n="components.SettingsModal.text040" style={{ minWidth: '160px' }}>{uiText("components.SettingsModal.text040")}</span>
                                                    <input data-i18n="components.SettingsModal.text041"
                                                        type="number"
                                                        step="0.05"
                                                        min="0"
                                                        max="1"
                                                        placeholder={String(META_JUDGMENT_DEFAULTS.cache_threshold_ratio)}
                                                        value={metaCacheThresholdRatio}
                                                        onChange={(e) => setMetaCacheThresholdRatio(e.target.value)}
                                                        style={{ width: '7rem' }}
                                                        disabled={!effectiveKeepCache}
                                                        title={effectiveKeepCache ? '' : uiText("components.SettingsModal.text041")}
                                                    />
                                                    <span data-i18n="components.SettingsModal.text042" style={{ fontSize: '0.85em', color: '#888' }}>{uiText("components.SettingsModal.text042")}{META_JUDGMENT_DEFAULTS.cache_threshold_ratio})
                                                    </span>
                                                </div>
                                            </div>
                                            <div data-i18n="components.SettingsModal.text043" className={styles.description}>{uiText("components.SettingsModal.text043")}</div>
                                        </>
                                    );
                                })()}
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text044" className={styles.label}>{uiText("components.SettingsModal.text044")}</label>
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
                                    <span data-i18n="components.SettingsModal.text045">{uiText("components.SettingsModal.text045")}</span>
                                    <span data-i18n="components.SettingsModal.text046" style={{ fontSize: '0.85em', color: '#888' }}>{uiText("components.SettingsModal.text046")}</span>
                                </div>
                                <div data-i18n="components.SettingsModal.text047" className={styles.description}>{uiText("components.SettingsModal.text047")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text048" className={styles.label}>{uiText("components.SettingsModal.text048")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={chronicleEnabled}
                                            onChange={(e) => setChronicleEnabled(e.target.checked)}
                                        />
                                        <span data-i18n="components.SettingsModal.text049 components.SettingsModal.text050">{chronicleEnabled ? uiText("components.SettingsModal.text049") : uiText("components.SettingsModal.text050")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.text051" className={styles.description}>{uiText("components.SettingsModal.text051")}</div>
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
                                        <div data-i18n="components.SettingsModal.text052 components.SettingsModal.text053">{uiText("components.SettingsModal.text052")}<strong>{costEstimate.unprocessed_messages.toLocaleString(getFormatLocale())}</strong>{uiText("components.SettingsModal.text053")}</div>
                                        <div data-i18n="components.SettingsModal.text054">{uiText("components.SettingsModal.text054")}<strong>
                                                {costEstimate.is_free_tier
                                                    ? `${formatCost(0, costEstimate.currency)} (Free tier)`
                                                    : formatCost(costEstimate.estimated_cost_usd, costEstimate.currency)
                                                }
                                            </strong>
                                            {' '}({costEstimate.model_name})
                                        </div>
                                        <div data-i18n="components.SettingsModal.text055 components.SettingsModal.text056">{uiText("components.SettingsModal.text055")}{costEstimate.estimated_llm_calls}{uiText("components.SettingsModal.text056")}</div>
                                    </div>
                                )}
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text057" className={styles.label}>{uiText("components.SettingsModal.text057")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <input
                                        type="number"
                                        step="1000"
                                        min="0"
                                        placeholder="20000"
                                        value={chronicleCharBudget}
                                        onChange={(e) => setChronicleCharBudget(e.target.value)}
                                        style={{ width: '7rem' }}
                                    />
                                    <span data-i18n="components.SettingsModal.text058">{uiText("components.SettingsModal.text058")}</span>
                                    <span data-i18n="components.SettingsModal.text059" style={{ fontSize: '0.85em', color: '#888' }}>{uiText("components.SettingsModal.text059")}</span>
                                </div>
                                <div data-i18n="components.SettingsModal.text060" className={styles.description}>{uiText("components.SettingsModal.text060")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text061" className={styles.label}>{uiText("components.SettingsModal.text061")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={autonomousChronicleEnabled}
                                            onChange={(e) => setAutonomousChronicleEnabled(e.target.checked)}
                                        />
                                        <span data-i18n="components.SettingsModal.text062 components.SettingsModal.text063">{autonomousChronicleEnabled ? uiText("components.SettingsModal.text062") : uiText("components.SettingsModal.text063")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.text064" className={styles.description}>{uiText("components.SettingsModal.text064")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text065" className={styles.label}>{uiText("components.SettingsModal.text065")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={autoRecallEnabled}
                                            onChange={(e) => setAutoRecallEnabled(e.target.checked)}
                                        />
                                        <span data-i18n="components.SettingsModal.text066 components.SettingsModal.text067">{autoRecallEnabled ? uiText("components.SettingsModal.text066") : uiText("components.SettingsModal.text067")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.text068" className={styles.description}>{uiText("components.SettingsModal.text068")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.autoRecallEnhancedLabel" className={styles.label}>{uiText("components.SettingsModal.autoRecallEnhancedLabel")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={autoRecallEnhanced}
                                            onChange={(e) => setAutoRecallEnhanced(e.target.checked)}
                                        />
                                        <span data-i18n="components.SettingsModal.text066 components.SettingsModal.text067">{autoRecallEnhanced ? uiText("components.SettingsModal.text066") : uiText("components.SettingsModal.text067")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.autoRecallEnhancedDescription" className={styles.description}>{uiText("components.SettingsModal.autoRecallEnhancedDescription")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text069" className={styles.label}>{uiText("components.SettingsModal.text069")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={memoryWeaveContext}
                                            onChange={(e) => setMemoryWeaveContext(e.target.checked)}
                                        />
                                        <span data-i18n="components.SettingsModal.text070 components.SettingsModal.text071">{memoryWeaveContext ? uiText("components.SettingsModal.text070") : uiText("components.SettingsModal.text071")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.text072" className={styles.description}>{uiText("components.SettingsModal.text072")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text073" className={styles.label}>{uiText("components.SettingsModal.text073")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={memopediaIndexEnabled}
                                            onChange={(e) => setMemopediaIndexEnabled(e.target.checked)}
                                        />
                                        <span data-i18n="components.SettingsModal.text074 components.SettingsModal.text075">{memopediaIndexEnabled ? uiText("components.SettingsModal.text074") : uiText("components.SettingsModal.text075")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.text076" className={styles.description}>{uiText("components.SettingsModal.text076")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text077" className={styles.label}>{uiText("components.SettingsModal.text077")}</label>
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
                                    <span data-i18n="components.SettingsModal.text078">{uiText("components.SettingsModal.text078")}</span>
                                    <span data-i18n="components.SettingsModal.text079" style={{ fontSize: '0.85em', color: '#888' }}>{uiText("components.SettingsModal.text079")}</span>
                                </div>
                                <div data-i18n="components.SettingsModal.text080" className={styles.description}>{uiText("components.SettingsModal.text080")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text081" className={styles.label}>{uiText("components.SettingsModal.text081")}</label>
                                <div style={{ display: 'flex', flexDirection: 'column', gap: '0.4rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="radio"
                                            name={`spell-mode-${personaId}`}
                                            checked={spellEnabled}
                                            onChange={() => setSpellEnabled(true)}
                                        />
                                        <span data-i18n="components.SettingsModal.spellModeStandard">{uiText("components.SettingsModal.spellModeStandard")}</span>
                                    </label>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="radio"
                                            name={`spell-mode-${personaId}`}
                                            checked={!spellEnabled}
                                            onChange={() => setSpellEnabled(false)}
                                        />
                                        <span data-i18n="components.SettingsModal.spellModeDisabled">{uiText("components.SettingsModal.spellModeDisabled")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.spellModeDescription" className={styles.description}>
                                    {uiText("components.SettingsModal.spellModeDescription")}
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text085" className={styles.label}>{uiText("components.SettingsModal.text085")}</label>
                                <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem' }}>
                                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', cursor: 'pointer' }}>
                                        <input
                                            type="checkbox"
                                            checked={realtimeInfoEnabled}
                                            onChange={(e) => setRealtimeInfoEnabled(e.target.checked)}
                                        />
                                        <span data-i18n="components.SettingsModal.text086 components.SettingsModal.text087">{realtimeInfoEnabled ? uiText("components.SettingsModal.text086") : uiText("components.SettingsModal.text087")}</span>
                                    </label>
                                </div>
                                <div data-i18n="components.SettingsModal.text088" className={styles.description}>{uiText("components.SettingsModal.text088")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text089" className={styles.label}>{uiText("components.SettingsModal.text089")}</label>
                                <div data-i18n="components.SettingsModal.text090" className={styles.description} style={{ marginBottom: '0.5rem' }}>
                                    {uiText("components.SettingsModal.text090")}
                                </div>
                                {!spellEnabled && (
                                    <div data-i18n="components.SettingsModal.spellDisabledNotice" className={styles.description} style={{ marginBottom: '0.5rem', fontWeight: 600 }}>
                                        {uiText("components.SettingsModal.spellDisabledNotice")}
                                    </div>
                                )}
                                {realtimeSpells.length > 0 && (
                                    <div style={{ marginBottom: '0.75rem' }}>
                                        {realtimeSpells.map((spell) => (
                                            <div key={spell.binding_id} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem', padding: '0.4rem 0.6rem', background: 'var(--bg-secondary, #f5f5f5)', borderRadius: '4px' }}>
                                                <span style={{ flex: 1, fontSize: '0.85rem' }}>
                                                    <strong>{spell.label || spell.spell_name}</strong>
                                                    {spell.spell_args_json && <span style={{ opacity: 0.6, marginLeft: '0.5rem', fontSize: '0.8rem' }}>{spell.spell_args_json}</span>}
                                                </span>
                                                <button data-i18n="components.SettingsModal.text091"
                                                    type="button"
                                                    style={{ padding: '0.15rem 0.4rem', fontSize: '0.75rem', cursor: 'pointer' }}
                                                    onClick={async () => {
                                                        await apiFetch(`/api/people/${personaId}/realtime-spell/${spell.binding_id}`, { method: 'DELETE' });
                                                        setRealtimeSpells(prev => prev.filter(s => s.binding_id !== spell.binding_id));
                                                    }}
                                                >{uiText("components.SettingsModal.text091")}</button>
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
                                            <option data-i18n="components.SettingsModal.text092" value="">{uiText("components.SettingsModal.text092")}</option>
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
                                        <input data-i18n="components.SettingsModal.text093"
                                            type="text"
                                            placeholder={uiText("components.SettingsModal.text093")}
                                            value={newSpellLabel}
                                            onChange={(e) => setNewSpellLabel(e.target.value)}
                                            style={{ flex: 1, padding: '0.25rem 0.5rem', fontSize: '0.85rem' }}
                                        />
                                        <button data-i18n="components.SettingsModal.text094"
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
                                                const res = await apiFetch(`/api/people/${personaId}/realtime-spell`, {
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
                                        >{uiText("components.SettingsModal.text094")}</button>
                                    </div>
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text095" className={styles.label}>{uiText("components.SettingsModal.text095")}</label>
                                <select
                                    className={styles.select}
                                    value={linkedUserId}
                                    onChange={(e) => setLinkedUserId(e.target.value)}
                                >
                                    <option data-i18n="components.SettingsModal.text096" value="">{uiText("components.SettingsModal.text096")}</option>
                                    {availableUsers.map(u => (
                                        <option key={u.id} value={u.id}>{u.name}</option>
                                    ))}
                                </select>
                                <div data-i18n="components.SettingsModal.text097" className={styles.description}>{uiText("components.SettingsModal.text097")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text098" className={styles.label}>{uiText("components.SettingsModal.text098")}</label>
                                <ImageUpload
                                    value={avatarPath}
                                    onChange={setAvatarPath}
                                    circle={true}
                                />
                                <div data-i18n="components.SettingsModal.text099" className={styles.description}>{uiText("components.SettingsModal.text099")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text100" className={styles.label}>{uiText("components.SettingsModal.text100")}</label>
                                <ImageUpload
                                    value={appearanceImagePath}
                                    onChange={setAppearanceImagePath}
                                />
                                <div data-i18n="components.SettingsModal.text101" className={styles.description}>{uiText("components.SettingsModal.text101")}</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text102" className={styles.label}>{uiText("components.SettingsModal.text102")}</label>
                                <input data-i18n="components.SettingsModal.text103"
                                    className={styles.input}
                                    value={description}
                                    onChange={(e) => setDescription(e.target.value)}
                                    placeholder={uiText("components.SettingsModal.text103")}
                                />
                            </div>

                            <div className={styles.fieldGroup}>
                                <label data-i18n="components.SettingsModal.text104" className={styles.label}>{uiText("components.SettingsModal.text104")}</label>
                                <select data-i18n="components.SettingsModal.text105" aria-label={uiText("components.SettingsModal.text105")} value={language} onChange={e => setLanguage(e.target.value)}>
                                    <option value="">
                                        {uiText("components.SettingsModal.languageInherit", {
                                            p1: (config?.home_city_language === "en" ? uiText("components.SettingsModal.label001") : uiText("components.SettingsModal.text106"))
                                        })}
                                    </option>
                                    <option data-i18n="components.SettingsModal.text106" value="ja">{uiText("components.SettingsModal.text106")}</option>
                                    <option value="en">{uiText("components.SettingsModal.label001")}</option>
                                </select>
                                <p data-i18n="components.SettingsModal.text107">{uiText("components.SettingsModal.text107")}</p>
                                <label data-i18n="components.SettingsModal.text108" className={styles.label}>{uiText("components.SettingsModal.text108")}</label>
                                <textarea data-i18n="components.SettingsModal.text109"
                                    className={styles.textarea}
                                    value={systemPrompt}
                                    onChange={(e) => setSystemPrompt(e.target.value)}
                                    placeholder={uiText("components.SettingsModal.text109")}
                                />
                                <div data-i18n="components.SettingsModal.text110" className={styles.description}>{uiText("components.SettingsModal.text110")}</div>
                            </div>

                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button data-i18n="components.SettingsModal.text111" className={styles.cancelBtn} onClick={onClose}>{uiText("components.SettingsModal.text111")}</button>
                    <button data-i18n="components.SettingsModal.text112 components.SettingsModal.text113 components.SettingsModal.text114 components.SettingsModal.text115"
                        className={styles.saveBtn}
                        onClick={handleSave}
                        disabled={isLoading || isSaving || !loadedPersonaId || loadedPersonaId !== personaId}
                        title={
                            isLoading ? uiText("components.SettingsModal.text112")
                                : !loadedPersonaId ? uiText("components.SettingsModal.text113")
                                : loadedPersonaId !== personaId ? uiText("components.SettingsModal.text114", { p1: loadedPersonaId, p2: personaId })
                                : undefined
                        }
                    >
                        {isSaving ? <Loader2 size={16} className="spin" /> : <Save size={16} />}{uiText("components.SettingsModal.text115")}</button>
                </div>
            </div>
        </ModalOverlay>
    );
}
