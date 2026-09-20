import { apiFetch } from '@/i18n/api';
import { getFormatLocale, t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { resolveI18nText } from '@/i18n/resolve';
import LocaleControls from '@/i18n/LocaleControls';
import { getModelRoleLabel, getModelRoleDescription, getProviderPresetDisplayName, getWatermarkPresetLabel } from '@/i18n/modelRoles';
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
    /** 反射判断専用の宛先 (型付きの質問に確率で答えるだけで、文章を書けない)。
     *  反射判断の役割の選択肢にだけ出す。 */
    reflex_only?: boolean;
}

/** 反射判断の役割のキー (saiverse/model_defaults.py の MODEL_ROLES と同じ名前)。
 *  この役割だけが、文章を書けない反射判断専用の宛先を選べる。 */
const REFLEX_JUDGMENT_ROLE = 'reflex_judgment_model';

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
    display_name_en?: string;
    display_name_i18n?: Record<string, string>;
    description: string;
    description_en?: string;
    description_i18n?: Record<string, string>;
    permission_level: string;
}

type TabId = 'env' | 'world' | 'feeds' | 'models' | 'modelMgmt' | 'playbooks' | 'about' | 'utilities';
type ModelMgmtSubTab = 'providers' | 'models';

export default function GlobalSettingsModal({ isOpen, onClose }: GlobalSettingsModalProps) {
    const currentLocale = useLocale();
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
            apiFetch('/api/user/buildings'),
            apiFetch('/api/usage/personas'),
        ]);
        if (bRes.ok) { const d = await bRes.json(); setBfBuildings(d.buildings || []); }
        if (pRes.ok) { const d = await pRes.json(); setBfPersonas(d); }
    }, [bfBuildings.length]);

    const runBackfill = async () => {
        setBfRunning(true);
        setBfResults(null);
        try {
            const res = await apiFetch('/api/admin/backfill-item-descriptions', {
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
            const res = await apiFetch('/api/config/playbook-permissions');
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
            const res = await apiFetch('/api/config/playbook-permissions', {
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
            const res = await apiFetch('/api/config/image-default-quality');
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
            const res = await apiFetch('/api/config/image-default-quality', {
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
            const res = await apiFetch('/api/config/media-recall');
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
            const res = await apiFetch('/api/config/media-recall', {
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
            const res = await apiFetch('/api/config/gemini-auto-cache');
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
            const res = await apiFetch('/api/config/gemini-auto-cache', {
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
            const res = await apiFetch('/api/config/metabolism-defaults');
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
        // 「デフォルト」だけは数字を書き込まず、未設定 (空欄) に戻す。明示値で保存すると、
        // 将来組み込みの既定が変わったとき、このユーザーだけ古い数字に取り残される。
        // 未設定なら常に組み込みの既定へ追従する (実効値は同じなので選択表示も
        // デフォルトのまま光る)。
        if (preset.id === 'default') {
            setWmInputs({ target: '', high: '' });
            return;
        }
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
                setWmError(uiText("components.GlobalSettingsModal.text001", { p1: detail }));
                return;
            }
            applyMetabolismDefaults(await res.json());
            setWmSavedAt(Date.now());
        } catch (e) {
            setWmError(uiText("components.GlobalSettingsModal.text002", { p1: e }));
        } finally {
            setWmSaving(false);
        }
    };

    const loadDeveloperModeState = async () => {
        try {
            const res = await apiFetch('/api/config/developer-mode');
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
            const res = await apiFetch('/api/config/developer-mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setDeveloperMode(newState);
                window.dispatchEvent(new CustomEvent('saiverse-developer-mode', { detail: newState }));
            }
        } catch (e) {
            console.error("Failed to toggle developer mode", e);
        }
    };

    const loadUpdateCheckState = async () => {
        try {
            const res = await apiFetch('/api/config/update-check');
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
            const res = await apiFetch('/api/config/update-check', {
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
            const res = await apiFetch('/api/config/announcements-monitor');
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
            const res = await apiFetch('/api/config/announcements-monitor', {
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
            const res = await apiFetch('/api/admin/env');
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
            const res = await apiFetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: editedEnv })
            });
            if (res.ok) {
                // 保存しなかったモデル設定や、切り替えられなかったペルソナの知らせ
                const data = await res.json().catch(() => null);
                const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
                alert(notices.length > 0
                    ? `${uiText("components.GlobalSettingsModal.text003")}\n\n${notices.join('\n\n')}`
                    : uiText("components.GlobalSettingsModal.text003"));
                loadEnvVars(); // Reload to confirm
            } else {
                alert(uiText("components.GlobalSettingsModal.text004"));
            }
        } catch (e) {
            console.error("Save error", e);
        } finally {
            setIsSaving(false);
        }
    };

    const restartServer = async () => {
        if (!confirm(uiText("components.GlobalSettingsModal.text005"))) return;
        try {
            await apiFetch('/api/admin/restart', { method: 'POST' });
            alert(uiText("components.GlobalSettingsModal.text006"));
        } catch (e) {
            console.error(e);
        }
    };

    // --- About ---
    const loadVersionInfo = async () => {
        try {
            const res = await apiFetch('/api/system/version');
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
                apiFetch('/api/tutorial/model-roles'),
                apiFetch('/api/tutorial/available-models'),
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
            const res = await apiFetch('/api/tutorial/auto-configure-models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider }),
            });
            if (res.ok) {
                const data = await res.json().catch(() => null);
                const warnings: string[] = Array.isArray(data?.warnings) ? data.warnings : [];
                if (warnings.length > 0) {
                    alert(warnings.join('\n\n'));
                }
                await loadModelRoles();
            }
        } catch (e) {
            console.error('Failed to apply preset', e);
        }
    };

    const handleModelRoleChange = async (envKey: string, modelId: string) => {
        try {
            const res = await apiFetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: { [envKey]: modelId } }),
            });
            if (res.ok) {
                // 保存しなかったモデル設定や、切り替えられなかったペルソナの知らせ
                const data = await res.json().catch(() => null);
                const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
                if (notices.length > 0) {
                    alert(notices.join('\n\n'));
                }
            }
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
                    <h2 data-i18n="components.GlobalSettingsModal.text007"><Settings />{uiText("components.GlobalSettingsModal.text007")}</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                <div className={styles.content}>
                    {/* Sidebar Navigation */}
                    <div className={styles.sidebar}>
                        <div data-i18n="components.GlobalSettingsModal.text008"
                            className={`${styles.navItem} ${activeTab === 'env' ? styles.active : ''}`}
                            onClick={() => setActiveTab('env')}
                        >
                            <Settings size={18} />{uiText("components.GlobalSettingsModal.text008")}</div>
                        <div data-i18n="components.GlobalSettingsModal.text009"
                            className={`${styles.navItem} ${activeTab === 'world' ? styles.active : ''}`}
                            onClick={() => setActiveTab('world')}
                        >
                            <Globe size={18} />{uiText("components.GlobalSettingsModal.text009")}</div>
                        <div data-i18n="components.GlobalSettingsModal.text010"
                            className={`${styles.navItem} ${activeTab === 'feeds' ? styles.active : ''}`}
                            onClick={() => setActiveTab('feeds')}
                        >
                            <Rss size={18} />{uiText("components.GlobalSettingsModal.text010")}</div>
                        <div data-i18n="components.GlobalSettingsModal.text011"
                            className={`${styles.navItem} ${activeTab === 'models' ? styles.active : ''}`}
                            onClick={() => setActiveTab('models')}
                        >
                            <Cpu size={18} />{uiText("components.GlobalSettingsModal.text011")}</div>
                        <div data-i18n="components.GlobalSettingsModal.text012"
                            className={`${styles.navItem} ${activeTab === 'modelMgmt' ? styles.active : ''}`}
                            onClick={() => setActiveTab('modelMgmt')}
                        >
                            <Boxes size={18} />{uiText("components.GlobalSettingsModal.text012")}</div>
                        <div data-i18n="components.GlobalSettingsModal.text013"
                            className={`${styles.navItem} ${activeTab === 'playbooks' ? styles.active : ''}`}
                            onClick={() => setActiveTab('playbooks')}
                        >
                            <Layers size={18} />{uiText("components.GlobalSettingsModal.text013")}</div>
                        <div data-i18n="components.GlobalSettingsModal.text014"
                            className={`${styles.navItem} ${activeTab === 'about' ? styles.active : ''}`}
                            onClick={() => setActiveTab('about')}
                        >
                            <Info size={18} />{uiText("components.GlobalSettingsModal.text014")}</div>
                        <div data-i18n="components.GlobalSettingsModal.text015"
                            className={`${styles.navItem} ${activeTab === 'utilities' ? styles.active : ''}`}
                            onClick={() => setActiveTab('utilities')}
                        >
                            <Wrench size={18} />{uiText("components.GlobalSettingsModal.text015")}</div>
                    </div>

                    {/* Main Content Panel */}
                    <div className={styles.mainPanel}>
                        {activeTab === 'env' && (
                            <div className={styles.envContainer}>
                                {/* Theme Selector */}
                                <LocaleControls />
                                <div className={styles.themeContainer}>
                                    <div>
                                        <div data-i18n="components.GlobalSettingsModal.text016" className={styles.themeLabel}>
                                            {theme === 'dark' ? <Moon size={18} /> : theme === 'light' ? <Sun size={18} /> : <Monitor size={18} />}{uiText("components.GlobalSettingsModal.text016")}</div>
                                        <div data-i18n="components.GlobalSettingsModal.text017" className={styles.themeDescription}>{uiText("components.GlobalSettingsModal.text017")}</div>
                                    </div>
                                    <div className={styles.themeSelector}>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'system' ? styles.active : ''}`}
                                            onClick={() => changeTheme('system')}
                                        >
                                            <Monitor size={14} /> {uiText("components.GlobalSettingsModal.label001")}</button>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'light' ? styles.active : ''}`}
                                            onClick={() => changeTheme('light')}
                                        >
                                            <Sun size={14} /> {uiText("components.GlobalSettingsModal.label002")}</button>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'dark' ? styles.active : ''}`}
                                            onClick={() => changeTheme('dark')}
                                        >
                                            <Moon size={14} /> {uiText("components.GlobalSettingsModal.label003")}</button>
                                    </div>
                                </div>

                                {/* Image Default Quality Selector */}
                                <div className={styles.themeContainer}>
                                    <div>
                                        <div data-i18n="components.GlobalSettingsModal.text018" className={styles.themeLabel}>
                                            <Layers size={18} />{uiText("components.GlobalSettingsModal.text018")}</div>
                                        <div data-i18n="components.GlobalSettingsModal.text019" className={styles.themeDescription}>{uiText("components.GlobalSettingsModal.text019")}</div>
                                    </div>
                                    <div className={styles.themeSelector}>
                                        <button
                                            className={`${styles.themeOption} ${imageDefaultQuality === 'low' ? styles.active : ''}`}
                                            onClick={() => changeImageDefaultQuality('low')}
                                        >
                                            {uiText("components.GlobalSettingsModal.label004")}</button>
                                        <button
                                            className={`${styles.themeOption} ${imageDefaultQuality === 'medium' ? styles.active : ''}`}
                                            onClick={() => changeImageDefaultQuality('medium')}
                                        >
                                            {uiText("components.GlobalSettingsModal.label005")}</button>
                                        <button
                                            className={`${styles.themeOption} ${imageDefaultQuality === 'high' ? styles.active : ''}`}
                                            onClick={() => changeImageDefaultQuality('high')}
                                        >
                                            {uiText("components.GlobalSettingsModal.label006")}</button>
                                    </div>
                                </div>

                                <div
                                    className={styles.sectionHeader}
                                    style={{ cursor: 'pointer', userSelect: 'none' }}
                                    onClick={() => setEnvSectionOpen(!envSectionOpen)}
                                >
                                    <h3 data-i18n="components.GlobalSettingsModal.text020">
                                        {envSectionOpen ? <ChevronDown size={16} style={{ verticalAlign: 'middle', marginRight: 4 }} /> : <ChevronRight size={16} style={{ verticalAlign: 'middle', marginRight: 4 }} />}{uiText("components.GlobalSettingsModal.text020")}</h3>
                                    <button data-i18n="components.GlobalSettingsModal.text021" className={styles.restartBtn} onClick={(e) => { e.stopPropagation(); restartServer(); }}>
                                        <Power size={16} />{uiText("components.GlobalSettingsModal.text021")}</button>
                                </div>

                                {envSectionOpen && (isLoading ? (
                                    <div data-i18n="components.GlobalSettingsModal.text022">{uiText("components.GlobalSettingsModal.text022")}</div>
                                ) : (
                                    <>
                                        <div className={styles.envList}>
                                            {envVars.map(item => (
                                                <div key={item.key} className={styles.envItem}>
                                                    <div className={styles.envKey}>{item.key}</div>
                                                    <input data-i18n="components.GlobalSettingsModal.text023"
                                                        className={styles.envInput}
                                                        type={item.is_sensitive ? "password" : "text"}
                                                        defaultValue={item.is_sensitive ? "" : item.value}
                                                        placeholder={item.is_sensitive ? uiText("components.GlobalSettingsModal.text023") : ""}
                                                        onChange={(e) => handleEnvChange(item.key, e.target.value)}
                                                    />
                                                </div>
                                            ))}
                                        </div>
                                        <div className={styles.actionFooter}>
                                            <button data-i18n="components.GlobalSettingsModal.text024"
                                                className={styles.saveBtn}
                                                onClick={saveEnv}
                                                disabled={isSaving || Object.keys(editedEnv).length === 0}
                                            >
                                                {isSaving ? <RefreshCw className="spin" /> : <Save />}{uiText("components.GlobalSettingsModal.text024")}</button>
                                        </div>
                                    </>
                                ))}

                                {/* Update Check Toggle */}
                                <div className={styles.toggleContainer} style={{ marginTop: '1.5rem' }}>
                                    <div>
                                        <div data-i18n="components.GlobalSettingsModal.text025" className={styles.toggleLabel}>{uiText("components.GlobalSettingsModal.text025")}</div>
                                        <div data-i18n="components.GlobalSettingsModal.text026" className={styles.toggleDescription}>{uiText("components.GlobalSettingsModal.text026")}</div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${updateCheckEnabled ? styles.active : ''}`}
                                        onClick={toggleUpdateCheck}
                                    />
                                </div>

                                {/* Announcements Monitor Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div data-i18n="components.GlobalSettingsModal.text027" className={styles.toggleLabel}>{uiText("components.GlobalSettingsModal.text027")}</div>
                                        <div data-i18n="components.GlobalSettingsModal.text028" className={styles.toggleDescription}>{uiText("components.GlobalSettingsModal.text028")}</div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${announcementsEnabled ? styles.active : ''}`}
                                        onClick={toggleAnnouncements}
                                    />
                                </div>

                                {/* Media Recall Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div data-i18n="components.GlobalSettingsModal.text029" className={styles.toggleLabel}>{uiText("components.GlobalSettingsModal.text029")}</div>
                                        <div data-i18n="components.GlobalSettingsModal.text030" className={styles.toggleDescription}>{uiText("components.GlobalSettingsModal.text030")}</div>
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
                                            <div data-i18n="components.GlobalSettingsModal.text031" className={styles.toggleLabel}>{uiText("components.GlobalSettingsModal.text031")}</div>
                                            <div data-i18n="components.GlobalSettingsModal.text032" className={styles.toggleDescription}>{uiText("components.GlobalSettingsModal.text032")}</div>
                                        </div>
                                        <div
                                            className={`${styles.toggle} ${geminiAutoCacheEnabled ? styles.active : ''}`}
                                            onClick={toggleGeminiAutoCache}
                                        />
                                    </div>
                                    {geminiAutoCacheEnabled && (
                                        <div className={styles.subSetting}>
                                            <label data-i18n="components.GlobalSettingsModal.text033" className={styles.subSettingLabel} htmlFor="gemini-auto-cache-keep">{uiText("components.GlobalSettingsModal.text033")}</label>
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
                                            <div data-i18n="components.GlobalSettingsModal.text034 components.GlobalSettingsModal.text035" className={styles.subSettingHint}>{uiText("components.GlobalSettingsModal.text034")}{geminiAutoCacheKeepMax}{uiText("components.GlobalSettingsModal.text035")}</div>
                                        </div>
                                    )}
                                </div>

                                {/* Developer Mode Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div data-i18n="components.GlobalSettingsModal.text036" className={styles.toggleLabel}>
                                            <Cpu size={18} />{uiText("components.GlobalSettingsModal.text036")}</div>
                                        <div data-i18n="components.GlobalSettingsModal.text037" className={styles.toggleDescription}>{uiText("components.GlobalSettingsModal.text037")}</div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${developerMode ? styles.active : ''}`}
                                        onClick={toggleDeveloperMode}
                                    />
                                </div>

                                {/* ペルソナに送る量 (全体既定) */}
                                <div className={`${styles.toggleContainer} ${styles.toggleContainerStacked}`}>
                                    <div>
                                        <div data-i18n="components.GlobalSettingsModal.text104" className={styles.toggleLabel}>
                                            <Layers size={18} />
                                            {uiText("components.GlobalSettingsModal.text104")}
                                        </div>
                                        <div data-i18n="components.GlobalSettingsModal.text105" className={styles.toggleDescription}>
                                            {uiText("components.GlobalSettingsModal.text105")}
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
                                                        <span className={styles.wmPresetLabel}>{getWatermarkPresetLabel(preset.id, preset.label)}</span>
                                                        <span data-i18n="components.GlobalSettingsModal.text106" className={styles.wmPresetNums}>
                                                            {uiText("components.GlobalSettingsModal.text106", {
                                                                p1: preset.high.toLocaleString(getFormatLocale()),
                                                                p2: preset.target.toLocaleString(getFormatLocale())
                                                            })}
                                                        </span>
                                                    </button>
                                                ))}
                                                <button
                                                    type="button"
                                                    className={`${styles.wmPresetBtn} ${wmActivePreset == null ? styles.wmPresetBtnActive : ''}`}
                                                    aria-pressed={wmActivePreset == null}
                                                    onClick={() => wmTargetInputRef.current?.focus()}
                                                >
                                                    <span data-i18n="components.GlobalSettingsModal.text107" className={styles.wmPresetLabel}>{uiText("components.GlobalSettingsModal.text107")}</span>
                                                    <span data-i18n="components.GlobalSettingsModal.text108" className={styles.wmPresetNums}>{uiText("components.GlobalSettingsModal.text108")}</span>
                                                </button>
                                            </div>
                                            <div className={styles.wmPresetNote}>
                                                <Info size={14} />
                                                <span data-i18n="components.GlobalSettingsModal.text109">
                                                    {uiText("components.GlobalSettingsModal.text109")}
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
                                                            <span data-i18n="components.GlobalSettingsModal.text044 components.GlobalSettingsModal.text045 components.GlobalSettingsModal.text110" className={`${styles.wmBadge} ${isUser ? styles.wmBadgeUser : ''}`}>
                                                                {isUser
                                                                    ? uiText("components.GlobalSettingsModal.text044")
                                                                    : builtin != null ? uiText("components.GlobalSettingsModal.text045", { p1: builtin.toLocaleString(getFormatLocale()) }) : uiText("components.GlobalSettingsModal.text110")}
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
                                                                placeholder={builtin != null ? builtin.toLocaleString(getFormatLocale()) : ''}
                                                                onChange={e => {
                                                                    const v = e.target.value;
                                                                    setWmInputs(prev => ({ ...prev, [k]: v }));
                                                                }}
                                                                onKeyDown={e => { if (e.key === 'Enter') saveMetabolismDefaults(); }}
                                                            />
                                                            <span data-i18n="components.GlobalSettingsModal.text046" className={styles.wmUnit}>{uiText("components.GlobalSettingsModal.text046")}</span>
                                                            <button data-i18n="components.GlobalSettingsModal.text047"
                                                                type="button"
                                                                className={styles.wmResetBtn}
                                                                disabled={wmInputs[k] === ''}
                                                                onClick={() => setWmInputs(prev => ({ ...prev, [k]: '' }))}
                                                            >{uiText("components.GlobalSettingsModal.text047")}</button>
                                                        </div>
                                                    </div>
                                                );
                                            })}
                                        </div>
                                        {wmViolations.size > 0 && (
                                            <div data-i18n="components.GlobalSettingsModal.text048" className={styles.wmMessageBad}>
                                                {WATERMARK_LABELS.target} ≤ {WATERMARK_LABELS.high} {uiText("components.GlobalSettingsModal.text048")}
                                            </div>
                                        )}
                                    </div>

                                    {(wmHasNaN || wmHasZero) && (
                                        <div data-i18n="components.GlobalSettingsModal.text054" className={styles.wmMessageBad}>
                                            {uiText("components.GlobalSettingsModal.text054")}
                                        </div>
                                    )}
                                    {wmError && <div className={styles.wmMessageBad}>{wmError}</div>}
                                    <div className={styles.wmFooter}>
                                        <span data-i18n="components.GlobalSettingsModal.text055" className={styles.subSettingHint}>
                                            {uiText("components.GlobalSettingsModal.text055")}
                                        </span>
                                        <button data-i18n="components.GlobalSettingsModal.text056 components.GlobalSettingsModal.text057 components.GlobalSettingsModal.text058"
                                            type="button"
                                            className={styles.saveBtn}
                                            disabled={!wmCanSave}
                                            onClick={saveMetabolismDefaults}
                                        >
                                            <Save size={16} /> {wmSaving ? uiText("components.GlobalSettingsModal.text056") : wmSavedAt && !wmDirty ? uiText("components.GlobalSettingsModal.text057") : uiText("components.GlobalSettingsModal.text058")}
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
                                    <h3 data-i18n="components.GlobalSettingsModal.text059">{uiText("components.GlobalSettingsModal.text059")}</h3>
                                </div>

                                {modelRolesLoading ? (
                                    <div data-i18n="components.GlobalSettingsModal.text060">{uiText("components.GlobalSettingsModal.text060")}</div>
                                ) : (
                                    <>
                                        {modelPresets.length > 0 && (
                                            <div className={styles.presetContainer}>
                                                <div data-i18n="components.GlobalSettingsModal.text061" className={styles.presetHeader}>{uiText("components.GlobalSettingsModal.text061")}</div>
                                                <div data-i18n="components.GlobalSettingsModal.text062" className={styles.presetDescription}>{uiText("components.GlobalSettingsModal.text062")}</div>
                                                <div className={styles.presetList}>
                                                    {modelPresets.filter(p => p.is_available).map((preset) => (
                                                        <button
                                                            key={preset.provider}
                                                            className={styles.presetBtn}
                                                            onClick={() => handlePresetApply(preset.provider)}
                                                        >
                                                            {getProviderPresetDisplayName(preset.provider, preset.display_name)}
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
                                                            <span className={styles.roleLabel}>{getModelRoleLabel(role, info.label)}</span>
                                                            <span className={styles.roleDescription}>{getModelRoleDescription(role, info.description)}</span>
                                                        </div>
                                                        <div className={styles.roleValue}>
                                                            <span data-i18n="components.GlobalSettingsModal.text063" className={styles.roleModelName}>
                                                                {info.display_name || info.value || uiText("components.GlobalSettingsModal.text063")}
                                                            </span>
                                                            <button
                                                                className={styles.roleChangeBtn}
                                                                onClick={() => setExpandedModelRole(
                                                                    expandedModelRole === role ? null : role
                                                                )}
                                                            >
                                                                <ChevronDown size={14} />
                                                                <span data-i18n="components.GlobalSettingsModal.text064">{uiText("components.GlobalSettingsModal.text064")}</span>
                                                            </button>
                                                        </div>
                                                    </div>
                                                    {expandedModelRole === role && (
                                                        <div className={styles.roleDropdown}>
                                                            {modelsAvailable
                                                                .filter(m => m.is_available)
                                                                .filter(m => !m.reflex_only || role === REFLEX_JUDGMENT_ROLE)
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
                                    <button data-i18n="components.GlobalSettingsModal.text065"
                                        className={`${styles.subTab} ${modelMgmtSubTab === 'providers' ? styles.subTabActive : ''}`}
                                        onClick={() => setModelMgmtSubTab('providers')}
                                    >{uiText("components.GlobalSettingsModal.text065")}</button>
                                    <button data-i18n="components.GlobalSettingsModal.text066"
                                        className={`${styles.subTab} ${modelMgmtSubTab === 'models' ? styles.subTabActive : ''}`}
                                        onClick={() => setModelMgmtSubTab('models')}
                                    >{uiText("components.GlobalSettingsModal.text066")}</button>
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
                                        <h3 data-i18n="components.GlobalSettingsModal.text067">{uiText("components.GlobalSettingsModal.text067")}</h3>
                                        <p data-i18n="components.GlobalSettingsModal.text068" className={styles.pbSubtitle}>{uiText("components.GlobalSettingsModal.text068")}</p>
                                    </div>
                                </div>

                                {playbookPermsLoading ? (
                                    <div data-i18n="components.GlobalSettingsModal.text069" className={styles.pbEmpty}>
                                        <RefreshCw size={20} style={{ animation: 'spin 1s linear infinite' }} />{uiText("components.GlobalSettingsModal.text069")}</div>
                                ) : playbookPerms.length === 0 ? (
                                    <p data-i18n="components.GlobalSettingsModal.text070" className={styles.pbEmpty}>{uiText("components.GlobalSettingsModal.text070")}</p>
                                ) : (
                                    <div className={styles.pbList}>
                                        {playbookPerms.map(p => {
                                            const dispName = resolveI18nText(p.display_name_i18n, currentLocale, p.display_name_en, p.display_name);
                                            const descText = resolveI18nText(p.description_i18n, currentLocale, p.description_en, p.description);
                                            return (
                                                <div key={p.playbook_name} className={styles.pbItem}>
                                                    <div className={styles.pbItemInfo}>
                                                        <div className={styles.pbItemName}>
                                                            {dispName}
                                                        </div>
                                                        {descText && (
                                                            <div className={styles.pbItemDesc}>
                                                                {descText}
                                                            </div>
                                                        )}
                                                    </div>
                                                    <select
                                                        className={styles.pbSelect}
                                                        value={p.permission_level}
                                                        onChange={e => updatePlaybookPerm(p.playbook_name, e.target.value)}
                                                    >
                                                    <option data-i18n="components.GlobalSettingsModal.text071" value="auto_allow">{uiText("components.GlobalSettingsModal.text071")}</option>
                                                    <option data-i18n="components.GlobalSettingsModal.text072" value="ask_every_time">{uiText("components.GlobalSettingsModal.text072")}</option>
                                                    <option data-i18n="components.GlobalSettingsModal.text073" value="user_only">{uiText("components.GlobalSettingsModal.text073")}</option>
                                                    {p.permission_level === 'blocked' && (
                                                        <option data-i18n="components.GlobalSettingsModal.text074" value="blocked" disabled>{uiText("components.GlobalSettingsModal.text074")}</option>
                                                    )}
                                                </select>
                                            </div>
                                        );
                                    })}
                                    </div>
                                )}
                            </div>
                        )}

                        {activeTab === 'about' && (
                            <div className={styles.aboutContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3 data-i18n="components.GlobalSettingsModal.text075">{uiText("components.GlobalSettingsModal.text075")}</h3>
                                </div>

                                {/* Version */}
                                {versionInfo && (
                                    <div className={styles.aboutCard}>
                                        <div className={styles.aboutVersion}>
                                            v{versionInfo.version}
                                        </div>
                                        {versionInfo.update_available && (
                                            <div data-i18n="components.GlobalSettingsModal.text076 components.GlobalSettingsModal.text077" className={styles.aboutUpdateNotice}>{uiText("components.GlobalSettingsModal.text076")}{versionInfo.latest_version}{uiText("components.GlobalSettingsModal.text077")}</div>
                                        )}
                                    </div>
                                )}

                                {/* Developer */}
                                <div className={styles.aboutCard}>
                                    <div data-i18n="components.GlobalSettingsModal.text078" className={styles.aboutCardTitle}>{uiText("components.GlobalSettingsModal.text078")}</div>
                                    <div className={styles.aboutDeveloper}>
                                        <span data-i18n="components.GlobalSettingsModal.text079">{uiText("components.GlobalSettingsModal.text079")}</span>
                                        <a href="https://x.com/Lize_san_suki" target="_blank" rel="noopener noreferrer" className={styles.aboutLink}>
                                            <ExternalLink size={14} /> {uiText("components.GlobalSettingsModal.label007")}</a>
                                    </div>
                                </div>

                                {/* Links */}
                                <div className={styles.aboutCard}>
                                    <div data-i18n="components.GlobalSettingsModal.text080" className={styles.aboutCardTitle}>{uiText("components.GlobalSettingsModal.text080")}</div>
                                    <div className={styles.aboutLinks}>
                                        <a href="https://saiverse.net/" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>🌐</span>
                                            <div>
                                                <div data-i18n="components.GlobalSettingsModal.text081" className={styles.aboutLinkName}>{uiText("components.GlobalSettingsModal.text081")}</div>
                                                <div className={styles.aboutLinkDesc}>{uiText("components.GlobalSettingsModal.label008")}</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://discord.gg/qMcgEk83Ag" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>💬</span>
                                            <div>
                                                <div data-i18n="components.GlobalSettingsModal.text082" className={styles.aboutLinkName}>{uiText("components.GlobalSettingsModal.text082")}</div>
                                                <div data-i18n="components.GlobalSettingsModal.text083" className={styles.aboutLinkDesc}>{uiText("components.GlobalSettingsModal.text083")}</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://github.com/maha0525/SAIVerse" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>📦</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>{uiText("components.GlobalSettingsModal.label009")}</div>
                                                <div data-i18n="components.GlobalSettingsModal.text084" className={styles.aboutLinkDesc}>{uiText("components.GlobalSettingsModal.text084")}</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://note.com/maha0525/n/n5a63f572be8f" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>📝</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>{uiText("components.GlobalSettingsModal.label010")}</div>
                                                <div data-i18n="components.GlobalSettingsModal.text085" className={styles.aboutLinkDesc}>{uiText("components.GlobalSettingsModal.text085")}</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                    </div>
                                </div>

                                {/* Support */}
                                <div className={styles.aboutCard}>
                                    <div data-i18n="components.GlobalSettingsModal.text086" className={styles.aboutCardTitle}>{uiText("components.GlobalSettingsModal.text086")}</div>
                                    <div data-i18n="components.GlobalSettingsModal.text087" className={styles.aboutSupportText}>{uiText("components.GlobalSettingsModal.text087")}</div>
                                    <div className={styles.aboutSupportItems}>
                                        <a href="https://github.com/sponsors/maha0525" target="_blank" rel="noopener noreferrer" className={styles.aboutSupportItem} style={{ cursor: 'pointer' }}>
                                            <span data-i18n="components.GlobalSettingsModal.text088" className={`${styles.aboutSupportBadge} ${styles.active}`}>{uiText("components.GlobalSettingsModal.text088")}</span>
                                            {uiText("components.GlobalSettingsModal.label011")}<ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a data-i18n="components.GlobalSettingsModal.text090" href="https://note.com/maha0525/n/n5a63f572be8f" target="_blank" rel="noopener noreferrer" className={styles.aboutSupportItem} style={{ cursor: 'pointer' }}>
                                            <span data-i18n="components.GlobalSettingsModal.text089" className={`${styles.aboutSupportBadge} ${styles.active}`}>{uiText("components.GlobalSettingsModal.text089")}</span>{uiText("components.GlobalSettingsModal.text090")}<ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                    </div>
                                </div>
                            </div>
                        )}

                        {activeTab === 'utilities' && (
                            <div className={styles.utilitiesContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3 data-i18n="components.GlobalSettingsModal.text091">{uiText("components.GlobalSettingsModal.text091")}</h3>
                                </div>

                                {/* アイテム概要の一括生成 */}
                                <div className={styles.utilityCard}>
                                    <h4 data-i18n="components.GlobalSettingsModal.text092" className={styles.utilityTitle}>{uiText("components.GlobalSettingsModal.text092")}</h4>
                                    <p data-i18n="components.GlobalSettingsModal.text093" className={styles.utilityDesc}>{uiText("components.GlobalSettingsModal.text093")}</p>

                                    <div className={styles.utilityForm}>
                                        <div className={styles.utilityRow}>
                                            <label data-i18n="components.GlobalSettingsModal.text094">{uiText("components.GlobalSettingsModal.text094")}</label>
                                            <select value={bfBuildingId} onChange={e => { setBfBuildingId(e.target.value); setBfPersonaId(''); }}>
                                                <option data-i18n="components.GlobalSettingsModal.text095" value="">{uiText("components.GlobalSettingsModal.text095")}</option>
                                                {bfBuildings.map(b => (
                                                    <option key={b.id} value={b.id}>{b.name}</option>
                                                ))}
                                            </select>
                                        </div>

                                        {bfBuildingId && (
                                            <div className={styles.utilityRow}>
                                                <label data-i18n="components.GlobalSettingsModal.text096">{uiText("components.GlobalSettingsModal.text096")}</label>
                                                <select value={bfPersonaId} onChange={e => setBfPersonaId(e.target.value)}>
                                                    <option data-i18n="components.GlobalSettingsModal.text097" value="">{uiText("components.GlobalSettingsModal.text097")}</option>
                                                    {bfPersonas.map(p => (
                                                        <option key={p.persona_id} value={p.persona_id}>{p.persona_name}</option>
                                                    ))}
                                                </select>
                                            </div>
                                        )}

                                        <div className={styles.utilityRow}>
                                            <label data-i18n="components.GlobalSettingsModal.text098" className={styles.checkboxLabel}>
                                                <input type="checkbox" checked={bfDryRun} onChange={e => setBfDryRun(e.target.checked)} />{uiText("components.GlobalSettingsModal.text098")}</label>
                                        </div>

                                        <button data-i18n="components.GlobalSettingsModal.text099 components.GlobalSettingsModal.text100"
                                            className={styles.utilityRunBtn}
                                            onClick={runBackfill}
                                            disabled={bfRunning}
                                        >
                                            {bfRunning ? <><Loader size={14} className={styles.spin} />{uiText("components.GlobalSettingsModal.text099")}</> : uiText("components.GlobalSettingsModal.text100")}
                                        </button>
                                    </div>

                                    {bfResults && (
                                        <div className={styles.utilityResults}>
                                            <div className={styles.utilityStats}>
                                                <span data-i18n="components.GlobalSettingsModal.text101" className={styles.statUpdated}>{uiText("components.GlobalSettingsModal.text101")}{bfResults.processed}</span>
                                                <span data-i18n="components.GlobalSettingsModal.text102" className={styles.statSkipped}>{uiText("components.GlobalSettingsModal.text102")}{bfResults.skipped}</span>
                                                <span data-i18n="components.GlobalSettingsModal.text103" className={styles.statFailed}>{uiText("components.GlobalSettingsModal.text103")}{bfResults.failed}</span>
                                                {bfDryRun && <span className={styles.dryRunBadge}>{uiText("components.GlobalSettingsModal.label012")}</span>}
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
