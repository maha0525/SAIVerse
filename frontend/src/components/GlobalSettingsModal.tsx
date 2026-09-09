import React, { useState, useEffect, useCallback, useRef } from 'react';
import { X, Settings, Globe, Layers, Save, RefreshCw, Power, Monitor, Sun, Moon, Cpu, ChevronDown, ChevronRight, Info, ExternalLink, Wrench, CheckCircle, XCircle, Loader, Boxes, Rss } from 'lucide-react';
import styles from './GlobalSettingsModal.module.css';
import WorldEditor from './settings/WorldEditor';
import ProviderManagementPanel from './settings/ProviderManagementPanel';
import ModelManagementPanel from './settings/ModelManagementPanel';
import FeedManagementPanel from './settings/FeedManagementPanel';
import ModalOverlay from './common/ModalOverlay';
import WatermarkBar, { WATERMARK_LABELS, WatermarkBarValues, findWatermarkOrderViolations } from './common/WatermarkBar';

interface GlobalSettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
}

interface EnvVar {
    key: string;
    value: string;
    is_sensitive: boolean;
}

interface ModelRoleInfo {
    env_key: string;
    value: string;
    display_name: string;
    label: string;
    description: string;
}

interface PresetInfo {
    provider: string;
    display_name: string;
    is_available: boolean;
}

interface ModelInfo {
    id: string;
    display_name: string;
    provider: string;
    is_available: boolean;
    supports_structured_output?: boolean;
}

/** 送る量のプリセット一つ (GET /api/config/metabolism-defaults の presets)。 */
interface WatermarkPreset {
    id: string;
    label: string;
    target: number;
    high: number;
}

interface PlaybookPermEntry {
    playbook_name: string;
    display_name: string;
    description: string;
    permission_level: string;
}

type TabId = 'env' | 'world' | 'feeds' | 'models' | 'modelMgmt' | 'playbooks' | 'about' | 'utilities';
type ModelMgmtSubTab = 'providers' | 'models';

