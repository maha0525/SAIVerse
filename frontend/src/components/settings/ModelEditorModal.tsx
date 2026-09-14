
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect } from 'react';
import { X } from 'lucide-react';
import styles from './ModelEditorModal.module.css';
import ModalOverlay from '../common/ModalOverlay';

export type ModelEditorMode = 'create' | 'edit';

interface ProviderChoice {
    id: string;
    display_name: string;
}

export interface ModelCloneSource {
    key: string;
    config: Record<string, unknown>;
}

interface Props {
    isOpen: boolean;
    mode: ModelEditorMode;
    modelKey?: string;  // required when mode='edit'
    cloneSource?: ModelCloneSource;  // pre-fill for create mode (from duplicate)
    onClose: () => void;
    onSaved: () => void;
}

// Fields surfaced as dedicated form inputs. Everything else lives in the JSON editor.
const BASIC_FIELDS = ['model', 'display_name', 'provider_ref', 'context_length'] as const;
const DEFAULT_CONTEXT_LENGTH = 128000;

// Metabolism の水位 (文字数)。キーは三値 — 無し (= 一律既定に従う) / 明示 null
// (= その水位を持たない = Metabolism なし) / 数値。専用欄が**単独所有**し、追加設定
// JSON からは常に除外する (二重所有だと空欄にしても JSON 側の null が復活する —
// Codex 指摘 2026-07-30)。欄の表記: 空欄 = キー無し / "none" = null / 数字 = 数値。
// 旧 metabolism_low_chars (最初に読み込む文字数) は 2026-09-04 廃止 — 専用欄から
// 外れたため、古いモデル JSON に残っているキーは追加設定 JSON 側に現れる
// (backend は黙って無視する)。
// (部屋の様子などの記録の二欄 perception_high_chars / perception_target_chars は
//  2026-09-09 廃止 — docs/intent/presented_context_reduction.md 設計 3。古いモデル
//  JSON に残っているキーは追加設定 JSON 側に現れ、backend は黙って無視する)
const WATERMARK_FIELDS = [
    'metabolism_high_chars', 'metabolism_target_chars',
] as const;
type WatermarkField = typeof WATERMARK_FIELDS[number];
const WATERMARK_LABELS: Record<WatermarkField, { label: string; hint: string }> = {
    metabolism_high_chars: {
        get label() { return uiText("components.settings.ModelEditorModal.text001"); },
        get hint() { return uiText("components.settings.ModelEditorModal.text002"); },
    },
    metabolism_target_chars: {
        get label() { return uiText("components.settings.ModelEditorModal.text003"); },
        get hint() { return uiText("components.settings.ModelEditorModal.text004"); },
    },
};

/** 全欄が空 (= すべて既定に従う) の初期値。欄が増えたときに書き忘れないよう一箇所で作る。 */
const emptyWatermarks = (): Record<WatermarkField, string> =>
    Object.fromEntries(WATERMARK_FIELDS.map(f => [f, ''])) as Record<WatermarkField, string>;

/** 水位欄の値: '' = キー無し (既定) / 'none' = null (持たない) / '数字' = 数値。 */
const watermarkFieldFromConfig = (value: unknown): string => {
    if (value === null) return 'none';
    if (typeof value === 'number') return String(value);
    return '';
};

