import React, { useState, useEffect, useRef } from 'react';
import { X, Save, Loader2, Settings } from 'lucide-react';
import styles from './SettingsModal.module.css';
import ImageUpload from './common/ImageUpload';
import ModalOverlay from './common/ModalOverlay';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

interface AIConfig {
    name: string;
    description: string;
    system_prompt: string;
    default_model: string | null;
    lightweight_model: string | null;
    activity_state: string;  // 'Stop' / 'Sleep' / 'Idle' / 'Active'
    chronicle_enabled: boolean;
    memory_weave_context: boolean;
    avatar_path: string | null;
    appearance_image_path: string | null;  // Visual context appearance image
    linked_user_id: number | null;  // First linked user ID
}

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
    const [activityState, setActivityState] = useState<string>('Idle');
    const [chronicleEnabled, setChronicleEnabled] = useState(true);
    const [memoryWeaveContext, setMemoryWeaveContext] = useState(true);
    const [spellEnabled, setSpellEnabled] = useState(false);
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
                setActivityState(data.activity_state || 'Idle');
                setChronicleEnabled(data.chronicle_enabled ?? true);
                setMemoryWeaveContext(data.memory_weave_context ?? true);
                setSpellEnabled(data.spell_enabled ?? false);
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
                    default_model: defaultModel,  // empty string = clear to None
                    lightweight_model: lightweightModel,  // empty string = clear to None
                    activity_state: activityState,
                    chronicle_enabled: chronicleEnabled,
                    memory_weave_context: memoryWeaveContext,
                    spell_enabled: spellEnabled,
                    avatar_path: avatarPath || null,
                    appearance_image_path: appearanceImagePath || null,
                    linked_user_id: linkedUserId ? parseInt(linkedUserId) : 0  // 0 = clear link
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
