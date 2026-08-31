'use client';

import { useState, useEffect, useCallback } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import styles from './ActionsPanel.module.css';

interface ToolCallDef {
    tool: string;
    args: Record<string, unknown>;
}

interface StepDef {
    type: 'parallel' | 'wait';
    calls?: ToolCallDef[];
    duration_ms?: number | string;
}

interface ParamSpec {
    type: 'number' | 'integer' | 'string' | 'boolean';
    default?: unknown;
    min?: number;
    max?: number;
    description?: string;
}

interface ActionDef {
    id: string;
    display_name: string;
    description: string;
    parameters?: Record<string, ParamSpec>;
    steps: StepDef[];
}

const PARAM_TYPES: ParamSpec['type'][] = ['number', 'integer', 'string', 'boolean'];

interface JsonSchemaProp {
    type?: string;
    description?: string;
    enum?: unknown[];
    default?: unknown;
}

interface ToolSchema {
    name: string;
    description: string;
    parameters: {
        type?: string;
        properties?: Record<string, JsonSchemaProp>;
        required?: string[];
    };
}

// 引数の既定値入力を型に合わせて変換する。空欄は undefined。
function coerceParamValue(type: ParamSpec['type'], raw: string): unknown {
    if (raw === '') return undefined;
    if (type === 'integer') {
        const n = parseInt(raw, 10);
        return Number.isNaN(n) ? raw : n;
    }
    if (type === 'number') {
        const n = Number(raw);
        return Number.isNaN(n) ? raw : n;
    }
    if (type === 'boolean') return raw === 'true' || raw === '1';
    return raw;
}

// 待機時間の入力: 数値ならそのまま number、${...} 等の文字列はそのまま保持。
function parseDurationInput(raw: string): number | string {
    const trimmed = raw.trim();
    if (/^\d+$/.test(trimmed)) return Number(trimmed);
    return raw;
}

interface TestTarget {
    value: string;
    label: string;
}

interface ActionsPanelProps {
    addonName: string;
}

const API_BASE = '/api/addon';

function emptyAction(): ActionDef {
    return {
        id: '',
        display_name: '',
        description: '',
        steps: [{ type: 'parallel', calls: [{ tool: '', args: {} }] }],
    };
}