export default function GlobalSettingsModal({ isOpen, onClose }: GlobalSettingsModalProps) {
    const [activeTab, setActiveTab] = useState<TabId>('env');
    const [envVars, setEnvVars] = useState<EnvVar[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [editedEnv, setEditedEnv] = useState<Record<string, string>>({});

    // DB State

    // Global Auto Mode

    // Developer Mode
    const [developerMode, setDeveloperMode] = useState(false);

    // Monitoring toggles
    const [updateCheckEnabled, setUpdateCheckEnabled] = useState(true);
    const [announcementsEnabled, setAnnouncementsEnabled] = useState(true);

    // Image default quality
    const [imageDefaultQuality, setImageDefaultQuality] = useState<'low' | 'medium' | 'high'>('high');

    // Media recall (attached image/audio/video summaries feed auto-recall search)
    const [mediaRecallEnabled, setMediaRecallEnabled] = useState(false);

    // Gemini auto cache (every Gemini call creates an explicit cache to get cached input pricing)
    const [geminiAutoCacheEnabled, setGeminiAutoCacheEnabled] = useState(false);
    const [geminiAutoCacheKeepSeconds, setGeminiAutoCacheKeepSeconds] = useState(0);
    const [geminiAutoCacheKeepInput, setGeminiAutoCacheKeepInput] = useState('0');
    const [geminiAutoCacheKeepMax, setGeminiAutoCacheKeepMax] = useState(3600);

    // ペルソナに送る量 — 全体既定 (GET/PUT /api/config/metabolism-defaults)。
    // 三層 (組み込み既定 < 全体設定 < モデル定義) の真ん中。2026-09-09 に
    // 「部屋の様子などの記録」側の二つの数字を廃止したので (docs/intent/
    // presented_context_reduction.md 設計 3)、画面が扱うのは会話の整理の一組
    // (残す量 / 整理をはじめる量) だけ。欄の文字列は編集中の値で、
    // '' = 未設定 (組み込み既定に従う)。
    type WatermarkKey = keyof WatermarkBarValues;
    const WATERMARK_KEYS: WatermarkKey[] = ['target', 'high'];
    const WATERMARK_API_KEYS: Record<WatermarkKey, string> = {
        target: 'metabolism_target_chars',
        high: 'metabolism_high_chars',
    };
    const [wmGlobal, setWmGlobal] = useState<WatermarkBarValues>({ target: null, high: null });
    // 組み込み既定とプリセットの数字はサーバーが持つ (saiverse/model_configs.py の
    // BUILTIN_METABOLISM_DEFAULTS / METABOLISM_PRESETS)。画面に書き写すと、既定を
    // 動かしたときに画面だけ古い数字を出すので、読み込むまでは持たない。
    const [wmBuiltin, setWmBuiltin] = useState<WatermarkBarValues>({ target: null, high: null });
    const [wmPresets, setWmPresets] = useState<WatermarkPreset[]>([]);
    const [wmInputs, setWmInputs] = useState<Record<WatermarkKey, string>>({ target: '', high: '' });
    // 「カスタム」を押したときに数字を打ち始められるよう、残す量の欄へ移る。
    const wmTargetInputRef = useRef<HTMLInputElement>(null);
    const [wmSaving, setWmSaving] = useState(false);
    const [wmError, setWmError] = useState<string | null>(null);
    const [wmSavedAt, setWmSavedAt] = useState<number | null>(null);

    // Collapsible sections
    const [envSectionOpen, setEnvSectionOpen] = useState(false);

    // Theme
    const [theme, setTheme] = useState<'system' | 'light' | 'dark'>('system');

    // About
    const [versionInfo, setVersionInfo] = useState<{ version: string; latest_version?: string; update_available?: boolean } | null>(null);

    // Model Roles
    const [modelRoles, setModelRoles] = useState<Record<string, ModelRoleInfo>>({});
    const [modelPresets, setModelPresets] = useState<PresetInfo[]>([]);
    const [modelsAvailable, setModelsAvailable] = useState<ModelInfo[]>([]);
    const [expandedModelRole, setExpandedModelRole] = useState<string | null>(null);
    const [modelRolesLoading, setModelRolesLoading] = useState(false);

    // Playbook Permissions
    const [playbookPerms, setPlaybookPerms] = useState<PlaybookPermEntry[]>([]);
    const [playbookPermsLoading, setPlaybookPermsLoading] = useState(false);

    // Model management subtab (providers / models)
    const [modelMgmtSubTab, setModelMgmtSubTab] = useState<ModelMgmtSubTab>('providers');

    // Utilities — backfill item descriptions
    interface BackfillResult { item_id: string; item_name: string; status: string; reason?: string | null; description?: string | null; }
    const [bfBuildings, setBfBuildings] = useState<{id: string; name: string}[]>([]);
    const [bfPersonas, setBfPersonas] = useState<{persona_id: string; persona_name: string}[]>([]);
    const [bfBuildingId, setBfBuildingId] = useState('');
    const [bfPersonaId, setBfPersonaId] = useState('');
    const [bfDryRun, setBfDryRun] = useState(true);
    const [bfRunning, setBfRunning] = useState(false);
    const [bfResults, setBfResults] = useState<{processed: number; skipped: number; failed: number; results: BackfillResult[]} | null>(null);

    const loadBackfillOptions = useCallback(async () => {
        if (bfBuildings.length > 0) return;
        const [bRes, pRes] = await Promise.all([
            fetch('/api/user/buildings'),
            fetch('/api/usage/personas'),
        ]);
        if (bRes.ok) { const d = await bRes.json(); setBfBuildings(d.buildings || []); }
        if (pRes.ok) { const d = await pRes.json(); setBfPersonas(d); }
    }, [bfBuildings.length]);

    const runBackfill = async () => {
        setBfRunning(true);
        setBfResults(null);
        try {
            const res = await fetch('/api/admin/backfill-item-descriptions', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    building_id: bfBuildingId || null,
                    persona_id: bfPersonaId || null,
                    dry_run: bfDryRun,
                }),
            });
            if (!res.ok) throw new Error(await res.text());
            setBfResults(await res.json());
        } catch (e) {
            console.error(e);
        } finally {
            setBfRunning(false);
        }
    };

    useEffect(() => {
        if (isOpen && activeTab === 'env') {
            loadDeveloperModeState();
            loadUpdateCheckState();
            loadAnnouncementsState();
            loadImageDefaultQuality();
            loadMediaRecallState();
            loadGeminiAutoCacheState();
            loadMetabolismDefaults();
            // Load theme from localStorage
            const saved = localStorage.getItem('saiverse-theme') as 'system' | 'light' | 'dark' | null;
            setTheme(saved || 'system');
        }
        if (isOpen && activeTab === 'models') {
            loadModelRoles();
        }
        if (isOpen && activeTab === 'about') {
            loadVersionInfo();
        }
        if (isOpen && activeTab === 'playbooks') {
            loadPlaybookPerms();
        }
        if (isOpen && activeTab === 'utilities') {
            loadBackfillOptions();
        }
    }, [isOpen, activeTab, loadBackfillOptions]);

    // Load env vars when section is expanded
    useEffect(() => {
        if (isOpen && activeTab === 'env' && envSectionOpen && envVars.length === 0) {
            loadEnvVars();
        }
    }, [isOpen, activeTab, envSectionOpen]);

    const loadPlaybookPerms = async () => {
        setPlaybookPermsLoading(true);
        try {
            const res = await fetch('/api/config/playbook-permissions');
            if (res.ok) {
                const data = await res.json();
                setPlaybookPerms(data);
            }
        } catch (e) {
            console.error('Failed to load playbook permissions', e);
        } finally {
            setPlaybookPermsLoading(false);
        }
    };

    const updatePlaybookPerm = async (playbookName: string, level: string) => {
        // Optimistic update
        setPlaybookPerms(prev =>
            prev.map(p => p.playbook_name === playbookName ? { ...p, permission_level: level } : p)
        );
        try {
            const res = await fetch('/api/config/playbook-permissions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playbook_name: playbookName, permission_level: level }),
            });
            if (!res.ok) {
                // Revert on failure
                loadPlaybookPerms();
            }
        } catch (e) {
            console.error('Failed to update playbook permission', e);
            loadPlaybookPerms();
        }
    };

    const changeTheme = (newTheme: 'system' | 'light' | 'dark') => {
        setTheme(newTheme);
        localStorage.setItem('saiverse-theme', newTheme);
        window.dispatchEvent(new Event('theme-change'));
    };

    const loadImageDefaultQuality = async () => {
        try {
            const res = await fetch('/api/config/image-default-quality');
            if (res.ok) {
                const data = await res.json();
                setImageDefaultQuality(data.quality);
            }
        } catch (e) {
            console.error("Failed to load image default quality", e);
        }
    };

    const changeImageDefaultQuality = async (q: 'low' | 'medium' | 'high') => {
        try {
            const res = await fetch('/api/config/image-default-quality', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ quality: q })
            });
            if (res.ok) {
                setImageDefaultQuality(q);
            }
        } catch (e) {
            console.error("Failed to set image default quality", e);
        }
    };

    const loadMediaRecallState = async () => {
        try {
            const res = await fetch('/api/config/media-recall');
            if (res.ok) {
                const data = await res.json();
                setMediaRecallEnabled(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load media recall state", e);
        }
    };

    const toggleMediaRecall = async () => {
        const newState = !mediaRecallEnabled;
        try {
            const res = await fetch('/api/config/media-recall', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setMediaRecallEnabled(newState);
            }
        } catch (e) {
            console.error("Failed to toggle media recall", e);
        }
    };

    const loadGeminiAutoCacheState = async () => {
        try {
            const res = await fetch('/api/config/gemini-auto-cache');
            if (res.ok) {
                const data = await res.json();
                setGeminiAutoCacheEnabled(!!data.enabled);
                const keep = typeof data.keep_seconds === 'number' ? data.keep_seconds : 0;
                setGeminiAutoCacheKeepSeconds(keep);
                setGeminiAutoCacheKeepInput(String(keep));
                if (typeof data.keep_seconds_max === 'number') {
                    setGeminiAutoCacheKeepMax(data.keep_seconds_max);
                }
            }
        } catch (e) {
            console.error("Failed to load Gemini auto cache state", e);
        }
    };

    const saveGeminiAutoCache = async (enabled: boolean, keepSeconds: number) => {
        try {
            const res = await fetch('/api/config/gemini-auto-cache', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled, keep_seconds: keepSeconds })
            });
            if (res.ok) {
                const data = await res.json();
                setGeminiAutoCacheEnabled(!!data.enabled);
                setGeminiAutoCacheKeepSeconds(data.keep_seconds);
                setGeminiAutoCacheKeepInput(String(data.keep_seconds));
            }
        } catch (e) {
            console.error("Failed to update Gemini auto cache", e);
        }
    };

    const toggleGeminiAutoCache = () => {
        saveGeminiAutoCache(!geminiAutoCacheEnabled, geminiAutoCacheKeepSeconds);
    };

    // 数値欄は入力中に勝手に丸めると打ちにくいので、確定 (フォーカスが外れる / Enter) のときだけ保存する
    const commitGeminiAutoCacheKeepSeconds = () => {
        const parsed = Number.parseInt(geminiAutoCacheKeepInput, 10);
        const next = Number.isNaN(parsed)
            ? 0
            : Math.min(Math.max(parsed, 0), geminiAutoCacheKeepMax);
        if (next === geminiAutoCacheKeepSeconds) {
            setGeminiAutoCacheKeepInput(String(next));
            return;
        }
        saveGeminiAutoCache(geminiAutoCacheEnabled, next);
    };

    // API の一組 {target, high}。global は設定値 (null = 未設定)、builtin は組み込み既定。
    type WatermarkPayloadGroup = { target?: number | null; high?: number | null };

    const applyMetabolismDefaults = (data: {
        global?: WatermarkPayloadGroup;
        builtin?: WatermarkPayloadGroup;
        presets?: Array<Partial<WatermarkPreset>>;
    }) => {
        const g: WatermarkBarValues = {
            target: data.global?.target ?? null,
            high: data.global?.high ?? null,
        };
        setWmGlobal(g);
        // 組み込み既定は必ず数値。欠けていれば今の値のままにする (古い応答対策)。
        if (typeof data.builtin?.target === 'number' && typeof data.builtin?.high === 'number') {
            setWmBuiltin({ target: data.builtin.target, high: data.builtin.high });
        }
        setWmPresets(
            (Array.isArray(data.presets) ? data.presets : []).flatMap(p =>
                typeof p?.id === 'string' && typeof p?.label === 'string'
                    && typeof p?.target === 'number' && typeof p?.high === 'number'
                    ? [{ id: p.id, label: p.label, target: p.target, high: p.high }]
                    : [],
            ),
        );
        setWmInputs({
            target: g.target != null ? String(g.target) : '',
            high: g.high != null ? String(g.high) : '',
        });
    };

    const loadMetabolismDefaults = async () => {
        try {
            const res = await fetch('/api/config/metabolism-defaults');
            if (res.ok) {
                applyMetabolismDefaults(await res.json());
                setWmError(null);
            }
        } catch (e) {
            console.error('Failed to load metabolism defaults', e);
        }
    };

    // 欄の文字列 → 数値 (空欄 = null = 未設定)。整数でない文字は NaN で返して呼び出し側が弾く。
    const parseWatermarkInput = (raw: string): number | null => {
        const s = raw.trim();
        if (s === '') return null;
        if (!/^\d+$/.test(s)) return Number.NaN;
        return parseInt(s, 10);
    };

    // 画面上の実効値 = 欄に数字があればそれ、空欄なら組み込み既定 (棒と検査に使う)
    const wmEdited: WatermarkBarValues = {
        target: parseWatermarkInput(wmInputs.target),
        high: parseWatermarkInput(wmInputs.high),
    };
    const wmHasNaN = WATERMARK_KEYS.some(k => Number.isNaN(wmEdited[k]));
    const wmHasZero = WATERMARK_KEYS.some(k => wmEdited[k] != null && (wmEdited[k] as number) < 1);
    // 棒に渡す実効値。NaN (数字でない入力) は null 扱いで既定に落とす — `??` は NaN を
    // 通してしまい、凡例が「NaN 字」になる。
    const wmNum = (v: number | null): number | null => (v == null || Number.isNaN(v) ? null : v);
    const wmEffective: WatermarkBarValues = {
        target: wmNum(wmEdited.target) ?? wmBuiltin.target,
        high: wmNum(wmEdited.high) ?? wmBuiltin.high,
    };
    const wmBadInput = wmHasNaN || wmHasZero;
    const wmViolations: Set<WatermarkKey> =
        wmBadInput ? new Set<WatermarkKey>() : findWatermarkOrderViolations(wmEffective);
    const wmDirty = WATERMARK_KEYS.some(k => (wmEdited[k] ?? null) !== (wmGlobal[k] ?? null));
    const wmCanSave = !wmSaving && wmDirty && !wmBadInput && wmViolations.size === 0;
    // どのプリセットを選んでいる状態か。数字そのものから決めるので、欄を直接
    // 書き換えたときも表示が追いつく (どれとも一致しなければカスタム = null)。
    const wmActivePreset = wmBadInput
        ? null
        : (wmPresets.find(p => p.target === wmEffective.target && p.high === wmEffective.high) ?? null);

    const applyWatermarkPreset = (preset: WatermarkPreset) => {
        setWmInputs({ target: String(preset.target), high: String(preset.high) });
    };

    const saveMetabolismDefaults = async () => {
        if (!wmCanSave) return;
        setWmSaving(true);
        setWmError(null);
        try {
            // 変えた欄だけ送る (PUT は省略 = 触らない)。両方を送ると、最初の読み込みに
            // 失敗して欄が空のまま一欄だけ直したとき、残りを null で消してしまう。
            const body: Record<string, number | null> = {};
            for (const k of WATERMARK_KEYS) {
                if ((wmEdited[k] ?? null) !== (wmGlobal[k] ?? null)) {
                    body[WATERMARK_API_KEYS[k]] = wmEdited[k] ?? null;
                }
            }
            const res = await fetch('/api/config/metabolism-defaults', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            if (!res.ok) {
                let detail = `HTTP ${res.status}`;
                try { const j = await res.json(); if (j?.detail) detail = String(j.detail); } catch { /* 本文なし */ }
                setWmError(`保存できませんでした: ${detail}`);
                return;
            }
            applyMetabolismDefaults(await res.json());
            setWmSavedAt(Date.now());
        } catch (e) {
            setWmError(`保存できませんでした: ${e}`);
        } finally {
            setWmSaving(false);
        }
    };

    const loadDeveloperModeState = async () => {
        try {
            const res = await fetch('/api/config/developer-mode');
            if (res.ok) {
                const data = await res.json();
                setDeveloperMode(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load developer mode state", e);
        }
    };

    const toggleDeveloperMode = async () => {
        const newState = !developerMode;
        try {
            const res = await fetch('/api/config/developer-mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setDeveloperMode(newState);
            }
        } catch (e) {
            console.error("Failed to toggle developer mode", e);
        }
    };

    const loadUpdateCheckState = async () => {
        try {
            const res = await fetch('/api/config/update-check');
            if (res.ok) {
                const data = await res.json();
                setUpdateCheckEnabled(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load update check state", e);
        }
    };

    const toggleUpdateCheck = async () => {
        const newState = !updateCheckEnabled;
        try {
            const res = await fetch('/api/config/update-check', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setUpdateCheckEnabled(newState);
            }
        } catch (e) {
            console.error("Failed to toggle update check", e);
        }
    };

    const loadAnnouncementsState = async () => {
        try {
            const res = await fetch('/api/config/announcements-monitor');
            if (res.ok) {
                const data = await res.json();
                setAnnouncementsEnabled(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load announcements state", e);
        }
    };

    const toggleAnnouncements = async () => {
        const newState = !announcementsEnabled;
        try {
            const res = await fetch('/api/config/announcements-monitor', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setAnnouncementsEnabled(newState);
            }
        } catch (e) {
            console.error("Failed to toggle announcements", e);
        }
    };

    const loadEnvVars = async () => {
        setIsLoading(true);
        try {
            const res = await fetch('/api/admin/env');
            if (res.ok) {
                const data = await res.json();
                setEnvVars(data);
                // Reset edits
                setEditedEnv({});
            }
        } catch (e) {
            console.error("Failed to load env vars", e);
        } finally {
            setIsLoading(false);
        }
    };

    const handleEnvChange = (key: string, value: string) => {
        setEditedEnv(prev => ({
            ...prev,
            [key]: value
        }));
    };

    const saveEnv = async () => {
        setIsSaving(true);
        try {
            const res = await fetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: editedEnv })
            });
            if (res.ok) {
                alert("環境変数を保存しました。");
                loadEnvVars(); // Reload to confirm
            } else {
                alert("保存に失敗しました。");
            }
        } catch (e) {
            console.error("Save error", e);
        } finally {
            setIsSaving(false);
        }
    };

    const restartServer = async () => {
        if (!confirm("サーバーを再起動しますか？UIが一時的に切断されます。")) return;
        try {
            await fetch('/api/admin/restart', { method: 'POST' });
            alert("サーバーを再起動中です。数秒後にページを再読み込みしてください。");
        } catch (e) {
            console.error(e);
        }
    };

    // --- About ---
    const loadVersionInfo = async () => {
        try {
            const res = await fetch('/api/system/version');
            if (res.ok) {
                setVersionInfo(await res.json());
            }
        } catch (e) {
            console.error('Failed to load version info', e);
        }
    };

    // --- Model Roles ---
    const loadModelRoles = async () => {
        setModelRolesLoading(true);
        try {
            const [rolesRes, modelsRes] = await Promise.all([
                fetch('/api/tutorial/model-roles'),
                fetch('/api/tutorial/available-models'),
            ]);
            if (rolesRes.ok) {
                const data = await rolesRes.json();
                setModelRoles(data.current);
                setModelPresets(data.presets);
            }
            if (modelsRes.ok) {
                const data = await modelsRes.json();
                setModelsAvailable(data.models);
            }
        } catch (e) {
            console.error('Failed to load model roles', e);
        } finally {
            setModelRolesLoading(false);
        }
    };

    const handlePresetApply = async (provider: string) => {
        try {
            const res = await fetch('/api/tutorial/auto-configure-models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider }),
            });
            if (res.ok) {
                await loadModelRoles();
            }
        } catch (e) {
            console.error('Failed to apply preset', e);
        }
    };

    const handleModelRoleChange = async (envKey: string, modelId: string) => {
        try {
            await fetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: { [envKey]: modelId } }),
            });
            setExpandedModelRole(null);
            await loadModelRoles();
        } catch (e) {
            console.error('Failed to update model role', e);
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div
                className={styles.modal}
                onClick={e => e.stopPropagation()}
                // No need to stop propagation here if parent overlay already stops it,
                // but for safety in case overlay structure changes:
                onTouchStart={(e) => e.stopPropagation()}
                onTouchMove={(e) => e.stopPropagation()}
            >
                <div className={styles.header}>
                    <h2><Settings /> グローバル設定</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                <div className={styles.content}>
                    {/* Sidebar Navigation */}
                    <div className={styles.sidebar}>
                        <div
                            className={`${styles.navItem} ${activeTab === 'env' ? styles.active : ''}`}
                            onClick={() => setActiveTab('env')}
                        >
                            <Settings size={18} /> 環境
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'world' ? styles.active : ''}`}
                            onClick={() => setActiveTab('world')}
                        >
                            <Globe size={18} /> ワールドエディタ
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'feeds' ? styles.active : ''}`}
                            onClick={() => setActiveTab('feeds')}
                        >
                            <Rss size={18} /> フィード
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'models' ? styles.active : ''}`}
                            onClick={() => setActiveTab('models')}
                        >
                            <Cpu size={18} /> モデルロール
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'modelMgmt' ? styles.active : ''}`}
                            onClick={() => setActiveTab('modelMgmt')}
                        >
                            <Boxes size={18} /> モデル管理
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'playbooks' ? styles.active : ''}`}
                            onClick={() => setActiveTab('playbooks')}
                        >
                            <Layers size={18} /> Playbook権限
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'about' ? styles.active : ''}`}
                            onClick={() => setActiveTab('about')}
                        >
                            <Info size={18} /> 情報
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'utilities' ? styles.active : ''}`}
                            onClick={() => setActiveTab('utilities')}
                        >
                            <Wrench size={18} /> 便利機能
                        </div>
                    </div>

                    {/* Main Content Panel */}
                    <div className={styles.mainPanel}>
                        {activeTab === 'env' && (
                            <div className={styles.envContainer}>
                                {/* Theme Selector */}
                                <div className={styles.themeContainer}>
                                    <div>
                                        <div className={styles.themeLabel}>
                                            {theme === 'dark' ? <Moon size={18} /> : theme === 'light' ? <Sun size={18} /> : <Monitor size={18} />}
                                            テーマ
                                        </div>
                                        <div className={styles.themeDescription}>
                                            UIの表示モードを切り替えます
                                        </div>
                                    </div>
                                    <div className={styles.themeSelector}>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'system' ? styles.active : ''}`}
                                            onClick={() => changeTheme('system')}
                                        >
                                            <Monitor size={14} /> System
                                        </button>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'light' ? styles.active : ''}`}
                                            onClick={() => changeTheme('light')}
                                        >
                                            <Sun size={14} /> Light
                                        </button>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'dark' ? styles.active : ''}`}
                                            onClick={() => changeTheme('dark')}
                                        >
                                            <Moon size={14} /> Dark
                                        </button>
                                    </div>
                                </div>

                                {/* Image Default Quality Selector */}
                                <div className={styles.themeContainer}>
                                    <div>
                                        <div className={styles.themeLabel}>
                                            <Layers size={18} />
                                            画像生成デフォルト品質
                                        </div>
                                        <div className={styles.themeDescription}>
                                            画像生成ツールでquality未指定時のデフォルト品質を設定します
                                        </div>
                                    </div>
                                    <div className={styles.themeSelector}>
                                        <button
                                            className={`${styles.themeOption} ${imageDefaultQuality === 'low' ? styles.active : ''}`}
                                            onClick={() => changeImageDefaultQuality('low')}
                                        >
                                            Low
                                        </button>
                                        <button
                                            className={`${styles.themeOption} ${imageDefaultQuality === 'medium' ? styles.active : ''}`}
                                            onClick={() => changeImageDefaultQuality('medium')}
                                        >
                                            Medium
                                        </button>
                                        <button
                                            className={`${styles.themeOption} ${imageDefaultQuality === 'high' ? styles.active : ''}`}
                                            onClick={() => changeImageDefaultQuality('high')}
                                        >
                                            High
                                        </button>
                                    </div>
                                </div>

                                <div
                                    className={styles.sectionHeader}
                                    style={{ cursor: 'pointer', userSelect: 'none' }}
                                    onClick={() => setEnvSectionOpen(!envSectionOpen)}
                                >
                                    <h3>
                                        {envSectionOpen ? <ChevronDown size={16} style={{ verticalAlign: 'middle', marginRight: 4 }} /> : <ChevronRight size={16} style={{ verticalAlign: 'middle', marginRight: 4 }} />}
                                        サーバー環境変数 (.env)
                                    </h3>
                                    <button className={styles.restartBtn} onClick={(e) => { e.stopPropagation(); restartServer(); }}>
                                        <Power size={16} /> サーバー再起動
                                    </button>
                                </div>

                                {envSectionOpen && (isLoading ? (
                                    <div>読み込み中...</div>
                                ) : (
                                    <>
                                        <div className={styles.envList}>
                                            {envVars.map(item => (
                                                <div key={item.key} className={styles.envItem}>
                                                    <div className={styles.envKey}>{item.key}</div>
                                                    <input
                                                        className={styles.envInput}
                                                        type={item.is_sensitive ? "password" : "text"}
                                                        defaultValue={item.is_sensitive ? "" : item.value}
                                                        placeholder={item.is_sensitive ? "（非表示/変更なし）" : ""}
                                                        onChange={(e) => handleEnvChange(item.key, e.target.value)}
                                                    />
                                                </div>
                                            ))}
                                        </div>
                                        <div className={styles.actionFooter}>
                                            <button
                                                className={styles.saveBtn}
                                                onClick={saveEnv}
                                                disabled={isSaving || Object.keys(editedEnv).length === 0}
                                            >
                                                {isSaving ? <RefreshCw className="spin" /> : <Save />} 保存
                                            </button>
                                        </div>
                                    </>
                                ))}

                                {/* Update Check Toggle */}
                                <div className={styles.toggleContainer} style={{ marginTop: '1.5rem' }}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            アップデート通知
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            新しいバージョンの有無を定期的にチェックします
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${updateCheckEnabled ? styles.active : ''}`}
                                        onClick={toggleUpdateCheck}
                                    />
                                </div>

                                {/* Announcements Monitor Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            お知らせ通知
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            開発者からのお知らせを定期的に取得します
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${announcementsEnabled ? styles.active : ''}`}
                                        onClick={toggleAnnouncements}
                                    />
                                </div>

                                {/* Media Recall Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            添付したメディアの内容を自動想起に使う
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            オンにすると、画像・音声・動画を添付したときに内容を読み取ってから思い出しに使います。読み取りの分だけ返信が数秒遅くなります。
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${mediaRecallEnabled ? styles.active : ''}`}
                                        onClick={toggleMediaRecall}
                                    />
                                </div>

                                {/* Gemini Auto Cache Toggle */}
                                <div className={`${styles.toggleContainer} ${styles.toggleContainerStacked}`}>
                                    <div className={styles.toggleRow}>
                                        <div>
                                            <div className={styles.toggleLabel}>
                                                Gemini 自動キャッシュ（実験的）
                                            </div>
                                            <div className={styles.toggleDescription}>
                                                全ての Gemini 呼び出しでキャッシュを自動作成し、入力トークンをキャッシュ価格にします。保持秒数が 0 のときは応答後すぐ削除します。
                                            </div>
                                        </div>
                                        <div
                                            className={`${styles.toggle} ${geminiAutoCacheEnabled ? styles.active : ''}`}
                                            onClick={toggleGeminiAutoCache}
                                        />
                                    </div>
                                    {geminiAutoCacheEnabled && (
                                        <div className={styles.subSetting}>
                                            <label className={styles.subSettingLabel} htmlFor="gemini-auto-cache-keep">
                                                応答後の保持秒数
                                            </label>
                                            <input
                                                id="gemini-auto-cache-keep"
                                                type="number"
                                                min={0}
                                                max={geminiAutoCacheKeepMax}
                                                step={1}
                                                className={styles.subSettingInput}
                                                value={geminiAutoCacheKeepInput}
                                                onChange={e => setGeminiAutoCacheKeepInput(e.target.value)}
                                                onBlur={commitGeminiAutoCacheKeepSeconds}
                                                onKeyDown={e => { if (e.key === 'Enter') e.currentTarget.blur(); }}
                                            />
                                            <div className={styles.subSettingHint}>
                                                1 以上にすると、その秒数のあいだ Gemini 側にキャッシュを残します（最大 {geminiAutoCacheKeepMax} 秒）。機能が不完全な部分があります。自己責任でご利用ください。
                                            </div>
                                        </div>
                                    )}
                                </div>

                                {/* Developer Mode Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            <Cpu size={18} />
                                            開発者モード
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            ONにすると開発中の機能が表示されます（不安定なため推奨しません）
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${developerMode ? styles.active : ''}`}
                                        onClick={toggleDeveloperMode}
                                    />
                                </div>

                                {/* ペルソナに送る量 (全体既定) */}
                                <div className={`${styles.toggleContainer} ${styles.toggleContainerStacked}`}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            <Layers size={18} />
                                            ペルソナに送る量
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            ペルソナに毎回送る会話がどれだけ溜まったら、古い部分をあらすじへ畳んで整理するかを決めます。ここは全モデル共通の既定値です。
                                        </div>
                                    </div>

                                    {wmPresets.length > 0 && (
                                        <div className={styles.wmPresetArea}>
                                            <div className={styles.wmPresetRow}>
                                                {wmPresets.map(preset => (
                                                    <button
                                                        key={preset.id}
                                                        type="button"
                                                        className={`${styles.wmPresetBtn} ${wmActivePreset?.id === preset.id ? styles.wmPresetBtnActive : ''}`}
                                                        aria-pressed={wmActivePreset?.id === preset.id}
                                                        onClick={() => applyWatermarkPreset(preset)}
                                                    >
                                                        <span className={styles.wmPresetLabel}>{preset.label}</span>
                                                        <span className={styles.wmPresetNums}>
                                                            {preset.high.toLocaleString()} 字で整理し、{preset.target.toLocaleString()} 字残す
                                                        </span>
                                                    </button>
                                                ))}
                                                <button
                                                    type="button"
                                                    className={`${styles.wmPresetBtn} ${wmActivePreset == null ? styles.wmPresetBtnActive : ''}`}
                                                    aria-pressed={wmActivePreset == null}
                                                    onClick={() => wmTargetInputRef.current?.focus()}
                                                >
                                                    <span className={styles.wmPresetLabel}>カスタム</span>
                                                    <span className={styles.wmPresetNums}>下の欄で自分で決める</span>
                                                </button>
                                            </div>
                                            <div className={styles.wmPresetNote}>
                                                <Info size={14} />
                                                <span>
                                                    大きい設定ほど、一回に送る量は増えますが、会話の整理はあまり走りません。小さい設定にすると一回に送る量は減りますが、そのぶん整理が何度も走り、そのたびに AI への問い合わせが増えて、それまで送っていた内容をもう一度送り直すことになります。料金や待ち時間がかえって増えることがあるので、迷ったときは「デフォルト」に戻してください。
                                                </span>
                                            </div>
                                        </div>
                                    )}

                                    <div className={styles.wmGroup}>
                                        <div className={styles.wmBarArea}>
                                            <WatermarkBar
                                                values={wmEffective}
                                                invalidKeys={wmViolations}
                                                labels={WATERMARK_LABELS}
                                            />
                                        </div>
                                        <div className={styles.wmFields}>
                                            {WATERMARK_KEYS.map(k => {
                                                const edited = wmEdited[k];
                                                const isUser = edited != null;
                                                const builtin = wmBuiltin[k];
                                                const bad = wmViolations.has(k) || Number.isNaN(edited) || (edited != null && (edited as number) < 1);
                                                const inputId = `wm-${k}`;
                                                return (
                                                    <div key={k} className={styles.wmField}>
                                                        <label className={styles.subSettingLabel} htmlFor={inputId}>
                                                            {WATERMARK_LABELS[k]}
                                                            <span className={`${styles.wmBadge} ${isUser ? styles.wmBadgeUser : ''}`}>
                                                                {isUser
                                                                    ? '設定した値'
                                                                    : builtin != null ? `既定 ${builtin.toLocaleString()} 字` : '既定'}
                                                            </span>
                                                        </label>
                                                        <div className={styles.wmInputRow}>
                                                            <input
                                                                id={inputId}
                                                                ref={k === 'target' ? wmTargetInputRef : undefined}
                                                                type="text"
                                                                inputMode="numeric"
                                                                className={`${styles.subSettingInput} ${bad ? styles.wmInputBad : ''}`}
                                                                value={wmInputs[k]}
                                                                placeholder={builtin != null ? builtin.toLocaleString() : ''}
                                                                onChange={e => {
                                                                    const v = e.target.value;
                                                                    setWmInputs(prev => ({ ...prev, [k]: v }));
                                                                }}
                                                                onKeyDown={e => { if (e.key === 'Enter') saveMetabolismDefaults(); }}
                                                            />
                                                            <span className={styles.wmUnit}>字</span>
                                                            <button
                                                                type="button"
                                                                className={styles.wmResetBtn}
                                                                disabled={wmInputs[k] === ''}
                                                                onClick={() => setWmInputs(prev => ({ ...prev, [k]: '' }))}
                                                            >
                                                                既定に戻す
                                                            </button>
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                        {wmViolations.size > 0 && (
                                            <div className={styles.wmMessageBad}>
                                                {WATERMARK_LABELS.target} ≤ {WATERMARK_LABELS.high} の順にしてください
                                            </div>
                                        )}
                                    </div>

                                    {(wmHasNaN || wmHasZero) && (
                                        <div className={styles.wmMessageBad}>
                                            1 以上の整数を入力してください（空欄 = 既定に戻す）
                                        </div>
                                    )}
                                    {wmError && <div className={styles.wmMessageBad}>{wmError}</div>}
                                    <div className={styles.wmFooter}>
                                        <span className={styles.subSettingHint}>
                                            モデル設定で数値を入れたモデルはそちらが優先されます。空欄のモデルはこの値に従います。
                                        </span>
                                        <button
                                            type="button"
                                            className={styles.saveBtn}
                                            disabled={!wmCanSave}
                                            onClick={saveMetabolismDefaults}
                                        >
                                            <Save size={16} /> {wmSaving ? '保存中...' : wmSavedAt && !wmDirty ? '保存しました' : '保存'}
                                        </button>
                                    </div>
                                </div>

                            </div>
                        )}

                        {activeTab === 'world' && (
                            <WorldEditor />
                        )}

                        {activeTab === 'models' && (
                            <div className={styles.modelsContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3>モデルロール設定</h3>
                                </div>

                                {modelRolesLoading ? (
                                    <div>読み込み中...</div>
                                ) : (
                                    <>
                                        {modelPresets.length > 0 && (
                                            <div className={styles.presetContainer}>
                                                <div className={styles.presetHeader}>プリセット切替</div>
                                                <div className={styles.presetDescription}>
                                                    プロバイダを選択すると、全ロールのモデルを一括変更します
                                                </div>
                                                <div className={styles.presetList}>
                                                    {modelPresets.filter(p => p.is_available).map((preset) => (
                                                        <button
                                                            key={preset.provider}
                                                            className={styles.presetBtn}
                                                            onClick={() => handlePresetApply(preset.provider)}
                                                        >
                                                            {preset.display_name}
                                                        </button>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        <div className={styles.rolesList}>
                                            {Object.entries(modelRoles).map(([role, info]) => (
                                                <div key={role} className={styles.roleItem}>
                                                    <div className={styles.roleHeader}>
                                                        <div className={styles.roleInfo}>
                                                            <span className={styles.roleLabel}>{info.label}</span>
                                                            <span className={styles.roleDescription}>{info.description}</span>
                                                        </div>
                                                        <div className={styles.roleValue}>
                                                            <span className={styles.roleModelName}>
                                                                {info.display_name || info.value || '(未設定)'}
                                                            </span>
                                                            <button
                                                                className={styles.roleChangeBtn}
                                                                onClick={() => setExpandedModelRole(
                                                                    expandedModelRole === role ? null : role
                                                                )}
                                                            >
                                                                <ChevronDown size={14} />
                                                                <span>変更</span>
                                                            </button>
                                                        </div>
                                                    </div>
                                                    {expandedModelRole === role && (
                                                        <div className={styles.roleDropdown}>
                                                            {modelsAvailable
                                                                .filter(m => m.is_available)
                                                                .map(model => (
                                                                    <div
                                                                        key={model.id}
                                                                        className={`${styles.roleDropdownItem} ${model.id === info.value ? styles.selected : ''}`}
                                                                        onClick={() => handleModelRoleChange(info.env_key, model.id)}
                                                                    >
                                                                        <span className={styles.roleDropdownName}>{model.display_name}</span>
                                                                        <span className={styles.roleDropdownProvider}>{model.provider}</span>
                                                                    </div>
                                                                ))
                                                            }
                                                        </div>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    </>
                                )}
                            </div>
                        )}

                        {activeTab === 'modelMgmt' && (
                            <div>
                                <div className={styles.subTabRow}>
                                    <button
                                        className={`${styles.subTab} ${modelMgmtSubTab === 'providers' ? styles.subTabActive : ''}`}
                                        onClick={() => setModelMgmtSubTab('providers')}
                                    >
                                        プロバイダ
                                    </button>
                                    <button
                                        className={`${styles.subTab} ${modelMgmtSubTab === 'models' ? styles.subTabActive : ''}`}
                                        onClick={() => setModelMgmtSubTab('models')}
                                    >
                                        モデル
                                    </button>
                                </div>
                                {modelMgmtSubTab === 'providers' && <ProviderManagementPanel />}
                                {modelMgmtSubTab === 'models' && <ModelManagementPanel />}
                            </div>
                        )}

                        {activeTab === 'feeds' && <FeedManagementPanel />}

                        {activeTab === 'playbooks' && (
                            <div className={styles.envContainer}>
                                <div className={styles.sectionHeader}>
                                    <div>
                                        <h3>Playbook実行権限</h3>
                                        <p className={styles.pbSubtitle}>
                                            ペルソナが各Playbookを自動実行する際の権限レベルを設定します
                                        </p>
                                    </div>
                                </div>

                                {playbookPermsLoading ? (
                                    <div className={styles.pbEmpty}>
                                        <RefreshCw size={20} style={{ animation: 'spin 1s linear infinite' }} /> 読み込み中...
                                    </div>
                                ) : playbookPerms.length === 0 ? (
                                    <p className={styles.pbEmpty}>
                                        Router呼び出し可能なPlaybookがありません
                                    </p>
                                ) : (
                                    <div className={styles.pbList}>
                                        {playbookPerms.map(p => (
                                            <div key={p.playbook_name} className={styles.pbItem}>
                                                <div className={styles.pbItemInfo}>
                                                    <div className={styles.pbItemName}>
                                                        {p.display_name}
                                                    </div>
                                                    {p.description && (
                                                        <div className={styles.pbItemDesc}>
                                                            {p.description}
                                                        </div>
                                                    )}
                                                </div>
                                                <select
                                                    className={styles.pbSelect}
                                                    value={p.permission_level}
                                                    onChange={e => updatePlaybookPerm(p.playbook_name, e.target.value)}
                                                >
                                                    <option value="auto_allow">自動実行OK</option>
                                                    <option value="ask_every_time">毎回許可が必要</option>
                                                    <option value="user_only">ユーザー指定時のみ</option>
                                                    {p.permission_level === 'blocked' && (
                                                        <option value="blocked" disabled>使用禁止</option>
                                                    )}
                                                </select>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}

                        {activeTab === 'about' && (
                            <div className={styles.aboutContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3>SAIVerseについて</h3>
                                </div>

                                {/* Version */}
                                {versionInfo && (
                                    <div className={styles.aboutCard}>
                                        <div className={styles.aboutVersion}>
                                            v{versionInfo.version}
                                        </div>
                                        {versionInfo.update_available && (
                                            <div className={styles.aboutUpdateNotice}>
                                                新しいバージョン v{versionInfo.latest_version} が利用可能です
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* Developer */}
                                <div className={styles.aboutCard}>
                                    <div className={styles.aboutCardTitle}>開発者</div>
                                    <div className={styles.aboutDeveloper}>
                                        <span>まはー</span>
                                        <a href="https://x.com/Lize_san_suki" target="_blank" rel="noopener noreferrer" className={styles.aboutLink}>
                                            <ExternalLink size={14} /> @Lize_san_suki
                                        </a>
                                    </div>
                                </div>

                                {/* Links */}
                                <div className={styles.aboutCard}>
                                    <div className={styles.aboutCardTitle}>リンク</div>
                                    <div className={styles.aboutLinks}>
                                        <a href="https://saiverse.net/" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>🌐</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>公式サイト</div>
                                                <div className={styles.aboutLinkDesc}>saiverse.net</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://discord.gg/qMcgEk83Ag" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>💬</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>Discord コミュニティ</div>
                                                <div className={styles.aboutLinkDesc}>質問・雑談・バグ報告など</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://github.com/maha0525/SAIVerse" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>📦</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>GitHub</div>
                                                <div className={styles.aboutLinkDesc}>ソースコード・Issues</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://note.com/maha0525/n/n5a63f572be8f" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>📝</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>Note</div>
                                                <div className={styles.aboutLinkDesc}>開発記録・サポート（チップ）</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                    </div>
                                </div>

                                {/* Support */}
                                <div className={styles.aboutCard}>
                                    <div className={styles.aboutCardTitle}>支援について</div>
                                    <div className={styles.aboutSupportText}>
                                        SAIVerseはフリーソフトウェアとして開発を続けています。
                                    </div>
                                    <div className={styles.aboutSupportItems}>
                                        <a href="https://github.com/sponsors/maha0525" target="_blank" rel="noopener noreferrer" className={styles.aboutSupportItem} style={{ cursor: 'pointer' }}>
                                            <span className={`${styles.aboutSupportBadge} ${styles.active}`}>受付中</span>
                                            GitHub Sponsors
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://note.com/maha0525/n/n5a63f572be8f" target="_blank" rel="noopener noreferrer" className={styles.aboutSupportItem} style={{ cursor: 'pointer' }}>
                                            <span className={`${styles.aboutSupportBadge} ${styles.active}`}>受付中</span>
                                            Noteからチップを送る
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                    </div>
                                </div>
                            </div>
                        )}

                        {activeTab === 'utilities' && (
                            <div className={styles.utilitiesContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3>便利機能</h3>
                                </div>

                                {/* アイテム概要の一括生成 */}
                                <div className={styles.utilityCard}>
                                    <h4 className={styles.utilityTitle}>アイテム概要の一括生成</h4>
                                    <p className={styles.utilityDesc}>
                                        概要が未設定（またはデフォルト）の画像アイテムに対して、作成当時の会話履歴を参照しながら概要を自動生成します。
                                    </p>

                                    <div className={styles.utilityForm}>
                                        <div className={styles.utilityRow}>
                                            <label>対象Building</label>
                                            <select value={bfBuildingId} onChange={e => { setBfBuildingId(e.target.value); setBfPersonaId(''); }}>
                                                <option value="">すべて（City全体）</option>
                                                {bfBuildings.map(b => (
                                                    <option key={b.id} value={b.id}>{b.name}</option>
                                                ))}
                                            </select>
                                        </div>

                                        {bfBuildingId && (
                                            <div className={styles.utilityRow}>
                                                <label>参照ペルソナ</label>
                                                <select value={bfPersonaId} onChange={e => setBfPersonaId(e.target.value)}>
                                                    <option value="">自動（全ペルソナから最近傍を選択）</option>
                                                    {bfPersonas.map(p => (
                                                        <option key={p.persona_id} value={p.persona_id}>{p.persona_name}</option>
                                                    ))}
                                                </select>
                                            </div>
                                        )}

                                        <div className={styles.utilityRow}>
                                            <label className={styles.checkboxLabel}>
                                                <input type="checkbox" checked={bfDryRun} onChange={e => setBfDryRun(e.target.checked)} />
                                                ドライラン（確認のみ・DBに書き込まない）
                                            </label>
                                        </div>

                                        <button
                                            className={styles.utilityRunBtn}
                                            onClick={runBackfill}
                                            disabled={bfRunning}
                                        >
                                            {bfRunning ? <><Loader size={14} className={styles.spin} /> 処理中...</> : '実行'}
                                        </button>
                                    </div>

                                    {bfResults && (
                                        <div className={styles.utilityResults}>
                                            <div className={styles.utilityStats}>
                                                <span className={styles.statUpdated}>更新: {bfResults.processed}</span>
                                                <span className={styles.statSkipped}>スキップ: {bfResults.skipped}</span>
                                                <span className={styles.statFailed}>失敗: {bfResults.failed}</span>
                                                {bfDryRun && <span className={styles.dryRunBadge}>DRY RUN</span>}
                                            </div>
                                            <div className={styles.utilityResultList}>
                                                {bfResults.results.map(r => (
                                                    <div key={r.item_id} className={`${styles.utilityResultItem} ${styles[`result_${r.status}`]}`}>
                                                        <span className={styles.resultIcon}>
                                                            {r.status === 'updated' || r.status === 'dry_run'
                                                                ? <CheckCircle size={14} />
                                                                : r.status === 'failed'
                                                                    ? <XCircle size={14} />
                                                                    : <span>—</span>}
                                                        </span>
                                                        <div className={styles.resultBody}>
                                                            <span className={styles.resultName}>{r.item_name}</span>
                                                            {r.description && <span className={styles.resultDesc}>{r.description}</span>}
                                                            {r.reason && <span className={styles.resultReason}>{r.reason}</span>}
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
