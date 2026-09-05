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
const WATERMARK_FIELDS = [
    'metabolism_high_chars', 'metabolism_target_chars',
    'perception_high_chars', 'perception_target_chars',
] as const;
type WatermarkField = typeof WATERMARK_FIELDS[number];
const WATERMARK_LABELS: Record<WatermarkField, { label: string; hint: string }> = {
    metabolism_high_chars: {
        label: '整理をはじめる文字数 (metabolism_high_chars)',
        hint: '会話コンテキストがこの文字数を超えたら、古い出来事からあらすじへ畳んで整理します。none にすると文字数では発火しません。',
    },
    metabolism_target_chars: {
        label: '整理後に残す文字数 (metabolism_target_chars)',
        hint: '整理はこの文字数まで畳んだら止まります。少なすぎるときは畳んだ範囲をここまで開き直します。会話の起点がまだ無いとき（新規ペルソナ等）に最初に読み込む量もこの値です。none にするとこのモデルは履歴の自動整理を行いません。',
    },
    perception_high_chars: {
        label: '部屋の様子などの記録の省略をはじめる文字数 (perception_high_chars)',
        hint: '移動したときの部屋の様子や、使えるスペルが増えた・減ったといった記録の合計がこの文字数を超えたら、古いものからまとめて省略します（省略されるのは送る内容からだけで、記録そのものは消えません）。none にすると省略せず、合計は伸びるに任せます。',
    },
    perception_target_chars: {
        label: '部屋の様子などの記録を省略した後に残す文字数 (perception_target_chars)',
        hint: '一度の省略でここまでまとめて減らします。一個ずつ減らさないのは、送る内容の前の方が毎回書き換わるとキャッシュが効かなくなるためです。none にすると全体設定の既定に従います。',
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
    // GET /api/config/metabolism-defaults の effective。取れないときは組み込みの数字。
    const [effectiveDefaults, setEffectiveDefaults] = useState<Record<WatermarkField, number>>({
        metabolism_high_chars: 120000,
        metabolism_target_chars: 40000,
        perception_high_chars: 60000,
        perception_target_chars: 40000,
    });
    // 「整理をはじめる量 − 残す量 > 記録の上限 + 余裕」の余裕の分 (サーバーの値)。
    const [headroom, setHeadroom] = useState(10000);

    const loadEffectiveDefaults = async () => {
        try {
            const res = await fetch('/api/config/metabolism-defaults');
            if (!res.ok) return;
            const data = await res.json();
            const eff = data?.effective;
            if (eff && typeof eff.high === 'number' && typeof eff.target === 'number') {
                setEffectiveDefaults(prev => ({
                    ...prev,
                    metabolism_high_chars: eff.high,
                    metabolism_target_chars: eff.target,
                    ...(typeof eff.perception_high === 'number' ? { perception_high_chars: eff.perception_high } : {}),
                    ...(typeof eff.perception_target === 'number' ? { perception_target_chars: eff.perception_target } : {}),
                }));
            }
            if (typeof data?.headroom === 'number') setHeadroom(data.headroom);
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
                setParseError('追加設定はオブジェクト ({}) で指定してください');
                return;
            }
            setParseError(null);
        } catch (e) {
            setParseError(`JSON エラー: ${(e as Error).message}`);
        }
    }, [extraJson]);

    const loadProviderList = async () => {
        try {
            const res = await fetch('/api/providers');
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
            const res = await fetch(`/api/config/models/${k}`);
            if (!res.ok) {
                setSaveError(`読み込み失敗: HTTP ${res.status}`);
                return;
            }
            const data = await res.json();
            const cfg = (data.config ?? {}) as Record<string, unknown>;
            applyConfig(data.key, cfg);
            setSource(data.source);
        } catch (e) {
            setSaveError(`読み込み失敗: ${e}`);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        setSaveError(null);

        if (!key || !key.match(/^[a-zA-Z0-9_.\-]+$/)) {
            setSaveError('キーは英数字・ハイフン・アンダースコア・ドットのみ使用可能です');
            return;
        }
        if (!model.trim()) {
            setSaveError('モデル ID (model) を入力してください');
            return;
        }
        if (!Number.isFinite(contextLength) || contextLength <= 0) {
            setSaveError('context_length は正の整数を入力してください');
            return;
        }

        let extra: Record<string, unknown>;
        try {
            extra = extraJson.trim() ? JSON.parse(extraJson) : {};
            if (typeof extra !== 'object' || Array.isArray(extra) || extra === null) {
                setSaveError('追加設定はオブジェクトで指定してください');
                return;
            }
        } catch (e) {
            setSaveError(`追加設定の JSON が不正です: ${(e as Error).message}`);
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
        const wmEffective: Record<WatermarkField, number | null> = { ...effectiveDefaults };
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
                setSaveError(`${field} は 1 以上の整数か none を入力してください（空欄 = 全体設定の既定）`);
                return;
            }
            merged[field] = value;
            wmEffective[field] = value;
        }
        const wmHigh = wmEffective.metabolism_high_chars;
        const wmTarget = wmEffective.metabolism_target_chars;
        const pwHigh = wmEffective.perception_high_chars;
        const pwTarget = wmEffective.perception_target_chars;
        if (wmTarget != null && wmHigh != null && wmTarget > wmHigh) {
            setSaveError('整理後に残す文字数は、整理をはじめる文字数以下にしてください');
            return;
        }
        if (pwTarget != null && pwHigh != null && pwTarget > pwHigh) {
            setSaveError('部屋の様子などの記録を省略した後に残す文字数は、省略をはじめる文字数以下にしてください');
            return;
        }
        if (wmTarget != null && wmHigh != null && pwHigh != null && !(wmHigh - wmTarget > pwHigh + headroom)) {
            setSaveError(
                `整理をはじめる文字数 (${wmHigh.toLocaleString()}) と整理後に残す文字数 (${wmTarget.toLocaleString()}) の差 `
                + `${(wmHigh - wmTarget).toLocaleString()} 字が、部屋の様子などの記録の上限 ${pwHigh.toLocaleString()} 字 + 余裕 `
                + `${headroom.toLocaleString()} 字 を上回っていません。このままだと会話をどれだけ整理しても、`
                + '送る量が上限を下回らないことがあります（空欄の欄は全体設定の既定で数えています）',
            );
            return;
        }

        setSaving(true);
        try {
            let res: Response;
            if (mode === 'create') {
                res = await fetch('/api/config/models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key, config: merged }),
                });
            } else {
                res = await fetch(`/api/config/models/${key}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: merged }),
                });
            }
            if (!res.ok) {
                const text = await res.text();
                setSaveError(`保存失敗: HTTP ${res.status} ${text}`);
                return;
            }
            onSaved();
            onClose();
        } catch (e) {
            setSaveError(`保存失敗: ${e}`);
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
                    <h3>{mode === 'create' ? (cloneSource ? 'モデルを複製' : 'モデルを新規作成') : `モデルを編集: ${key}`}</h3>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div>読み込み中...</div>
                    ) : (
                        <>
                            {isShadowingNonUser && (
                                <div className={styles.warningBanner}>
                                    {source} のモデルを編集中です。保存すると user_data に上書きが作成され、{source} は変更されません。リセットしたい場合は user_data 側のファイルを削除してください。
                                </div>
                            )}

                            <div className={styles.field}>
                                <label>キー（ファイル名）</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={key}
                                    onChange={e => setKey(e.target.value)}
                                    placeholder="例: qwen-via-lmstudio"
                                    disabled={mode === 'edit'}
                                />
                            </div>

                            <div className={styles.field}>
                                <label>モデル ID (model)</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={model}
                                    onChange={e => setModel(e.target.value)}
                                    placeholder="例: qwen2.5-72b-instruct"
                                />
                                <span className={styles.hint}>API 呼び出しに使うモデル名（プロバイダ側のモデル ID）</span>
                            </div>

                            <div className={styles.field}>
                                <label>表示名 (display_name)</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={displayName}
                                    onChange={e => setDisplayName(e.target.value)}
                                    placeholder="例: Qwen 2.5 72B (LM Studio)"
                                />
                            </div>

                            <div className={styles.field}>
                                <label>プロバイダ参照 (provider_ref)</label>
                                <select
                                    className={styles.input}
                                    value={providerRef}
                                    onChange={e => setProviderRef(e.target.value)}
                                >
                                    <option value="">（参照なし — provider/base_url を直接指定する場合）</option>
                                    {providers.map(p => (
                                        <option key={p.id} value={p.id}>
                                            {p.display_name} ({p.id})
                                        </option>
                                    ))}
                                </select>
                                <span className={styles.hint}>
                                    プロバイダを選ぶと base_url / api_key_env がプロバイダ側から自動継承されます
                                </span>
                            </div>

                            <div className={styles.field}>
                                <label>コンテキスト長 (context_length)</label>
                                <input
                                    className={styles.input}
                                    type="number"
                                    value={contextLength}
                                    onChange={e => setContextLength(parseInt(e.target.value, 10) || 0)}
                                    min={1}
                                    step={1}
                                />
                            </div>

                            {WATERMARK_FIELDS.map(field => (
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
                                        placeholder={`空欄 = 全体設定の既定 (${effectiveDefaults[field].toLocaleString()} 字) に従う / none = 使わない`}
                                    />
                                    <span className={styles.hint}>
                                        {WATERMARK_LABELS[field].hint}
                                        {' '}空欄のときは全体設定の既定 {effectiveDefaults[field].toLocaleString()} 字に従います（全体設定 → 環境タブ「ペルソナに送る量の水位」）。
                                    </span>
                                </div>
                            ))}

                            <div className={styles.field}>
                                <label>
                                    <span>追加設定 (JSON)</span>
                                    {parseError && <span className={styles.parseError}>{parseError}</span>}
                                </label>
                                <textarea
                                    className={`${styles.textarea} ${parseError ? styles.textareaError : ''}`}
                                    value={extraJson}
                                    onChange={e => setExtraJson(e.target.value)}
                                    rows={12}
                                    spellCheck={false}
                                />
                                <span className={styles.hint}>
                                    parameters / pricing / cache / supports_images など、上記以外のフィールドを JSON で指定。
                                    空 ({'{}'}) で問題ない場合も多い。詳細スキーマは builtin_data/models/ の例を参照。
                                </span>
                            </div>

                            {saveError && <div className={styles.error}>{saveError}</div>}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>キャンセル</button>
                    <button
                        className={styles.saveBtn}
                        onClick={handleSave}
                        disabled={saving || loading || !!parseError}
                    >
                        {saving ? '保存中...' : '保存'}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
