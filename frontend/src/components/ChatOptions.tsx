import React, { useEffect, useState } from 'react';
import styles from './ChatOptions.module.css';
import { X } from 'lucide-react';

interface ModelInfo {
    id: string;
    name: string;
}

interface PlaybookParamOption {
    value: string;
    label: string;
}

interface PlaybookParam {
    name: string;
    description: string;
    param_type: string;
    required: boolean;
    default: any;
    enum_values?: string[];
    enum_source?: string;
    user_configurable: boolean;
    ui_widget?: string;
    resolved_options?: PlaybookParamOption[];
}

interface PlaybookInfo {
    id: string;
    name: string;
    description?: string;
    input_schema?: PlaybookParam[];
}

interface ParamSpec {
    label: string;
    type: 'slider' | 'number' | 'dropdown' | 'text';
    default: any;
    min?: number;
    max?: number;
    step?: number;
    options?: string[];
    description?: string;
}

interface CacheConfig {
    enabled: boolean;
    ttl: string;
    supported: boolean;
    ttl_options: string[];
    cache_type: string | null;
}

interface ChatOptionsProps {
    isOpen: boolean;
    onClose: () => void;
    currentPlaybook: string | null;
    onPlaybookChange: (id: string | null) => void;
    playbookParams: Record<string, any>;
    onPlaybookParamsChange: (params: Record<string, any>) => void;
    currentModel: string;
    onModelChange: (model: string, displayName: string) => void;
}