export default function ModelEditorModal({ isOpen, mode, modelKey, cloneSource, onClose, onSaved }: Props) {
    useLocale();
    const [key, setKey] = useState('');
    // Basic fields (dedicated inputs)
    const [model, setModel] = useState('');
    const [displayName, setDisplayName] = useState('');
    const [providerRef, setProviderRef] = useState('');
    const [contextLength, setContextLength] = useState<number>(DEFAULT_CONTEXT_LENGTH);
    // 水位。'' = キー無し / 'none' = null / '数字' = 数値 (単独所有)
    const [watermarks, setWatermarks] = useState<Record<WatermarkField, string>>(emptyWatermarks);
    // Everything else (JSON editor)
    const [extraJson, setExtraJson] = useState('{}');
    const [providers, setProviders] = useState<ProviderChoice[]>([]);
    const [source, setSource] = useState<string>('user_data');
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [parseError, setParseError] = useState<string | null>(null);
    const [saveError, setSaveError] = useState<string | null>(null);
    // 空欄のモデルが実際に従う既定 (全体設定があればそれ、無ければ組み込み)。
    // GET /api/config/metabolism-defaults の effective。数字はサーバーが持つので
    // 画面には書き写さない — 読み込めるまでは null (欄の説明から数字を伏せる)。
    const [effectiveDefaults, setEffectiveDefaults] = useState<Record<WatermarkField, number> | null>(null);

    const loadEffectiveDefaults = async () => {
        try {
            const res = await apiFetch('/api/config/metabolism-defaults');
            if (!res.ok) return;
            const data = await res.json();
            const eff = data?.effective;
            if (eff && typeof eff.high === 'number' && typeof eff.target === 'number') {
                setEffectiveDefaults({
                    metabolism_high_chars: eff.high,
                    metabolism_target_chars: eff.target,
                });
            }
        } catch (e) {
            console.error('Failed to load watermark defaults', e);
        }
    };

    const applyConfig = (k: string, cfg: Record<string, unknown>) => {
        setKey(k);
        setModel(typeof cfg.model === 'string' ? cfg.model : '');
        setDisplayName(typeof cfg.display_name === 'string' ? cfg.display_name : '');
        setProviderRef(typeof cfg.provider_ref === 'string' ? cfg.provider_ref : '');
        setContextLength(
            typeof cfg.context_length === 'number' ? cfg.context_length : DEFAULT_CONTEXT_LENGTH,
        );
        const wm: Record<WatermarkField, string> = emptyWatermarks();
        const extra: Record<string, unknown> = {};
        for (const [field, value] of Object.entries(cfg)) {
            if ((BASIC_FIELDS as readonly string[]).includes(field)) continue;
            // 水位は専用欄が単独所有 (null も 'none' として欄に写し、JSON には残さない)
            if ((WATERMARK_FIELDS as readonly string[]).includes(field)) {
                wm[field as WatermarkField] = watermarkFieldFromConfig(value);
                continue;
            }
            extra[field] = value;
        }
        setWatermarks(wm);
        setExtraJson(JSON.stringify(extra, null, 2));
        setSource('user_data');
    };

    useEffect(() => {
        if (!isOpen) return;
        setSaveError(null);
        loadProviderList();
        loadEffectiveDefaults();
        if (mode === 'edit' && modelKey) {
            loadModel(modelKey);
        } else if (mode === 'create' && cloneSource) {
            applyConfig(`${cloneSource.key}-copy`, cloneSource.config);
        } else {
            setKey('');
            setModel('');
            setDisplayName('');
            setProviderRef('');
            setContextLength(DEFAULT_CONTEXT_LENGTH);
            setWatermarks(emptyWatermarks());
            setExtraJson('{}');
            setSource('user_data');
        }
    }, [isOpen, mode, modelKey, cloneSource]);

    // Live JSON validation for the extras textarea
    useEffect(() => {
        if (!extraJson.trim()) {
            setParseError(null);
            return;
        }
        try {
            const parsed = JSON.parse(extraJson);
            if (typeof parsed !== 'object' || Array.isArray(parsed) || parsed === null) {
                setParseError(uiText("components.settings.ModelEditorModal.text009"));
                return;
            }
            setParseError(null);
        } catch (e) {
            setParseError(uiText("components.settings.ModelEditorModal.text010", { p1: (e as Error).message }));
        }
    }, [extraJson]);

    const loadProviderList = async () => {
        try {
            const res = await apiFetch('/api/providers');
            if (!res.ok) return;
            const data = await res.json();
            setProviders(
                (data as Array<{ id: string; display_name: string }>).map(p => ({
                    id: p.id,
                    display_name: p.display_name,
                })),
            );
        } catch (e) {
            console.error('Failed to load provider list', e);
        }
    };

    const loadModel = async (k: string) => {
        setLoading(true);
        try {
            const res = await apiFetch(`/api/config/models/${k}`);
            if (!res.ok) {
                setSaveError(uiText("components.settings.ModelEditorModal.text011", { p1: res.status }));
                return;
            }
            const data = await res.json();
            const cfg = (data.config ?? {}) as Record<string, unknown>;
            applyConfig(data.key, cfg);
            setSource(data.source);
        } catch (e) {
            setSaveError(uiText("components.settings.ModelEditorModal.text012", { p1: e }));
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        setSaveError(null);

        if (!key || !key.match(/^[a-zA-Z0-9_.\-]+$/)) {
            setSaveError(uiText("components.settings.ModelEditorModal.text013"));
            return;
        }
        if (!model.trim()) {
            setSaveError(uiText("components.settings.ModelEditorModal.text014"));
            return;
        }
        if (!Number.isFinite(contextLength) || contextLength <= 0) {
            setSaveError(uiText("components.settings.ModelEditorModal.text015"));
            return;
        }

        let extra: Record<string, unknown>;
        try {
            extra = extraJson.trim() ? JSON.parse(extraJson) : {};
            if (typeof extra !== 'object' || Array.isArray(extra) || extra === null) {
                setSaveError(uiText("components.settings.ModelEditorModal.text016"));
                return;
            }
        } catch (e) {
            setSaveError(uiText("components.settings.ModelEditorModal.text017", { p1: (e as Error).message }));
            return;
        }

        // Basic fields override extra (so accidental duplicates in JSON don't shadow the form)
        const merged: Record<string, unknown> = {
            ...extra,
            model: model.trim(),
            context_length: contextLength,
        };
        if (displayName.trim()) {
            merged.display_name = displayName.trim();
        } else {
            delete merged.display_name;
        }
        if (providerRef) {
            merged.provider_ref = providerRef;
        } else {
            delete merged.provider_ref;
        }
        // 水位: 専用欄が単独所有 — JSON に紛れた同名キーは欄の値で常に上書きする。
        // 空欄 = キーを書かない (一律既定) / "none" = null (持たない) / 数字 = 数値。
        // 検査は**実効値**で行う (空欄は全体設定の既定で埋める) — サーバー側
        // (api/routes/config.py の _watermark_constraints_error) と同じ数え方。
        const wmEffective: Record<WatermarkField, number | null> = {
            metabolism_high_chars: effectiveDefaults?.metabolism_high_chars ?? null,
            metabolism_target_chars: effectiveDefaults?.metabolism_target_chars ?? null,
        };
        for (const field of WATERMARK_FIELDS) {
            const raw = watermarks[field].trim();
            delete merged[field];
            if (raw === '') continue;
            if (raw.toLowerCase() === 'none') {
                merged[field] = null;
                wmEffective[field] = null;
                continue;
            }
            const value = parseInt(raw, 10);
            if (isNaN(value) || String(value) !== raw || value < 1) {
                setSaveError(uiText("components.settings.ModelEditorModal.text018", { p1: field }));
                return;
            }
            merged[field] = value;
            wmEffective[field] = value;
        }
        const wmHigh = wmEffective.metabolism_high_chars;
        const wmTarget = wmEffective.metabolism_target_chars;
        if (wmTarget != null && wmHigh != null && wmTarget > wmHigh) {
            setSaveError(uiText("components.settings.ModelEditorModal.text019"));
            return;
        }

        setSaving(true);
        try {
            let res: Response;
            if (mode === 'create') {
                res = await apiFetch('/api/config/models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key, config: merged }),
                });
            } else {
                res = await apiFetch(`/api/config/models/${key}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: merged }),
                });
            }
            if (!res.ok) {
                const text = await res.text();
                setSaveError(uiText("components.settings.ModelEditorModal.text025", { p1: res.status, p2: text }));
                return;
            }
            // 保存で決め直したときに、新しい設定に切り替えられなかったペルソナの知らせ
            const data = await res.json().catch(() => null);
            const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
            if (notices.length > 0) {
                alert(notices.join('\n\n'));
            }
            onSaved();
            onClose();
        } catch (e) {
            setSaveError(uiText("components.settings.ModelEditorModal.text026", { p1: e }));
        } finally {
            setSaving(false);
        }
    };

    if (!isOpen) return null;

    const isShadowingNonUser = mode === 'edit' && source !== 'user_data';

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h3 data-i18n="components.settings.ModelEditorModal.text027 components.settings.ModelEditorModal.text028 components.settings.ModelEditorModal.text029">{mode === 'create' ? (cloneSource ? uiText("components.settings.ModelEditorModal.text027") : uiText("components.settings.ModelEditorModal.text028")) : uiText("components.settings.ModelEditorModal.text029", { p1: key })}</h3>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div data-i18n="components.settings.ModelEditorModal.text030">{uiText("components.settings.ModelEditorModal.text030")}</div>
                    ) : (
                        <>
                            {isShadowingNonUser && (
                                <div data-i18n="components.settings.ModelEditorModal.text031 components.settings.ModelEditorModal.text032" className={styles.warningBanner}>
                                    {source}{uiText("components.settings.ModelEditorModal.text031")}{source}{uiText("components.settings.ModelEditorModal.text032")}</div>
                            )}

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ModelEditorModal.text033">{uiText("components.settings.ModelEditorModal.text033")}</label>
                                <input data-i18n="components.settings.ModelEditorModal.text034"
                                    className={styles.input}
                                    type="text"
                                    value={key}
                                    onChange={e => setKey(e.target.value)}
                                    placeholder={uiText("components.settings.ModelEditorModal.text034")}
                                    disabled={mode === 'edit'}
                                />
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ModelEditorModal.text035">{uiText("components.settings.ModelEditorModal.text035")}</label>
                                <input data-i18n="components.settings.ModelEditorModal.text036"
                                    className={styles.input}
                                    type="text"
                                    value={model}
                                    onChange={e => setModel(e.target.value)}
                                    placeholder={uiText("components.settings.ModelEditorModal.text036")}
                                />
                                <span data-i18n="components.settings.ModelEditorModal.text037" className={styles.hint}>{uiText("components.settings.ModelEditorModal.text037")}</span>
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ModelEditorModal.text038">{uiText("components.settings.ModelEditorModal.text038")}</label>
                                <input data-i18n="components.settings.ModelEditorModal.text039"
                                    className={styles.input}
                                    type="text"
                                    value={displayName}
                                    onChange={e => setDisplayName(e.target.value)}
                                    placeholder={uiText("components.settings.ModelEditorModal.text039")}
                                />
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ModelEditorModal.text040">{uiText("components.settings.ModelEditorModal.text040")}</label>
                                <select
                                    className={styles.input}
                                    value={providerRef}
                                    onChange={e => setProviderRef(e.target.value)}
                                >
                                    <option data-i18n="components.settings.ModelEditorModal.text041" value="">{uiText("components.settings.ModelEditorModal.text041")}</option>
                                    {providers.map(p => (
                                        <option key={p.id} value={p.id}>
                                            {p.display_name} ({p.id})
                                        </option>
                                    ))}
                                </select>
                                <span data-i18n="components.settings.ModelEditorModal.text042" className={styles.hint}>{uiText("components.settings.ModelEditorModal.text042")}</span>
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ModelEditorModal.text043">{uiText("components.settings.ModelEditorModal.text043")}</label>
                                <input
                                    className={styles.input}
                                    type="number"
                                    value={contextLength}
                                    onChange={e => setContextLength(parseInt(e.target.value, 10) || 0)}
                                    min={1}
                                    step={1}
                                />
                            </div>

                            {WATERMARK_FIELDS.map(field => {
                                const fallback = effectiveDefaults?.[field] ?? null;
                                return (
                                    <div className={styles.field} key={field}>
                                        <label>{WATERMARK_LABELS[field].label}</label>
                                        <input
                                            className={styles.input}
                                            type="text"
                                            inputMode="numeric"
                                            value={watermarks[field]}
                                            onChange={e => {
                                                const v = e.target.value;
                                                setWatermarks(prev => ({ ...prev, [field]: v }));
                                            }}
                                            placeholder={
                                                fallback != null
                                                    ? `空欄 = 全体設定の既定 (${fallback.toLocaleString()} 字) に従う / none = 使わない`
                                                    : '空欄 = 全体設定の既定に従う / none = 使わない'
                                            }
                                        />
                                        <span className={styles.hint}>
                                            {WATERMARK_LABELS[field].hint}
                                            {' '}空欄のときは全体設定の既定
                                            {fallback != null ? ` ${fallback.toLocaleString()} 字` : ''}
                                            に従います（全体設定 → 環境タブ「ペルソナに送る量」）。
                                        </span>
                                    </div>
                                );
                            })}

                            <div className={styles.field}>
                                <label>
                                    <span data-i18n="components.settings.ModelEditorModal.text047">{uiText("components.settings.ModelEditorModal.text047")}</span>
                                    {parseError && <span className={styles.parseError}>{parseError}</span>}
                                </label>
                                <textarea
                                    className={`${styles.textarea} ${parseError ? styles.textareaError : ''}`}
                                    value={extraJson}
                                    onChange={e => setExtraJson(e.target.value)}
                                    rows={12}
                                    spellCheck={false}
                                />
                                <span data-i18n="components.settings.ModelEditorModal.text048 components.settings.ModelEditorModal.text049" className={styles.hint}>{uiText("components.settings.ModelEditorModal.text048")}{'{}'}{uiText("components.settings.ModelEditorModal.text049")}</span>
                            </div>

                            {saveError && <div className={styles.error}>{saveError}</div>}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button data-i18n="components.settings.ModelEditorModal.text050" className={styles.cancelBtn} onClick={onClose}>{uiText("components.settings.ModelEditorModal.text050")}</button>
                    <button data-i18n="components.settings.ModelEditorModal.text051 components.settings.ModelEditorModal.text052"
                        className={styles.saveBtn}
                        onClick={handleSave}
                        disabled={saving || loading || !!parseError}
                    >
                        {saving ? uiText("components.settings.ModelEditorModal.text051") : uiText("components.settings.ModelEditorModal.text052")}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
