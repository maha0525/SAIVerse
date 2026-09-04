import React, { useEffect, useState, useMemo, useRef } from 'react';
import styles from './ChatOptions.module.css';
import { X, ChevronDown, Star } from 'lucide-react';
import { formatCost } from '@/lib/formatCost';
import ModelEditorModal from './settings/ModelEditorModal';
import ContextVolumeBar, { ContextStatus, canDrawContextVolumeBar } from './common/ContextVolumeBar';

interface RateLimitInfo {
    rpd: number;
    reset_timezone: string;
}

interface ModelInfo {
    id: string;
    name: string;
    provider?: string | null;
    group?: string | null;  // UI grouping label (falls back to provider)
    input_price?: number | null;
    output_price?: number | null;
    currency?: string;
    rate_limit?: RateLimitInfo | null;
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

// Phase 1: read-only キャッシュタイマーの status (GET /api/people/{id}/cache-status)
interface CacheStatus {
    persona_id: string;
    model: string | null;
    supported: boolean;       // Anthropic explicit のみ true
    cache_type: string | null;
    active: boolean;          // cache が現在生きているか
    anchor_updated_at: number | null;  // epoch seconds
    ttl_seconds: number | null;
    expires_at: number | null;         // epoch seconds
    remaining_seconds: number;
    cache_setting: string;             // Phase 2: 実効 cache 設定 ("off" | "5m" | "1h")
}

// ContextStatus (GET /api/people/{id}/context-status) の型と横棒の描画は
// common/ContextVolumeBar に置いてある — Chronicle 生成の確認窓と共有する。

interface ChatOptionsProps {
    isOpen: boolean;
    onClose: () => void;
    currentModel: string;
    onModelChange: (model: string, displayName: string, rateLimit?: RateLimitInfo | null) => void;
    buildingId?: string | null;  // キャッシュタイマーの persona switcher 用 (occupants 取得)
}

export default function ChatOptions({ isOpen, onClose, currentModel: propCurrentModel, onModelChange, buildingId }: ChatOptionsProps) {
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [currentModel, setCurrentModel] = useState<string>('');
    const [params, setParams] = useState<Record<string, any>>({});
    const [paramSpecs, setParamSpecs] = useState<Record<string, ParamSpec>>({});
    const [loading, setLoading] = useState(false);
    const [cacheConfig, setCacheConfig] = useState<CacheConfig>({
        enabled: true,
        ttl: '5m',
        supported: false,
        ttl_options: [],
        cache_type: null
    });
    // データ送信量セクションの読み取り専用表示 (設定はモデル定義側 — 2026-07-30
    // グローバル上書き廃止、docs/issues/chat_options_metabolism_section_redesign.md)
    const [contextStatus, setContextStatus] = useState<ContextStatus | null>(null);
    const [contextStatusError, setContextStatusError] = useState(false);
    // モデル変更後の再取得トリガー (水位はモデル依存なので旧モデルの表示が残る)
    const [contextStatusReload, setContextStatusReload] = useState(0);
    const [maxImageEmbeds, setMaxImageEmbeds] = useState<number | null>(null);
    const [maxImageEmbedsDefault, setMaxImageEmbedsDefault] = useState<number | null>(null);
    const [historySettingsOpen, setHistorySettingsOpen] = useState(false);
    const [modelParamsOpen, setModelParamsOpen] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [favoriteModels, setFavoriteModels] = useState<string[]>([]);
    const [editorOpen, setEditorOpen] = useState(false);
    const [savingAs, setSavingAs] = useState(false);
    // Cache timer (Phase 1): persona switcher + read-only status polling
    const [cachePersonas, setCachePersonas] = useState<{ id: string; name: string }[]>([]);
    const [selectedCachePersonaId, setSelectedCachePersonaId] = useState<string>('');
    const [cacheStatus, setCacheStatus] = useState<CacheStatus | null>(null);
    const [nowMs, setNowMs] = useState<number>(() => Date.now());

    useEffect(() => {
        if (isOpen) {
            fetchData();
        }
    }, [isOpen]);

    // Cache timer: load building occupants (persona switcher source) on open
    useEffect(() => {
        if (!isOpen || !buildingId) {
            setCachePersonas([]);
            return;
        }
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(`/api/info/details?building_id=${encodeURIComponent(buildingId)}`);
                if (!res.ok) return;
                const data = await res.json();
                const occ = (data.occupants || []).map((o: { id: string; name: string }) => ({ id: o.id, name: o.name }));
                if (cancelled) return;
                setCachePersonas(occ);
                setSelectedCachePersonaId(prev =>
                    prev && occ.some((o: { id: string }) => o.id === prev) ? prev : (occ[0]?.id || '')
                );
            } catch (e) {
                console.error('Failed to fetch building occupants for cache timer', e);
            }
        })();
        return () => { cancelled = true; };
    }, [isOpen, buildingId]);

    // Cache timer: poll cache-status for the selected persona every 2s
    useEffect(() => {
        if (!isOpen || !selectedCachePersonaId) {
            setCacheStatus(null);
            return;
        }
        let cancelled = false;
        const poll = async () => {
            try {
                const res = await fetch(`/api/people/${encodeURIComponent(selectedCachePersonaId)}/cache-status`);
                if (!res.ok) return;
                const data = await res.json();
                if (!cancelled) {
                    // cacheStatus と nowMs を同時に同期。これをしないと、開いた瞬間は
                    // マウント時の古い nowMs で残り時間が計算され、最初の 1s ティックまで
                    // 変な値が表示される。poll は開いた直後に即実行されるので初回から正しくなる。
                    setCacheStatus(data);
                    setNowMs(Date.now());
                }
            } catch {
                // network error: keep last known status, retry next tick
            }
        };
        poll();
        const id = setInterval(poll, 2000);
        return () => { cancelled = true; clearInterval(id); };
    }, [isOpen, selectedCachePersonaId]);

    // Cache timer: 1s local ticker for smooth countdown between 2s polls
    useEffect(() => {
        if (!isOpen || !cacheStatus?.active || !cacheStatus.expires_at) return;
        const id = setInterval(() => setNowMs(Date.now()), 1000);
        return () => clearInterval(id);
    }, [isOpen, cacheStatus?.active, cacheStatus?.expires_at]);

    // データ送信量: 選択ペルソナの提示コンテキスト状態。開いた時・ペルソナ切替時・
    // モデル変更後 (contextStatusReload) に取得する — 計測は読み戻しの読み取り専用
    // 計画を再利用していて DB 読みを伴うため、cache-status のような 2 秒ポーリングは
    // しない。切替時は前の値を即座に消す (取得失敗時に別ペルソナ/旧モデルの値を
    // 出し続けない — Codex 指摘 2026-07-30)。
    useEffect(() => {
        setContextStatus(null);
        setContextStatusError(false);
        if (!isOpen || !selectedCachePersonaId) return;
        let cancelled = false;
        (async () => {
            try {
                const res = await fetch(`/api/people/${encodeURIComponent(selectedCachePersonaId)}/context-status`);
                if (cancelled) return;
                if (!res.ok) {
                    setContextStatusError(true);
                    return;
                }
                const data = await res.json();
                if (!cancelled) setContextStatus(data);
            } catch (e) {
                console.error('Failed to fetch context status', e);
                if (!cancelled) setContextStatusError(true);
            }
        })();
        return () => { cancelled = true; };
    }, [isOpen, selectedCachePersonaId, contextStatusReload]);

    // stillValid: 応答の**適用直前**に呼ばれる有効性チェック (省略時は常に適用)。
    // 遅延 resync が渡す — 開始済みの fetch は clearTimeout では止まらないので、
    // 取得中に新しいモデル選択が入った場合は結果を捨てる (Codex 指摘 2026-07-30)。
    // onClick ハンドラから直接呼ばれると第一引数に MouseEvent が入るため、関数で
    // ないものは無視する。
    const fetchData = async (stillValid?: unknown) => {
        const isStillValid: () => boolean =
            typeof stillValid === 'function' ? (stillValid as () => boolean) : () => true;
        setLoading(true);
        setError(null);

        // Abort after 10 seconds to prevent infinite hang
        const controller = new AbortController();
        const timeoutId = setTimeout(() => controller.abort(), 10000);

        try {
            const results = await Promise.allSettled([
                fetch('/api/config/models', { signal: controller.signal }),
                fetch('/api/config/config', { signal: controller.signal }),
                fetch('/api/config/cache', { signal: controller.signal }),
                fetch('/api/config/favorite-models', { signal: controller.signal })
            ]);
            if (!isStillValid()) return; // 取得中に新しい選択が入った — 結果を捨てる

            const failures: string[] = [];
            let fetchedModels: ModelInfo[] = [];

            // Models
            if (results[0].status === 'fulfilled' && results[0].value.ok) {
                try {
                    fetchedModels = await results[0].value.json();
                    if (!isStillValid()) return; // json() の await 中の追い越しも捨てる
                    setModels(fetchedModels);
                } catch (e) { console.error("Failed to parse models response", e); failures.push('models'); }
            } else {
                const reason = results[0].status === 'rejected' ? results[0].reason : `HTTP ${results[0].value.status}`;
                console.error("Failed to fetch models:", reason);
                failures.push('models');
            }

            // Config
            if (results[1].status === 'fulfilled' && results[1].value.ok) {
                try {
                    const config = await results[1].value.json();
                    if (!isStillValid()) return;
                    const modelId = config.current_model || '';
                    setCurrentModel(modelId);
                    const modelInfo = fetchedModels.find(m => m.id === modelId);
                    onModelChange(modelId, modelInfo?.name || '', modelInfo?.rate_limit);
                    setParamSpecs(config.parameters || {});
                    setParams(config.current_values || {});
                    setMaxImageEmbeds(config.max_image_embeds ?? null);
                    setMaxImageEmbedsDefault(config.max_image_embeds_model_default ?? null);
                } catch (e) { console.error("Failed to parse config response", e); failures.push('config'); }
            } else {
                const reason = results[1].status === 'rejected' ? results[1].reason : `HTTP ${results[1].value.status}`;
                console.error("Failed to fetch config:", reason);
                failures.push('config');
            }

            // Cache
            if (results[2].status === 'fulfilled' && results[2].value.ok) {
                try {
                    const cacheData = await results[2].value.json();
                    if (!isStillValid()) return;
                    setCacheConfig(cacheData);
                }
                catch (e) { console.error("Failed to parse cache response", e); failures.push('cache'); }
            } else {
                const reason = results[2].status === 'rejected' ? results[2].reason : `HTTP ${results[2].value.status}`;
                console.error("Failed to fetch cache:", reason);
                failures.push('cache');
            }

            // Favorites
            if (results[3].status === 'fulfilled' && results[3].value.ok) {
                try {
                    const favData = await results[3].value.json();
                    if (!isStillValid()) return;
                    setFavoriteModels(favData.models || []);
                } catch (e) { console.error("Failed to parse favorites response", e); }
            }

            if (failures.length === 3) {
                setError("バックエンドサーバーに接続できません。サーバーが起動しているか確認してください。");
            } else if (failures.length > 0) {
                setError(`一部の設定を読み込めませんでした (${failures.join(', ')})`);
            }
        } catch (e) {
            console.error("Failed to load config", e);
            if (e instanceof DOMException && e.name === 'AbortError') {
                setError("設定の読み込みがタイムアウトしました。バックエンドサーバーの応答を確認してください。");
            } else {
                setError("設定の読み込み中にエラーが発生しました。");
            }
        } finally {
            clearTimeout(timeoutId);
            setLoading(false);
        }
    };

    const GROUP_LABELS: Record<string, string> = {
        anthropic: 'Anthropic',
        openai: 'OpenAI',
        gemini: 'Google Gemini',
        openrouter: 'OpenRouter',
        nvidia_nim: 'NVIDIA NIM',
        xai: 'xAI',
        ollama: 'Ollama',
        llama_cpp: 'llama.cpp',
        plamo: 'PLaMo',
        sakana: 'Sakana AI',
    };

    const groupLabel = (group: string): string => {
        return GROUP_LABELS[group] || group;
    };

    const isFavorite = (modelId: string) => favoriteModels.includes(modelId);

    const toggleFavorite = async (modelId: string) => {
        const newFavorites = isFavorite(modelId)
            ? favoriteModels.filter(id => id !== modelId)
            : [...favoriteModels, modelId];
        setFavoriteModels(newFavorites);
        try {
            await fetch('/api/config/favorite-models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ models: newFavorites })
            });
        } catch (e) {
            console.error("Failed to save favorite models", e);
        }
    };

    // Group models by group field, with favorites at top
    const groupedModels = useMemo(() => {
        const favorites = models.filter(m => favoriteModels.includes(m.id));
        const byGroup: Record<string, ModelInfo[]> = {};
        for (const m of models) {
            const group = m.group || m.provider || 'other';
            if (!byGroup[group]) byGroup[group] = [];
            byGroup[group].push(m);
        }
        // Sort groups: known providers first, then alphabetical
        const groupOrder = ['anthropic', 'openai', 'gemini', 'openrouter', 'nvidia_nim', 'xai', 'ollama', 'llama_cpp'];
        const sortedGroups = Object.keys(byGroup).sort((a, b) => {
            const ai = groupOrder.indexOf(a);
            const bi = groupOrder.indexOf(b);
            if (ai !== -1 && bi !== -1) return ai - bi;
            if (ai !== -1) return -1;
            if (bi !== -1) return 1;
            return a.localeCompare(b);
        });
        return { favorites, byGroup, sortedGroups };
    }, [models, favoriteModels]);

    // モデル変更は直列化する — 素早い A→B 切替で A の応答が後着すると、選択欄は B
    // なのにパラメータ・水位表示が A に戻る (Codex 指摘 2026-07-30)。seq が最新で
    // ない仕事は POST 自体を送らず、最後の選択だけをサーバーに確定させる。
    // client_id はマウントごとの世代の名前空間 — サーバーの世代ガードはこの中で
    // だけ効く (リロードや別タブを 409 にしない)。
    const modelChangeSeqRef = useRef(0);
    const modelChangeChainRef = useRef<Promise<void>>(Promise.resolve());
    const modelChangeClientIdRef = useRef<string>(
        `chat-options-${Math.random().toString(36).slice(2)}-${Date.now()}`,
    );
    const modelResyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const handleModelChange = (modelId: string) => {
        setCurrentModel(modelId);
        // Find display name from models list
        const modelInfo = models.find(m => m.id === modelId);
        onModelChange(modelId, modelInfo?.name || '', modelInfo?.rate_limit); // Notify parent component
        const seq = ++modelChangeSeqRef.current;
        // 前の選択が予約した遅延 resync は取り消す — 古い状態で表示を巻き戻すため
        if (modelResyncTimerRef.current) {
            clearTimeout(modelResyncTimerRef.current);
            modelResyncTimerRef.current = null;
        }
        // .catch: applyModelChange は内部で全例外を握るが、万一 reject が漏れた
        // 場合にチェーンが死んで以後の選択が送信されなくなるのを防ぐ保険。
        modelChangeChainRef.current = modelChangeChainRef.current
            .then(() => applyModelChange(modelId, seq))
            .catch(() => {});
    };

    const applyModelChange = async (modelId: string, seq: number) => {
        if (seq !== modelChangeSeqRef.current) return; // もっと新しい選択が控えている
        // 期限なしの POST が pending のままだとチェーン全体が永久停止し、以後の
        // 選択がサーバーへ届かなくなる (Codex 指摘 2026-07-30) — 10 秒で中断して
        // 失敗扱いにする (fetchData と同じ期限)。
        const controller = new AbortController();
        let timedOut = false;
        const timeoutId = setTimeout(() => { timedOut = true; controller.abort(); }, 10000);
        try {
            const res = await fetch('/api/config/model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                // client_id + seq: サーバー側の世代ガード — abort した古い要求が
                // 遅れて完了しても、同一クライアントの新しい選択を上書きしない
                // (Codex 指摘 2026-07-30)
                body: JSON.stringify({
                    model: modelId, seq, client_id: modelChangeClientIdRef.current,
                }),
                signal: controller.signal,
            });
            if (seq !== modelChangeSeqRef.current) return; // 古い応答は適用しない
            if (res.status === 409) {
                // 別の (より新しい) 選択が適用済み — エラーではなく実状態に合わせる
                await fetchData();
                return;
            }
            if (!res.ok) {
                // 失敗: サーバーの実状態へ表示を合わせ直した上でエラーを出す
                await fetchData();
                setError('モデルの変更に失敗しました');
                return;
            }
            // Use inline parameters from response (no separate fetch needed)
            const data = await res.json();
            // サーバー正で選択を確定する — 途中で走った resync が表示を巻き戻して
            // いても、最新選択の成功応答が上書きして最終状態を一致させる
            if (data.current_model) {
                setCurrentModel(data.current_model);
                const appliedInfo = models.find(m => m.id === data.current_model);
                onModelChange(data.current_model, appliedInfo?.name || '', appliedInfo?.rate_limit);
            }
            setParamSpecs(data.parameters || {});
            setParams(data.current_values || {});
            setMaxImageEmbeds(data.max_image_embeds ?? null);
            setMaxImageEmbedsDefault(data.max_image_embeds_model_default ?? null);
            // 水位はモデル依存 — モデルが変わったら状態表示を取り直す
            setContextStatus(null);
            setContextStatusReload(n => n + 1);

            // Refetch cache config since it depends on selected model
            const cacheRes = await fetch('/api/config/cache', { signal: controller.signal });
            if (seq !== modelChangeSeqRef.current) return;
            if (cacheRes.ok) {
                setCacheConfig(await cacheRes.json());
            }
        } catch (e) {
            console.error("Failed to set model", e);
            if (seq === modelChangeSeqRef.current) {
                await fetchData();
                setError('モデルの変更に失敗しました');
                if (timedOut) {
                    // abort はサーバー側の適用を止めない — 遅れて確定した状態を
                    // 拾い直す。ただし予約後に新しい選択が入ったら実行しない
                    // (古い状態で新選択の表示を巻き戻すため — Codex 指摘 5巡目)。
                    // 完全な保証はサーバーの世代ガードが持つ。
                    modelResyncTimerRef.current = setTimeout(() => {
                        modelResyncTimerRef.current = null;
                        // 開始前 + 応答適用直前の両方で世代を確認 — 発火済みの
                        // resync は clearTimeout では止まらないため、適用側でも
                        // 最新世代でなければ結果を捨てる
                        const stillValid = () => seq === modelChangeSeqRef.current;
                        if (stillValid()) fetchData(stillValid);
                    }, 3000);
                }
            }
        } finally {
            clearTimeout(timeoutId);
        }
    };

    const handleParamChange = (key: string, value: any) => {
        const newParams = { ...params, [key]: value };
        setParams(newParams);
    };

    const handleMaxImageEmbedsInput = (value: string) => {
        const numValue = value === '' ? null : parseInt(value, 10);
        if (numValue !== null && (isNaN(numValue) || numValue < 0)) return;
        setMaxImageEmbeds(numValue);
    };

    const handleMaxImageEmbedsCommit = async () => {
        try {
            await fetch('/api/config/max-image-embeds', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ value: maxImageEmbeds })
            });
        } catch (e) {
            console.error("Failed to update max image embeds", e);
        }
    };

    // Phase 2: per-persona cache 設定 ("off" | "5m" | "1h")
    const handleCacheSettingChange = async (setting: string) => {
        if (!selectedCachePersonaId) return;
        // 楽観更新: セレクタを即反映。残り時間 (ttl_seconds/expires_at) は次の poll で更新される。
        setCacheStatus(prev => (prev ? { ...prev, cache_setting: setting } : prev));
        try {
            await fetch(`/api/people/${encodeURIComponent(selectedCachePersonaId)}/cache-config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ setting }),
            });
        } catch (e) {
            console.error('Failed to set per-persona cache setting', e);
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

    const buildSaveFromChatPayload = (targetKey: string, displayName: string, overwrite: boolean) => ({
        source_model: currentModel,
        target_key: targetKey,
        display_name: displayName,
        parameters: params,
        cache_enabled: cacheConfig.supported ? cacheConfig.enabled : null,
        cache_ttl: cacheConfig.supported ? cacheConfig.ttl : null,
        max_image_embeds: maxImageEmbeds,
        overwrite,
    });

    const handleSaveAs = async () => {
        if (!currentModel) return;
        const newKey = window.prompt('新しいモデルキー (ファイル名) を入力:', `${currentModel}-tweaked`);
        if (!newKey) return;
        const currentDisplay = models.find(m => m.id === currentModel)?.name || currentModel;
        const newDisplay = window.prompt('表示名を入力:', `${currentDisplay} (custom)`);
        if (!newDisplay) return;

        setSavingAs(true);
        try {
            const res = await fetch('/api/config/models/save-from-chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(buildSaveFromChatPayload(newKey, newDisplay, false)),
            });
            if (!res.ok) {
                alert(`保存に失敗しました: ${await res.text()}`);
                return;
            }
            // Refresh model list so the new model appears in the dropdown
            const modelsRes = await fetch('/api/config/models');
            if (modelsRes.ok) setModels(await modelsRes.json());
            alert('新しいモデルとして保存しました');
        } catch (e) {
            alert(`保存に失敗しました: ${e}`);
        } finally {
            setSavingAs(false);
        }
    };

    const handleOverwrite = async () => {
        if (!currentModel) return;
        const modelInfo = models.find(m => m.id === currentModel);
        const displayName = modelInfo?.name || currentModel;
        if (!window.confirm(`現在の設定を「${displayName}」に上書き保存しますか？\n\n※ builtin/expansion モデルの場合は user_data に上書きコピーが作成されます (元のファイルは変更されません)。`)) return;

        setSavingAs(true);
        try {
            const res = await fetch('/api/config/models/save-from-chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(buildSaveFromChatPayload(currentModel, displayName, true)),
            });
            if (!res.ok) {
                alert(`上書き保存に失敗しました: ${await res.text()}`);
                return;
            }
            alert('上書き保存しました');
        } catch (e) {
            alert(`上書き保存に失敗しました: ${e}`);
        } finally {
            setSavingAs(false);
        }
    };

    const renderCacheTimerBody = () => {
        if (!cacheStatus) {
            return <span className={styles.hint}>読み込み中...</span>;
        }
        if (!cacheStatus.supported) {
            return <span className={styles.hint}>このモデルは明示的キャッシュに非対応です（タイマー対象外）。</span>;
        }
        if (cacheStatus.cache_setting === 'off') {
            return <span className={styles.hint}>このペルソナはキャッシュ無効です。</span>;
        }
        if (!cacheStatus.active || !cacheStatus.expires_at || !cacheStatus.ttl_seconds) {
            return <span className={styles.hint}>キャッシュは現在効いていません（次の発話で作成されます）。</span>;
        }
        const remainingSec = Math.max(0, Math.round((cacheStatus.expires_at * 1000 - nowMs) / 1000));
        const pct = Math.max(0, Math.min(100, (remainingSec / cacheStatus.ttl_seconds) * 100));
        let color = '#34d399';
        if (pct < 15) color = '#f87171';
        else if (pct < 40) color = '#fbbf24';
        const mm = Math.floor(remainingSec / 60);
        const ss = remainingSec % 60;
        const timeText = `${mm}:${String(ss).padStart(2, '0')}`;
        const isLive = remainingSec > 0;
        return (
            <>
                <div className={styles.timerRow}>
                    <span className={`${styles.timerStatusDot} ${isLive ? styles.timerActive : styles.timerInactive}`} />
                    <div className={styles.timerBar}>
                        <div className={styles.timerBarFill} style={{ width: `${pct}%`, background: color }} />
                    </div>
                    <span className={styles.timerText}>残り {timeText}</span>
                </div>
                <span className={styles.hint}>
                    キャッシュ有効中。残り時間内に発話すると cache hit（格安）になります。
                </span>
            </>
        );
    };

    // データ送信量: 読み取り専用の状態表示。会話履歴は始点を固定したまま送られ、
    // 上限を超えると古い順にあらすじへ畳まれる。設定はモデル定義側 (モデル編集画面)。
    const renderContextStatusBody = () => {
        if (!selectedCachePersonaId) {
            return <span className={styles.hint}>ペルソナのいる建物で開くと、会話コンテキストの状態が表示されます。</span>;
        }
        if (contextStatusError) {
            return <span className={styles.hint}>状態を取得できませんでした。開き直すか、ペルソナを切り替えると再試行します。</span>;
        }
        if (!contextStatus) {
            return <span className={styles.hint}>読み込み中...</span>;
        }
        if (!contextStatus.metabolism) {
            return (
                <span className={styles.hint}>
                    このモデル（{contextStatus.model || '未設定'}）は水位を持たない設定のため、履歴の自動整理は行われません。
                </span>
            );
        }
        return (
            <>
                {canDrawContextVolumeBar(contextStatus) ? (
                    <ContextVolumeBar status={contextStatus} />
                ) : contextStatus.measurement_failed ? (
                    <span className={styles.hint}>現在量を測定できませんでした（水位のみ表示しています）。</span>
                ) : (
                    <span className={styles.hint}>まだ会話の起点がありません。最初の会話で確立されます。</span>
                )}
                <span className={styles.hint}>
                    会話履歴は始点を固定したまま送られ、上限を超えると古い出来事から順にあらすじへ畳んで「残す量」まで整理します。
                    {contextStatus.fold_unit_chars != null && contextStatus.fold_unit_chars > 0 && (
                        `整理は古い側から約 ${contextStatus.fold_unit_chars.toLocaleString()} 文字ぶんずつまとめて畳みます（スペルの結果などの長い記録は、圧縮した後の字数で数えます）。`
                    )}
                    畳みすぎて残す量を下回ったときは、次の会話の前に畳んだ範囲を自動で開き直します。
                    水位を変えたいときは、設定のモデル編集から（モデルごとの設定です）。
                </span>
            </>
        );
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
                            {error && (
                                <div className={styles.errorBanner}>
                                    <span>{error}</span>
                                    <button className={styles.retryBtn} onClick={fetchData}>再試行</button>
                                </div>
                            )}
                            <div className={styles.section}>
                                <div className={styles.formGroup}>
                                    <label>モデル</label>
                                    <div className={styles.modelSelectRow}>
                                        <select
                                            className={styles.select}
                                            value={currentModel}
                                            onChange={(e) => handleModelChange(e.target.value)}
                                        >
                                            <option value="">（デフォルト）</option>
                                            {groupedModels.favorites.length > 0 && (
                                                <optgroup label="★ お気に入り">
                                                    {groupedModels.favorites.map(m => (
                                                        <option key={`fav-${m.id}`} value={m.id}>{m.name}</option>
                                                    ))}
                                                </optgroup>
                                            )}
                                            {groupedModels.sortedGroups.map(group => (
                                                <optgroup key={group} label={groupLabel(group)}>
                                                    {groupedModels.byGroup[group].map(m => (
                                                        <option key={m.id} value={m.id}>{m.name}</option>
                                                    ))}
                                                </optgroup>
                                            ))}
                                        </select>
                                        {currentModel && (
                                            <button
                                                className={`${styles.favoriteBtn} ${isFavorite(currentModel) ? styles.favoriteBtnActive : ''}`}
                                                onClick={() => toggleFavorite(currentModel)}
                                                title={isFavorite(currentModel) ? 'お気に入りから削除' : 'お気に入りに追加'}
                                            >
                                                <Star size={18} fill={isFavorite(currentModel) ? 'currentColor' : 'none'} />
                                            </button>
                                        )}
                                    </div>
                                    {(() => {
                                        const sel = models.find(m => m.id === currentModel);
                                        if (!sel || (sel.input_price == null && sel.output_price == null)) return null;
                                        const cur = sel.currency ?? 'USD';
                                        return (
                                            <span className={styles.hint}>
                                                {sel.input_price != null && `入力: ${formatCost(sel.input_price, cur)}/1M tokens`}
                                                {sel.input_price != null && sel.output_price != null && ' ・ '}
                                                {sel.output_price != null && `出力: ${formatCost(sel.output_price, cur)}/1M tokens`}
                                            </span>
                                        );
                                    })()}
                                </div>
                            </div>

                            {cachePersonas.length > 0 && (
                                <div className={styles.section}>
                                    <div className={styles.formGroup}>
                                        <label>キャッシュ（このペルソナ）</label>
                                        {cachePersonas.length > 1 && (
                                            <div className={styles.cacheTimerTabs}>
                                                {cachePersonas.map(p => (
                                                    <button
                                                        key={p.id}
                                                        type="button"
                                                        className={`${styles.personaTab} ${p.id === selectedCachePersonaId ? styles.personaTabActive : ''}`}
                                                        onClick={() => setSelectedCachePersonaId(p.id)}
                                                    >
                                                        {p.name}
                                                    </button>
                                                ))}
                                            </div>
                                        )}
                                        {cacheStatus?.supported && (
                                            <div className={styles.cacheTtlOverrideRow}>
                                                <span className={styles.cacheTtlOverrideLabel}>キャッシュ</span>
                                                <select
                                                    className={styles.select}
                                                    value={cacheStatus.cache_setting}
                                                    onChange={(e) => handleCacheSettingChange(e.target.value)}
                                                >
                                                    <option value="off">オフ</option>
                                                    <option value="5m">5分</option>
                                                    <option value="1h">1時間（連続対話向け）</option>
                                                </select>
                                            </div>
                                        )}
                                        {renderCacheTimerBody()}
                                    </div>
                                </div>
                            )}

                            <div className={styles.section}>
                                <div
                                    className={styles.collapsibleTitle}
                                    onClick={() => setHistorySettingsOpen(!historySettingsOpen)}
                                >
                                    <span>データ送信量の管理</span>
                                    <ChevronDown
                                        size={16}
                                        className={`${styles.chevron} ${historySettingsOpen ? styles.chevronOpen : ''}`}
                                    />
                                </div>
                                {historySettingsOpen && (
                                    <>
                                        <div className={styles.formGroup}>
                                            <label>会話コンテキストの現在量</label>
                                            {cachePersonas.length > 1 && (
                                                <div className={styles.cacheTimerTabs}>
                                                    {cachePersonas.map(p => (
                                                        <button
                                                            key={p.id}
                                                            type="button"
                                                            className={`${styles.personaTab} ${p.id === selectedCachePersonaId ? styles.personaTabActive : ''}`}
                                                            onClick={() => setSelectedCachePersonaId(p.id)}
                                                        >
                                                            {p.name}
                                                        </button>
                                                    ))}
                                                </div>
                                            )}
                                            {renderContextStatusBody()}
                                        </div>
                                        <div className={styles.formGroup}>
                                            <label>
                                                画像埋め込み上限
                                                {maxImageEmbedsDefault != null && (
                                                    <span className={styles.hint}> （モデルデフォルト: {maxImageEmbedsDefault}）</span>
                                                )}
                                            </label>
                                            <input
                                                type="number"
                                                className={styles.input}
                                                min={0}
                                                max={50}
                                                value={maxImageEmbeds ?? ''}
                                                placeholder={maxImageEmbedsDefault ? `（自動: ${maxImageEmbedsDefault}）` : '（デフォルト: 4）'}
                                                onChange={(e) => handleMaxImageEmbedsInput(e.target.value)}
                                                onBlur={() => handleMaxImageEmbedsCommit()}
                                            />
                                            <span className={styles.hint}>
                                                LLMに送信する画像の最大枚数。超過分はテキスト要約に置換されます。0で全画像をテキスト化。空欄でデフォルト値（4枚）を使用。
                                            </span>
                                        </div>
                                    </>
                                )}
                            </div>

                            {Object.keys(paramSpecs).length > 0 && (
                                <div className={styles.section}>
                                    <div
                                        className={styles.collapsibleTitle}
                                        onClick={() => setModelParamsOpen(!modelParamsOpen)}
                                    >
                                        <span>モデルパラメータ</span>
                                        <ChevronDown
                                            size={16}
                                            className={`${styles.chevron} ${modelParamsOpen ? styles.chevronOpen : ''}`}
                                        />
                                    </div>
                                    {modelParamsOpen && Object.entries(paramSpecs).map(([key, spec]) => (
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
                    <div className={styles.footerLeft}>
                        <button
                            className={styles.linkBtn}
                            onClick={() => setEditorOpen(true)}
                            disabled={!currentModel}
                            title="モデルファイル全体を JSON で編集"
                        >
                            詳細編集...
                        </button>
                        <button
                            className={styles.linkBtn}
                            onClick={handleSaveAs}
                            disabled={!currentModel || savingAs}
                            title="現在の設定を新しいモデルとして保存"
                        >
                            別名で保存...
                        </button>
                        <button
                            className={styles.linkBtn}
                            onClick={handleOverwrite}
                            disabled={!currentModel || savingAs}
                            title="現在の設定をこのモデルに上書き保存"
                        >
                            上書き保存
                        </button>
                    </div>
                    <div className={styles.footerRight}>
                        <button className={styles.cancelBtn} onClick={onClose}>閉じる</button>
                        <button className={styles.saveBtn} onClick={saveParams}>設定を適用</button>
                    </div>
                </div>
            </div>

            <ModelEditorModal
                isOpen={editorOpen}
                mode="edit"
                modelKey={currentModel || undefined}
                onClose={() => setEditorOpen(false)}
                onSaved={() => {
                    // Reload everything since the model file changed
                    fetchData();
                    // 水位もモデル定義由来 — 編集直後の旧値を出し続けない
                    setContextStatus(null);
                    setContextStatusReload(n => n + 1);
                }}
            />
        </div>
    );
}