export default function ChatOptions({ isOpen, onClose, currentPlaybook, onPlaybookChange, playbookParams, onPlaybookParamsChange, currentModel: propCurrentModel, onModelChange }: ChatOptionsProps) {
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [playbooks, setPlaybooks] = useState<PlaybookInfo[]>([]);
    const [currentModel, setCurrentModel] = useState<string>('');
    const [params, setParams] = useState<Record<string, any>>({});
    const [paramSpecs, setParamSpecs] = useState<Record<string, ParamSpec>>({});
    const [loading, setLoading] = useState(false);
    const [playbookParamSpecs, setPlaybookParamSpecs] = useState<PlaybookParam[]>([]);
    const [cacheConfig, setCacheConfig] = useState<CacheConfig>({
        enabled: true,
        ttl: '5m',
        supported: false,
        ttl_options: [],
        cache_type: null
    });
    const [maxHistoryMessages, setMaxHistoryMessages] = useState<number | null>(null);
    const [maxHistoryMessagesDefault, setMaxHistoryMessagesDefault] = useState<number | null>(null);
    const [metabolismEnabled, setMetabolismEnabled] = useState<boolean>(true);
    const [metabolismKeepMessages, setMetabolismKeepMessages] = useState<number | null>(null);
    const [metabolismKeepMessagesDefault, setMetabolismKeepMessagesDefault] = useState<number | null>(null);

    useEffect(() => {
        if (isOpen) {
            fetchData();
        }
    }, [isOpen]);

    const fetchData = async () => {
        setLoading(true);
        try {
            const [modelsRes, playbooksRes, configRes, cacheRes] = await Promise.all([
                fetch('/api/config/models'),
                fetch('/api/config/playbooks'),
                fetch('/api/config/config'),
                fetch('/api/config/cache')
            ]);

            let fetchedModels: ModelInfo[] = [];
            if (modelsRes.ok) {
                fetchedModels = await modelsRes.json();
                setModels(fetchedModels);
            }
            if (playbooksRes.ok) setPlaybooks(await playbooksRes.json());

            if (configRes.ok) {
                const config = await configRes.json();
                const modelId = config.current_model || '';
                setCurrentModel(modelId);
                // Find display name from models list
                const modelInfo = fetchedModels.find(m => m.id === modelId);
                onModelChange(modelId, modelInfo?.name || ''); // Sync with parent
                setParamSpecs(config.parameters || {});
                setParams(config.current_values || {});
                setMaxHistoryMessages(config.max_history_messages ?? null);
                setMaxHistoryMessagesDefault(config.max_history_messages_model_default ?? null);
                setMetabolismEnabled(config.metabolism_enabled ?? true);
                setMetabolismKeepMessages(config.metabolism_keep_messages ?? null);
                setMetabolismKeepMessagesDefault(config.metabolism_keep_messages_model_default ?? null);
            }

            if (cacheRes.ok) {
                const cache = await cacheRes.json();
                setCacheConfig(cache);
            }

            // If a playbook is already selected, fetch its parameters
            if (currentPlaybook) {
                const paramsRes = await fetch(`/api/config/playbooks/${encodeURIComponent(currentPlaybook)}/params`);
                if (paramsRes.ok) {
                    const data = await paramsRes.json();
                    setPlaybookParamSpecs(data.params || []);
                }
            }
        } catch (e) {
            console.error("Failed to load config", e);
        } finally {
            setLoading(false);
        }
    };

    const handleModelChange = async (modelId: string) => {
        setCurrentModel(modelId);
        // Find display name from models list
        const modelInfo = models.find(m => m.id === modelId);
        onModelChange(modelId, modelInfo?.name || ''); // Notify parent component
        // Save immediately
        try {
            const res = await fetch('/api/config/model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: modelId })
            });
            if (res.ok) {
                // Use inline parameters from response (no separate fetch needed)
                const data = await res.json();
                setParamSpecs(data.parameters || {});
                setParams(data.current_values || {});
                setMaxHistoryMessages(data.max_history_messages ?? null);
                setMaxHistoryMessagesDefault(data.max_history_messages_model_default ?? null);
                setMetabolismEnabled(data.metabolism_enabled ?? true);
                setMetabolismKeepMessages(data.metabolism_keep_messages ?? null);
                setMetabolismKeepMessagesDefault(data.metabolism_keep_messages_model_default ?? null);
            }

            // Refetch cache config since it depends on selected model
            const cacheRes = await fetch('/api/config/cache');
            if (cacheRes.ok) {
                setCacheConfig(await cacheRes.json());
            }
        } catch (e) {
            console.error("Failed to set model", e);
        }
    };

    const handleParamChange = (key: string, value: any) => {
        const newParams = { ...params, [key]: value };
        setParams(newParams);
    };

    const handlePlaybookChange = async (playbookId: string | null) => {
        onPlaybookChange(playbookId);
        // Reset playbook params when changing playbook
        onPlaybookParamsChange({});
        setPlaybookParamSpecs([]);

        // Save to server immediately (include empty params to reset server-side)
        try {
            await fetch('/api/config/playbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playbook: playbookId, playbook_params: {} })
            });
        } catch (e) {
            console.error("Failed to save playbook", e);
        }

        // Fetch playbook params if a playbook is selected
        if (playbookId) {
            try {
                const res = await fetch(`/api/config/playbooks/${encodeURIComponent(playbookId)}/params`);
                if (res.ok) {
                    const data = await res.json();
                    setPlaybookParamSpecs(data.params || []);
                }
            } catch (e) {
                console.error("Failed to fetch playbook params", e);
            }
        }
    };

    const handlePlaybookParamChange = async (paramName: string, value: any) => {
        const newParams = { ...playbookParams, [paramName]: value };
        onPlaybookParamsChange(newParams);

        // Save to server immediately
        try {
            await fetch('/api/config/playbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    playbook: currentPlaybook,
                    playbook_params: newParams
                })
            });
        } catch (e) {
            console.error("Failed to save playbook params", e);
        }
    };

    const handleMaxHistoryMessagesInput = (value: string) => {
        const numValue = value === '' ? null : parseInt(value, 10);
        if (numValue !== null && (isNaN(numValue) || numValue < 1)) return;
        setMaxHistoryMessages(numValue);
    };

    const handleMaxHistoryMessagesCommit = async () => {
        try {
            await fetch('/api/config/max-history-messages', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value: maxHistoryMessages })
            });
        } catch (e) {
            console.error("Failed to update max history messages", e);
        }
    };

    const handleMetabolismEnabledChange = async (enabled: boolean) => {
        setMetabolismEnabled(enabled);
        try {
            await fetch('/api/config/metabolism', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled })
            });
        } catch (e) {
            console.error("Failed to update metabolism settings", e);
        }
    };

    const handleMetabolismKeepMessagesInput = (value: string) => {
        // Local state only — API call deferred to onBlur to avoid
        // intermediate values (e.g. "4" while typing "40") being rejected.
        const numValue = value === '' ? null : parseInt(value, 10);
        if (numValue !== null && (isNaN(numValue) || numValue < 1)) return;
        setMetabolismKeepMessages(numValue);
    };

    const getMaxKeepMessages = (): number | null => {
        const highWm = maxHistoryMessages ?? maxHistoryMessagesDefault;
        return highWm != null ? Math.max(1, highWm - 20) : null;
    };

    const handleMetabolismKeepMessagesCommit = async () => {
        let numValue = metabolismKeepMessages;

        // Auto-clamp to max allowed value
        const maxAllowed = getMaxKeepMessages();
        if (numValue != null && maxAllowed != null && numValue > maxAllowed) {
            numValue = maxAllowed;
            setMetabolismKeepMessages(numValue);
        }

        try {
            await fetch('/api/config/metabolism', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ keep_messages: numValue })
            });
        } catch (e) {
            console.error("Failed to update metabolism keep_messages", e);
        }
    };

    const handleCacheEnabledChange = async (enabled: boolean) => {
        const newConfig = { ...cacheConfig, enabled };
        setCacheConfig(newConfig);
        try {
            await fetch('/api/config/cache', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled })
            });
        } catch (e) {
            console.error("Failed to update cache settings", e);
        }
    };

    const handleCacheTtlChange = async (ttl: string) => {
        const newConfig = { ...cacheConfig, ttl };
        setCacheConfig(newConfig);
        try {
            await fetch('/api/config/cache', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ttl })
            });
        } catch (e) {
            console.error("Failed to update cache TTL", e);
        }
    };

    const saveParams = async () => {
        try {
            await fetch('/api/config/parameters', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ parameters: params })
            });
            onClose();
        } catch (e) {
            console.error("Failed to save params", e);
        }
    };

    if (!isOpen) return null;

    return (
        <div className={styles.overlay}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h2>チャットオプション</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div>設定を読み込み中...</div>
                    ) : (
                        <>
                            <div className={styles.section}>
                                <div className={styles.sectionTitle}>一般</div>
                                <div className={styles.formGroup}>
                                    <label>モデル</label>
                                    <select
                                        className={styles.select}
                                        value={currentModel}
                                        onChange={(e) => handleModelChange(e.target.value)}
                                    >
                                        <option value="">（デフォルト）</option>
                                        {models.map(m => (
                                            <option key={m.id} value={m.id}>{m.name}</option>
                                        ))}
                                    </select>
                                </div>
                                <div className={styles.formGroup}>
                                    <label>Playbook（次のメッセージに適用）</label>
                                    <select
                                        className={styles.select}
                                        value={currentPlaybook || ''}
                                        onChange={(e) => handlePlaybookChange(e.target.value || null)}
                                    >
                                        <option value="">（自動検出）</option>
                                        {playbooks.map(p => (
                                            <option key={p.id} value={p.id}>{p.name}</option>
                                        ))}
                                    </select>
                                </div>
                                <div className={styles.formGroup}>
                                    <label>
                                        メッセージ数上限
                                        {maxHistoryMessagesDefault != null && (
                                            <span className={styles.hint}> （モデルデフォルト: {maxHistoryMessagesDefault}）</span>
                                        )}
                                    </label>
                                    <input
                                        type="number"
                                        className={styles.input}
                                        min={1}
                                        max={500}
                                        value={maxHistoryMessages ?? ''}
                                        placeholder={maxHistoryMessagesDefault ? `（自動: ${maxHistoryMessagesDefault}）` : '（自動）'}
                                        onChange={(e) => handleMaxHistoryMessagesInput(e.target.value)}
                                        onBlur={() => handleMaxHistoryMessagesCommit()}
                                    />
                                    <span className={styles.hint}>
                                        LLMに送信する会話履歴の最大件数。コンテキスト超過エラーが発生する場合は値を下げてください。
                                    </span>
                                </div>
                                <div className={styles.formGroup}>
                                    <label className={styles.checkboxLabel}>
                                        <input
                                            type="checkbox"
                                            checked={metabolismEnabled}
                                            onChange={(e) => handleMetabolismEnabledChange(e.target.checked)}
                                        />
                                        履歴の新陳代謝
                                    </label>
                                    <span className={styles.hint}>
                                        ON: 会話履歴のウィンドウ始点を固定しキャッシュヒット率を向上。上限到達時にバルクトリミング+Chronicle生成。OFF: 従来のスライディングウィンドウ。
                                    </span>
                                </div>
                                {metabolismEnabled && (
                                    <div className={styles.formGroup}>
                                        <label>
                                            代謝後の保持件数
                                            {metabolismKeepMessagesDefault != null && (
                                                <span className={styles.hint}> （モデルデフォルト: {metabolismKeepMessagesDefault}）</span>
                                            )}
                                        </label>
                                        <input
                                            type="number"
                                            className={styles.input}
                                            min={1}
                                            max={getMaxKeepMessages() ?? 500}
                                            value={metabolismKeepMessages ?? ''}
                                            placeholder={metabolismKeepMessagesDefault ? `（自動: ${metabolismKeepMessagesDefault}）` : '（自動）'}
                                            onChange={(e) => handleMetabolismKeepMessagesInput(e.target.value)}
                                            onBlur={() => handleMetabolismKeepMessagesCommit()}
                                        />
                                        <span className={styles.hint}>
                                            上限到達時にこの件数まで古い履歴を整理します。
                                            {getMaxKeepMessages() != null
                                                ? `設定可能範囲: 1〜${getMaxKeepMessages()}（上限${maxHistoryMessages ?? maxHistoryMessagesDefault} - 20）。超過時は自動調整されます。`
                                                : '上限との差は20以上必要です。'
                                            }
                                        </span>
                                    </div>
                                )}
                            </div>

                            {cacheConfig.supported && (
                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>プロンプトキャッシュ (Anthropic)</div>
                                    <div className={styles.formGroup}>
                                        <label className={styles.checkboxLabel}>
                                            <input
                                                type="checkbox"
                                                checked={cacheConfig.enabled}
                                                onChange={(e) => handleCacheEnabledChange(e.target.checked)}
                                            />
                                            プロンプトキャッシュを有効化
                                        </label>
                                        <span className={styles.hint}>
                                            ON: プロンプトをキャッシュしてコスト削減（読取 0.1倍、書込 1.25倍〜2倍）。OFF: キャッシュなし（Anthropic APIは読取専用モード非対応）。
                                        </span>
                                    </div>
                                    {cacheConfig.enabled && cacheConfig.ttl_options.length > 0 && (
                                        <div className={styles.formGroup}>
                                            <label>キャッシュ TTL</label>
                                            <select
                                                className={styles.select}
                                                value={cacheConfig.ttl}
                                                onChange={(e) => handleCacheTtlChange(e.target.value)}
                                            >
                                                {cacheConfig.ttl_options.map(ttl => (
                                                    <option key={ttl} value={ttl}>
                                                        {ttl === '5m' ? '5分（書込コスト 1.25倍）' : '1時間（書込コスト 2倍）'}
                                                    </option>
                                                ))}
                                            </select>
                                        </div>
                                    )}
                                </div>
                            )}

                            {playbookParamSpecs.length > 0 && (
                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>Playbook パラメータ</div>
                                    {playbookParamSpecs.map(param => (
                                        <div key={param.name} className={styles.formGroup}>
                                            <label>
                                                {param.description || param.name}
                                                {!param.required && <span className={styles.optional}> （任意）</span>}
                                            </label>

                                            {param.resolved_options && param.resolved_options.length > 0 ? (
                                                <select
                                                    className={styles.select}
                                                    value={playbookParams[param.name] ?? param.default ?? ''}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, e.target.value || null)}
                                                >
                                                    <option value="">{param.required ? '（選択...）' : '（自動）'}</option>
                                                    {param.resolved_options.map(opt => (
                                                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                                                    ))}
                                                </select>
                                            ) : param.param_type === 'boolean' ? (
                                                <input
                                                    type="checkbox"
                                                    checked={playbookParams[param.name] ?? param.default ?? false}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, e.target.checked)}
                                                />
                                            ) : param.param_type === 'number' ? (
                                                <input
                                                    type="number"
                                                    className={styles.input}
                                                    value={playbookParams[param.name] ?? param.default ?? ''}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, parseFloat(e.target.value))}
                                                />
                                            ) : (
                                                <input
                                                    type="text"
                                                    className={styles.input}
                                                    value={playbookParams[param.name] ?? param.default ?? ''}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, e.target.value)}
                                                    placeholder={param.description}
                                                />
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}

                            {Object.keys(paramSpecs).length > 0 && (
                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>パラメータ</div>
                                    {Object.entries(paramSpecs).map(([key, spec]) => (
                                        <div key={key} className={styles.formGroup}>
                                            <label>
                                                {spec.label}
                                                <span className={styles.value}>{params[key]}</span>
                                            </label>

                                            {spec.type === 'slider' && (
                                                <input
                                                    type="range"
                                                    className={styles.slider}
                                                    min={spec.min} max={spec.max} step={spec.step}
                                                    value={params[key] ?? spec.default}
                                                    onChange={(e) => handleParamChange(key, parseFloat(e.target.value))}
                                                />
                                            )}

                                            {spec.type === 'number' && (
                                                <input
                                                    type="number"
                                                    className={styles.input}
                                                    min={spec.min} max={spec.max} step={spec.step}
                                                    value={params[key] ?? spec.default}
                                                    onChange={(e) => handleParamChange(key, parseFloat(e.target.value))}
                                                />
                                            )}

                                            {spec.type === 'dropdown' && (
                                                <select
                                                    className={styles.select}
                                                    value={params[key] ?? spec.default}
                                                    onChange={(e) => handleParamChange(key, e.target.value)}
                                                >
                                                    {spec.options?.map(opt => (
                                                        <option key={opt} value={opt}>{opt}</option>
                                                    ))}
                                                </select>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>閉じる</button>
                    <button className={styles.saveBtn} onClick={saveParams}>設定を適用</button>
                </div>
            </div>
        </div>
    );
}