export default function ActionsPanel({ addonName }: ActionsPanelProps) {
    const [collapsed, setCollapsed] = useState(true);
    const [actions, setActions] = useState<ActionDef[]>([]);
    const [availableTools, setAvailableTools] = useState<string[]>([]);
    const [toolSchemas, setToolSchemas] = useState<Record<string, ToolSchema>>({});
    // 複数機体アドオンでのテスト実行先 (機体) 候補。空なら従来通り機体を選ばず
    // テストする (単一接続の従来型アドオン)。
    const [testTargets, setTestTargets] = useState<TestTarget[]>([]);
    const [selectedTarget, setSelectedTarget] = useState<string>('');
    const [editing, setEditing] = useState<ActionDef | null>(null);
    const [isNew, setIsNew] = useState(false);
    const [testResult, setTestResult] = useState<{ ok: boolean; text: string } | null>(null);
    const [busy, setBusy] = useState(false);

    const fetchActions = useCallback(async () => {
        try {
            const res = await fetch(`${API_BASE}/${addonName}/actions`);
            if (res.ok) setActions(await res.json());
        } catch { /* ignore */ }
    }, [addonName]);

    const fetchTools = useCallback(async () => {
        try {
            const res = await fetch(`${API_BASE}/${addonName}/actions/tool-schemas`);
            if (res.ok) {
                const list: ToolSchema[] = await res.json();
                setAvailableTools(list.map(t => t.name));
                setToolSchemas(Object.fromEntries(list.map(t => [t.name, t])));
            }
        } catch { /* ignore */ }
    }, [addonName]);

    useEffect(() => {
        fetchActions();
    }, [fetchActions]);

    const fetchTestTargets = useCallback(async () => {
        try {
            const res = await fetch(`${API_BASE}/${addonName}/actions/test-targets`);
            if (res.ok) {
                const list: TestTarget[] = await res.json();
                setTestTargets(list);
                // 機体が居れば先頭を既定選択にする (デバイス操作 UI と同じ挙動)。
                // 既選択が候補から消えていたら選び直す。
                setSelectedTarget(prev =>
                    list.some(t => t.value === prev)
                        ? prev
                        : (list[0]?.value ?? ''));
            }
        } catch { /* ignore */ }
    }, [addonName]);

    useEffect(() => {
        if (!collapsed) {
            fetchTools();
            fetchTestTargets();
        }
    }, [collapsed, fetchTools, fetchTestTargets]);

    const handleDelete = async (actionId: string) => {
        if (!confirm(`アクション「${actionId}」を削除しますか？`)) return;
        try {
            const res = await fetch(`${API_BASE}/${addonName}/actions/${actionId}`, { method: 'DELETE' });
            if (!res.ok) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.detail || `Delete failed: ${res.status}`);
            }
            fetchActions();
        } catch (e) {
            setTestResult({ ok: false, text: String(e) });
        }
    };

    const handleTest = async (action: ActionDef) => {
        // 機体候補があるのに未選択ならテストしない (どの機体で撃つか曖昧なため)。
        if (testTargets.length > 0 && !selectedTarget) {
            setTestResult({ ok: false, text: 'テスト対象の機体を選んでください。' });
            return;
        }
        setBusy(true);
        setTestResult(null);
        try {
            const url = selectedTarget
                ? `${API_BASE}/${addonName}/actions/test?instance_id=${encodeURIComponent(selectedTarget)}`
                : `${API_BASE}/${addonName}/actions/test`;
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(action),
            });
            const data = await res.json();
            if (res.ok) {
                setTestResult({ ok: true, text: data.result || 'OK' });
            } else {
                setTestResult({ ok: false, text: data.detail || `Error ${res.status}` });
            }
        } catch (e) {
            setTestResult({ ok: false, text: String(e) });
        } finally {
            setBusy(false);
        }
    };

    const handleSave = async () => {
        if (!editing) return;
        setBusy(true);
        try {
            const url = isNew
                ? `${API_BASE}/${addonName}/actions`
                : `${API_BASE}/${addonName}/actions/${editing.id}`;
            const method = isNew ? 'POST' : 'PUT';
            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(editing),
            });
            if (res.ok) {
                setEditing(null);
                setTestResult(null);
                fetchActions();
            } else {
                const data = await res.json();
                setTestResult({ ok: false, text: data.detail || `Error ${res.status}` });
            }
        } catch (e) {
            setTestResult({ ok: false, text: String(e) });
        } finally {
            setBusy(false);
        }
    };

    const openNew = () => {
        setEditing(emptyAction());
        setIsNew(true);
        setTestResult(null);
    };

    const openEdit = (a: ActionDef) => {
        setEditing(JSON.parse(JSON.stringify(a)));
        setIsNew(false);
        setTestResult(null);
    };

    return (
        <div className={styles.container}>
            <button className={styles.sectionHeader} onClick={() => setCollapsed(c => !c)}>
                {collapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
                <span className={styles.headerLabel}>
                    複合アクション ({actions.length})
                </span>
            </button>
            {!collapsed && (
                <div className={styles.body}>
                    <TestTargetSelector
                        targets={testTargets}
                        selected={selectedTarget}
                        onSelect={setSelectedTarget}
                    />
                    {actions.length === 0 ? (
                        <p className={styles.emptyMessage}>アクションが定義されていません</p>
                    ) : (
                        <div className={styles.actionList}>
                            {actions.map(a => (
                                <div key={a.id} className={styles.actionCard}>
                                    <div className={styles.actionInfo}>
                                        <div className={styles.actionName}>{a.display_name}</div>
                                        {a.description && (
                                            <div className={styles.actionDesc}>{a.description}</div>
                                        )}
                                        {a.parameters && Object.keys(a.parameters).length > 0 && (
                                            <div className={styles.actionParams}>
                                                {Object.entries(a.parameters).map(([name, spec]) => (
                                                    <span key={name} className={styles.paramChip}>
                                                        {name}
                                                        {spec.default !== undefined && (
                                                            <span className={styles.paramChipDefault}>={String(spec.default)}</span>
                                                        )}
                                                    </span>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                    <div className={styles.actionButtons}>
                                        <button
                                            className={styles.iconBtn}
                                            onClick={() => handleTest(a)}
                                            disabled={busy}
                                            title="テスト実行"
                                        >
                                            &#9654;
                                        </button>
                                        <button
                                            className={styles.iconBtn}
                                            onClick={() => openEdit(a)}
                                            title="編集"
                                        >
                                            &#9998;
                                        </button>
                                        <button
                                            className={styles.iconBtnDanger}
                                            onClick={() => handleDelete(a.id)}
                                            title="削除"
                                        >
                                            &times;
                                        </button>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                    <button className={styles.addBtn} onClick={openNew}>
                        + 新しいアクションを作成
                    </button>
                </div>
            )}

            {editing && (
                <ActionEditor
                    action={editing}
                    isNew={isNew}
                    availableTools={availableTools}
                    toolSchemas={toolSchemas}
                    testResult={testResult}
                    busy={busy}
                    testTargets={testTargets}
                    selectedTarget={selectedTarget}
                    onSelectTarget={setSelectedTarget}
                    onChange={setEditing}
                    onSave={handleSave}
                    onTest={() => handleTest(editing)}
                    onClose={() => { setEditing(null); setTestResult(null); }}
                />
            )}
        </div>
    );
}


// 複数機体アドオンのテスト実行先セレクタ。候補が無ければ何も描画しない
// (= 単一接続の従来型アドオンでは従来通り機体を選ばずテストする)。
function TestTargetSelector({
    targets, selected, onSelect,
}: {
    targets: TestTarget[];
    selected: string;
    onSelect: (value: string) => void;
}) {
    if (targets.length === 0) return null;
    return (
        <div className={styles.testTargetRow}>
            <span className={styles.testTargetLabel}>テスト対象の機体</span>
            <select
                className={styles.testTargetSelect}
                value={selected}
                onChange={e => onSelect(e.target.value)}
            >
                {targets.map(t => (
                    <option key={t.value} value={t.value}>{t.label}</option>
                ))}
            </select>
        </div>
    );
}


interface ActionEditorProps {
    action: ActionDef;
    isNew: boolean;
    availableTools: string[];
    toolSchemas: Record<string, ToolSchema>;
    testResult: { ok: boolean; text: string } | null;
    busy: boolean;
    testTargets: TestTarget[];
    selectedTarget: string;
    onSelectTarget: (value: string) => void;
    onChange: (a: ActionDef) => void;
    onSave: () => void;
    onTest: () => void;
    onClose: () => void;
}

function ActionEditor({
    action, isNew, availableTools, toolSchemas, testResult, busy,
    testTargets, selectedTarget, onSelectTarget,
    onChange, onSave, onTest, onClose,
}: ActionEditorProps) {

    const update = (patch: Partial<ActionDef>) => {
        onChange({ ...action, ...patch });
    };

    const updateStep = (idx: number, patch: Partial<StepDef>) => {
        const steps = [...action.steps];
        steps[idx] = { ...steps[idx], ...patch };
        update({ steps });
    };

    const removeStep = (idx: number) => {
        update({ steps: action.steps.filter((_, i) => i !== idx) });
    };

    const moveStep = (idx: number, dir: -1 | 1) => {
        const newIdx = idx + dir;
        if (newIdx < 0 || newIdx >= action.steps.length) return;
        const steps = [...action.steps];
        [steps[idx], steps[newIdx]] = [steps[newIdx], steps[idx]];
        update({ steps });
    };

    const addStep = (type: 'parallel' | 'wait') => {
        const step: StepDef = type === 'parallel'
            ? { type: 'parallel', calls: [{ tool: '', args: {} }] }
            : { type: 'wait', duration_ms: 300 };
        update({ steps: [...action.steps, step] });
    };

    const updateCall = (stepIdx: number, callIdx: number, patch: Partial<ToolCallDef>) => {
        const steps = [...action.steps];
        const calls = [...(steps[stepIdx].calls || [])];
        calls[callIdx] = { ...calls[callIdx], ...patch };
        steps[stepIdx] = { ...steps[stepIdx], calls };
        update({ steps });
    };

    const removeCall = (stepIdx: number, callIdx: number) => {
        const steps = [...action.steps];
        const calls = (steps[stepIdx].calls || []).filter((_, i) => i !== callIdx);
        steps[stepIdx] = { ...steps[stepIdx], calls };
        update({ steps });
    };

    const addCall = (stepIdx: number) => {
        const steps = [...action.steps];
        const calls = [...(steps[stepIdx].calls || []), { tool: '', args: {} }];
        steps[stepIdx] = { ...steps[stepIdx], calls };
        update({ steps });
    };

    // --- parameters ---
    const paramEntries = Object.entries(action.parameters || {});
    const paramNames = paramEntries.map(([name]) => name);

    const setParams = (entries: [string, ParamSpec][]) => {
        update({ parameters: Object.fromEntries(entries) });
    };

    const addParam = () => {
        const base = 'param';
        let name = base;
        let i = 1;
        const existing = new Set(paramNames);
        while (existing.has(name)) { name = `${base}${i++}`; }
        setParams([...paramEntries, [name, { type: 'integer', default: 0 }]]);
    };

    const renameParam = (idx: number, newName: string) => {
        const entries = paramEntries.map(([n, s], i): [string, ParamSpec] =>
            i === idx ? [newName, s] : [n, s]);
        setParams(entries);
    };

    const updateParam = (idx: number, patch: Partial<ParamSpec>) => {
        const entries = paramEntries.map(([n, s], i): [string, ParamSpec] =>
            i === idx ? [n, { ...s, ...patch }] : [n, s]);
        setParams(entries);
    };

    const removeParam = (idx: number) => {
        setParams(paramEntries.filter((_, i) => i !== idx));
    };

    return (
        <div className={styles.editorOverlay} onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
            <div className={styles.editor}>
                <div className={styles.editorHeader}>
                    <h3>{isNew ? '新しいアクション' : `編集: ${action.display_name}`}</h3>
                    <button className={styles.iconBtn} onClick={onClose}>&times;</button>
                </div>
                <div className={styles.editorBody}>
                    <div className={styles.fieldGroup}>
                        <label className={styles.fieldLabel}>ID (英小文字・数字・アンダースコア)</label>
                        <input
                            className={styles.fieldInput}
                            value={action.id}
                            onChange={e => update({ id: e.target.value })}
                            placeholder="nod"
                            disabled={!isNew}
                        />
                    </div>
                    <div className={styles.fieldGroup}>
                        <label className={styles.fieldLabel}>表示名</label>
                        <input
                            className={styles.fieldInput}
                            value={action.display_name}
                            onChange={e => update({ display_name: e.target.value })}
                            placeholder="うなずき"
                        />
                    </div>
                    <div className={styles.fieldGroup}>
                        <label className={styles.fieldLabel}>説明</label>
                        <input
                            className={styles.fieldInput}
                            value={action.description}
                            onChange={e => update({ description: e.target.value })}
                            placeholder="首を上下に動かしてうなずく"
                        />
                    </div>

                    <div className={styles.stepsHeader}>
                        <span className={styles.stepsTitle}>引数 ({paramEntries.length})</span>
                    </div>
                    <p className={styles.paramHint}>
                        呼び出し時に変えられる値。ステップの値に <code>{'${名前}'}</code> と書くと差し込まれます。<code>{'${名前}*1000'}</code> のような式（四則・累乗・括弧）も使えます。
                    </p>
                    {paramEntries.map(([name, spec], pi) => (
                        <div key={pi} className={styles.paramCard}>
                            <div className={styles.paramRow}>
                                <input
                                    className={styles.paramNameInput}
                                    value={name}
                                    onChange={e => renameParam(pi, e.target.value)}
                                    placeholder="duration_ms"
                                />
                                <select
                                    className={styles.paramTypeSelect}
                                    value={spec.type}
                                    onChange={e => updateParam(pi, { type: e.target.value as ParamSpec['type'] })}
                                >
                                    {PARAM_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                                </select>
                                <button
                                    className={styles.stepSmallBtn}
                                    onClick={() => removeParam(pi)}
                                    title="この引数を削除"
                                    style={{ color: '#dc2626' }}
                                >&times;</button>
                            </div>
                            <div className={styles.paramRow}>
                                <input
                                    className={styles.paramSmallInput}
                                    value={spec.default === undefined ? '' : String(spec.default)}
                                    onChange={e => updateParam(pi, { default: coerceParamValue(spec.type, e.target.value) })}
                                    placeholder="既定値"
                                    title="既定値（省略時に使われる値）"
                                />
                                {(spec.type === 'number' || spec.type === 'integer') && (
                                    <>
                                        <input
                                            className={styles.paramSmallInput}
                                            type="number"
                                            value={spec.min ?? ''}
                                            onChange={e => updateParam(pi, { min: e.target.value === '' ? undefined : Number(e.target.value) })}
                                            placeholder="最小"
                                            title="最小値"
                                        />
                                        <input
                                            className={styles.paramSmallInput}
                                            type="number"
                                            value={spec.max ?? ''}
                                            onChange={e => updateParam(pi, { max: e.target.value === '' ? undefined : Number(e.target.value) })}
                                            placeholder="最大"
                                            title="最大値"
                                        />
                                    </>
                                )}
                            </div>
                            <input
                                className={styles.paramDescInput}
                                value={spec.description ?? ''}
                                onChange={e => updateParam(pi, { description: e.target.value })}
                                placeholder="説明（ペルソナへの引数の意味）"
                            />
                        </div>
                    ))}
                    <button className={styles.addCallBtn} onClick={addParam}>
                        + 引数を追加
                    </button>

                    <div className={styles.stepsHeader}>
                        <span className={styles.stepsTitle}>ステップ ({action.steps.length})</span>
                    </div>

                    {action.steps.map((step, si) => (
                        <div key={si} className={styles.stepCard}>
                            <div className={styles.stepHeader}>
                                <span className={styles.stepType}>
                                    {step.type === 'parallel' ? 'PARALLEL' : 'WAIT'}
                                </span>
                                <div className={styles.stepActions}>
                                    <button className={styles.stepSmallBtn} onClick={() => moveStep(si, -1)} disabled={si === 0} title="上へ">&uarr;</button>
                                    <button className={styles.stepSmallBtn} onClick={() => moveStep(si, 1)} disabled={si === action.steps.length - 1} title="下へ">&darr;</button>
                                    <button className={styles.stepSmallBtn} onClick={() => removeStep(si)} title="削除" style={{ color: '#dc2626' }}>&times;</button>
                                </div>
                            </div>

                            {step.type === 'parallel' && (
                                <>
                                    {(step.calls || []).map((call, ci) => (
                                        <div key={ci} className={styles.callCard}>
                                            <div className={styles.callTopRow}>
                                                <select
                                                    className={styles.callToolSelect}
                                                    value={call.tool}
                                                    onChange={e => updateCall(si, ci, { tool: e.target.value })}
                                                >
                                                    <option value="">-- ツール --</option>
                                                    {availableTools.map(t => (
                                                        <option key={t} value={t}>{t}</option>
                                                    ))}
                                                </select>
                                                {(step.calls || []).length > 1 && (
                                                    <button
                                                        className={styles.removeCallBtn}
                                                        onClick={() => removeCall(si, ci)}
                                                        title="このツール呼び出しを削除"
                                                    >
                                                        &times;
                                                    </button>
                                                )}
                                            </div>
                                            <ArgsForm
                                                args={call.args}
                                                schema={toolSchemas[call.tool]?.parameters}
                                                paramNames={paramNames}
                                                onChange={args => updateCall(si, ci, { args })}
                                            />
                                        </div>
                                    ))}
                                    <button className={styles.addCallBtn} onClick={() => addCall(si)}>
                                        + ツール呼び出し追加
                                    </button>
                                </>
                            )}

                            {step.type === 'wait' && (
                                <div>
                                    <label className={styles.fieldLabel}>待機時間 (ms / ${'{'}引数{'}'} / 式)</label>
                                    <input
                                        className={styles.waitInput}
                                        value={step.duration_ms ?? 300}
                                        onChange={e => updateStep(si, { duration_ms: parseDurationInput(e.target.value) })}
                                        placeholder="300 / ${duration_ms} / ${seconds}*1000"
                                    />
                                    {paramNames.length > 0 && (
                                        <div className={styles.paramChipRow}>
                                            {paramNames.map(name => (
                                                <button
                                                    key={name}
                                                    type="button"
                                                    className={styles.paramInsertChip}
                                                    onClick={() => updateStep(si, { duration_ms: `\${${name}}` })}
                                                    title={`待機時間に引数 ${name} を使う`}
                                                >{'${'}{name}{'}'}</button>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    ))}

                    <div className={styles.addStepRow}>
                        <button className={styles.addStepBtn} onClick={() => addStep('parallel')}>
                            + ツール実行ステップ
                        </button>
                        <button className={styles.addStepBtn} onClick={() => addStep('wait')}>
                            + 待機ステップ
                        </button>
                    </div>

                    <TestTargetSelector
                        targets={testTargets}
                        selected={selectedTarget}
                        onSelect={onSelectTarget}
                    />

                    {testResult && (
                        <div className={testResult.ok ? styles.testResultOk : styles.testResultError}>
                            {testResult.text}
                        </div>
                    )}
                </div>

                <div className={styles.editorFooter}>
                    <button className={styles.btnSecondary} onClick={onTest} disabled={busy}>
                        {busy ? '実行中...' : 'テスト実行'}
                    </button>
                    <button className={styles.btnSecondary} onClick={onClose}>
                        キャンセル
                    </button>
                    <button className={styles.btnPrimary} onClick={onSave} disabled={busy}>
                        保存
                    </button>
                </div>
            </div>
        </div>
    );
}


// 数値フィールドの入力を解釈する。空 → undefined、数値 → number、
// それ以外（${param} 等）は文字列のまま保持する。
function parseArgNumber(raw: string): unknown {
    const t = raw.trim();
    if (t === '') return undefined;
    if (/^-?\d+$/.test(t)) return parseInt(t, 10);
    if (/^-?\d*\.\d+$/.test(t)) return Number(t);
    return raw;
}

function ArgField({
    name, prop, required, value, paramNames, onChange,
}: {
    name: string;
    prop: JsonSchemaProp;
    required: boolean;
    value: unknown;
    paramNames: string[];
    onChange: (value: unknown) => void;
}) {
    const type = prop.type;
    const isNum = type === 'number' || type === 'integer';
    const display = value === undefined || value === null ? '' : String(value);

    let control: React.ReactNode;
    if (Array.isArray(prop.enum) && prop.enum.length > 0) {
        control = (
            <select
                className={styles.argFieldSelect}
                value={display}
                onChange={e => onChange(e.target.value === '' ? undefined : e.target.value)}
            >
                <option value="">（未設定）</option>
                {prop.enum.map(v => (
                    <option key={String(v)} value={String(v)}>{String(v)}</option>
                ))}
            </select>
        );
    } else if (type === 'boolean') {
        control = (
            <select
                className={styles.argFieldSelect}
                value={value === undefined ? '' : String(value)}
                onChange={e => onChange(e.target.value === '' ? undefined : e.target.value === 'true')}
            >
                <option value="">（未設定）</option>
                <option value="true">true</option>
                <option value="false">false</option>
            </select>
        );
    } else {
        control = (
            <input
                className={styles.argFieldInput}
                value={display}
                onChange={e => onChange(isNum ? parseArgNumber(e.target.value) : (e.target.value === '' ? undefined : e.target.value))}
                placeholder={isNum ? '数値 / ${引数} / 式' : prop.description || ''}
                title={prop.description || ''}
            />
        );
    }

    return (
        <div className={styles.argField}>
            <label className={styles.argFieldLabel} title={prop.description || ''}>
                {name}
                {required && <span className={styles.argFieldRequired}>*</span>}
            </label>
            {control}
            {isNum && paramNames.length > 0 && (
                <div className={styles.paramChipRow}>
                    {paramNames.map(p => (
                        <button
                            key={p}
                            type="button"
                            className={styles.paramInsertChip}
                            onClick={() => onChange(`\${${p}}`)}
                            title={`${name} に引数 ${p} を使う`}
                        >{'${'}{p}{'}'}</button>
                    ))}
                </div>
            )}
        </div>
    );
}

function ArgsForm({
    args, schema, paramNames, onChange,
}: {
    args: Record<string, unknown>;
    schema?: ToolSchema['parameters'];
    paramNames: string[];
    onChange: (args: Record<string, unknown>) => void;
}) {
    const props = schema?.properties;
    const hasSchema = !!props && Object.keys(props).length > 0;
    const [raw, setRaw] = useState(!hasSchema);

    // スキーマが手に入ったらフォーム編集を既定にする (新規ツール選択時など)。
    // スキーマが無いときは JSON 編集しか選べないので raw を維持。
    useEffect(() => { setRaw(!hasSchema); }, [hasSchema]);

    if (raw || !hasSchema) {
        return (
            <div className={styles.argsFormRaw}>
                <ArgsInput args={args} onChange={onChange} />
                {hasSchema && (
                    <button type="button" className={styles.argModeBtn} onClick={() => setRaw(false)}>
                        フォーム編集に戻す
                    </button>
                )}
            </div>
        );
    }

    const required = new Set(schema?.required || []);
    const setField = (key: string, value: unknown) => {
        const next = { ...args };
        if (value === undefined) delete next[key];
        else next[key] = value;
        onChange(next);
    };

    return (
        <div className={styles.argsForm}>
            {Object.entries(props!).map(([key, prop]) => (
                <ArgField
                    key={key}
                    name={key}
                    prop={prop}
                    required={required.has(key)}
                    value={args[key]}
                    paramNames={paramNames}
                    onChange={v => setField(key, v)}
                />
            ))}
            <button type="button" className={styles.argModeBtn} onClick={() => setRaw(true)}>
                JSON で編集
            </button>
        </div>
    );
}

function ArgsInput({
    args,
    onChange,
}: {
    args: Record<string, unknown>;
    onChange: (args: Record<string, unknown>) => void;
}) {
    const [text, setText] = useState(JSON.stringify(args));
    const [valid, setValid] = useState(true);

    useEffect(() => {
        setText(JSON.stringify(args));
        setValid(true);
    }, [args]);

    const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
        const raw = e.target.value;
        setText(raw);
        try {
            const parsed = JSON.parse(raw);
            if (typeof parsed === 'object' && parsed !== null) {
                setValid(true);
                onChange(parsed);
            } else {
                setValid(false);
            }
        } catch {
            setValid(false);
        }
    };

    const handleBlur = () => {
        if (!valid) {
            setText(JSON.stringify(args));
            setValid(true);
        }
    };

    return (
        <input
            className={`${styles.callArgsInput} ${!valid ? styles.argsInvalid : ''}`}
            value={text}
            onChange={handleChange}
            onBlur={handleBlur}
            placeholder='{"key": "value"}'
        />
    );
}
