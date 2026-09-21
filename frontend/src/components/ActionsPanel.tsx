'use client';
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


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
    useLocale();
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
            const res = await apiFetch(`${API_BASE}/${addonName}/actions`);
            if (res.ok) setActions(await res.json());
        } catch { /* ignore */ }
    }, [addonName]);

    const fetchTools = useCallback(async () => {
        try {
            const res = await apiFetch(`${API_BASE}/${addonName}/actions/tool-schemas`);
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
            const res = await apiFetch(`${API_BASE}/${addonName}/actions/test-targets`);
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
        if (!confirm(uiText("components.ActionsPanel.text001", { p1: actionId }))) return;
        try {
            const res = await apiFetch(`${API_BASE}/${addonName}/actions/${actionId}`, { method: 'DELETE' });
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
            setTestResult({ ok: false, text: uiText("components.ActionsPanel.text002") });
            return;
        }
        setBusy(true);
        setTestResult(null);
        try {
            const url = selectedTarget
                ? `${API_BASE}/${addonName}/actions/test?instance_id=${encodeURIComponent(selectedTarget)}`
                : `${API_BASE}/${addonName}/actions/test`;
            const res = await apiFetch(url, {
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
            const res = await apiFetch(url, {
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
                <span data-i18n="components.ActionsPanel.text003" className={styles.headerLabel}>{uiText("components.ActionsPanel.text003")}{actions.length})
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
                        <p data-i18n="components.ActionsPanel.text004" className={styles.emptyMessage}>{uiText("components.ActionsPanel.text004")}</p>
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
                                        <button data-i18n="components.ActionsPanel.text005"
                                            className={styles.iconBtn}
                                            onClick={() => handleTest(a)}
                                            disabled={busy}
                                            title={uiText("components.ActionsPanel.text005")}
                                        >
                                            &#9654;
                                        </button>
                                        <button data-i18n="components.ActionsPanel.text006"
                                            className={styles.iconBtn}
                                            onClick={() => openEdit(a)}
                                            title={uiText("components.ActionsPanel.text006")}
                                        >
                                            &#9998;
                                        </button>
                                        <button data-i18n="components.ActionsPanel.text007"
                                            className={styles.iconBtnDanger}
                                            onClick={() => handleDelete(a.id)}
                                            title={uiText("components.ActionsPanel.text007")}
                                        >
                                            {uiText("components.ActionsPanel.label001")}</button>
                                    </div>
                                </div>
                            ))}
                        </div>
                    )}
                    <button data-i18n="components.ActionsPanel.text008" className={styles.addBtn} onClick={openNew}>{uiText("components.ActionsPanel.text008")}</button>
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
    useLocale();
    if (targets.length === 0) return null;
    return (
        <div className={styles.testTargetRow}>
            <span data-i18n="components.ActionsPanel.text009" className={styles.testTargetLabel}>{uiText("components.ActionsPanel.text009")}</span>
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
    useLocale();

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
                    <h3 data-i18n="components.ActionsPanel.text010 components.ActionsPanel.text011">{isNew ? uiText("components.ActionsPanel.text010") : uiText("components.ActionsPanel.text011", { p1: action.display_name })}</h3>
                    <button className={styles.iconBtn} onClick={onClose}>{uiText("components.ActionsPanel.label002")}</button>
                </div>
                <div className={styles.editorBody}>
                    <div className={styles.fieldGroup}>
                        <label data-i18n="components.ActionsPanel.text012" className={styles.fieldLabel}>{uiText("components.ActionsPanel.text012")}</label>
                        <input
                            className={styles.fieldInput}
                            value={action.id}
                            onChange={e => update({ id: e.target.value })}
                            placeholder={uiText("components.ActionsPanel.label003")}
                            disabled={!isNew}
                        />
                    </div>
                    <div className={styles.fieldGroup}>
                        <label data-i18n="components.ActionsPanel.text013" className={styles.fieldLabel}>{uiText("components.ActionsPanel.text013")}</label>
                        <input data-i18n="components.ActionsPanel.text014"
                            className={styles.fieldInput}
                            value={action.display_name}
                            onChange={e => update({ display_name: e.target.value })}
                            placeholder={uiText("components.ActionsPanel.text014")}
                        />
                    </div>
                    <div className={styles.fieldGroup}>
                        <label data-i18n="components.ActionsPanel.text015" className={styles.fieldLabel}>{uiText("components.ActionsPanel.text015")}</label>
                        <input data-i18n="components.ActionsPanel.text016"
                            className={styles.fieldInput}
                            value={action.description}
                            onChange={e => update({ description: e.target.value })}
                            placeholder={uiText("components.ActionsPanel.text016")}
                        />
                    </div>

                    <div className={styles.stepsHeader}>
                        <span data-i18n="components.ActionsPanel.text017" className={styles.stepsTitle}>{uiText("components.ActionsPanel.text017")}{paramEntries.length})</span>
                    </div>
                    <p data-i18n="components.ActionsPanel.text018 components.ActionsPanel.text020 components.ActionsPanel.text022" className={styles.paramHint}>{uiText("components.ActionsPanel.text018")}<code data-i18n="components.ActionsPanel.text019">{uiText("components.ActionsPanel.text019")}</code>{uiText("components.ActionsPanel.text020")}<code data-i18n="components.ActionsPanel.text021">{uiText("components.ActionsPanel.text021")}</code>{uiText("components.ActionsPanel.text022")}</p>
                    {paramEntries.map(([name, spec], pi) => (
                        <div key={pi} className={styles.paramCard}>
                            <div className={styles.paramRow}>
                                <input
                                    className={styles.paramNameInput}
                                    value={name}
                                    onChange={e => renameParam(pi, e.target.value)}
                                    placeholder={uiText("components.ActionsPanel.label004")}
                                />
                                <select
                                    className={styles.paramTypeSelect}
                                    value={spec.type}
                                    onChange={e => updateParam(pi, { type: e.target.value as ParamSpec['type'] })}
                                >
                                    {PARAM_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                                </select>
                                <button data-i18n="components.ActionsPanel.text023"
                                    className={styles.stepSmallBtn}
                                    onClick={() => removeParam(pi)}
                                    title={uiText("components.ActionsPanel.text023")}
                                    style={{ color: '#dc2626' }}
                                >{uiText("components.ActionsPanel.label005")}</button>
                            </div>
                            <div className={styles.paramRow}>
                                <input data-i18n="components.ActionsPanel.text024 components.ActionsPanel.text025"
                                    className={styles.paramSmallInput}
                                    value={spec.default === undefined ? '' : String(spec.default)}
                                    onChange={e => updateParam(pi, { default: coerceParamValue(spec.type, e.target.value) })}
                                    placeholder={uiText("components.ActionsPanel.text024")}
                                    title={uiText("components.ActionsPanel.text025")}
                                />
                                {(spec.type === 'number' || spec.type === 'integer') && (
                                    <>
                                        <input data-i18n="components.ActionsPanel.text026 components.ActionsPanel.text027"
                                            className={styles.paramSmallInput}
                                            type="number"
                                            value={spec.min ?? ''}
                                            onChange={e => updateParam(pi, { min: e.target.value === '' ? undefined : Number(e.target.value) })}
                                            placeholder={uiText("components.ActionsPanel.text026")}
                                            title={uiText("components.ActionsPanel.text027")}
                                        />
                                        <input data-i18n="components.ActionsPanel.text028 components.ActionsPanel.text029"
                                            className={styles.paramSmallInput}
                                            type="number"
                                            value={spec.max ?? ''}
                                            onChange={e => updateParam(pi, { max: e.target.value === '' ? undefined : Number(e.target.value) })}
                                            placeholder={uiText("components.ActionsPanel.text028")}
                                            title={uiText("components.ActionsPanel.text029")}
                                        />
                                    </>
                                )}
                            </div>
                            <input data-i18n="components.ActionsPanel.text030"
                                className={styles.paramDescInput}
                                value={spec.description ?? ''}
                                onChange={e => updateParam(pi, { description: e.target.value })}
                                placeholder={uiText("components.ActionsPanel.text030")}
                            />
                        </div>
                    ))}
                    <button data-i18n="components.ActionsPanel.text031" className={styles.addCallBtn} onClick={addParam}>{uiText("components.ActionsPanel.text031")}</button>

                    <div className={styles.stepsHeader}>
                        <span data-i18n="components.ActionsPanel.text032" className={styles.stepsTitle}>{uiText("components.ActionsPanel.text032")}{action.steps.length})</span>
                    </div>

                    {action.steps.map((step, si) => (
                        <div key={si} className={styles.stepCard}>
                            <div className={styles.stepHeader}>
                                <span className={styles.stepType}>
                                    {step.type === 'parallel' ? 'PARALLEL' : 'WAIT'}
                                </span>
                                <div className={styles.stepActions}>
                                    <button data-i18n="components.ActionsPanel.text033" className={styles.stepSmallBtn} onClick={() => moveStep(si, -1)} disabled={si === 0} title={uiText("components.ActionsPanel.text033")}>{uiText("components.ActionsPanel.label006")}</button>
                                    <button data-i18n="components.ActionsPanel.text034" className={styles.stepSmallBtn} onClick={() => moveStep(si, 1)} disabled={si === action.steps.length - 1} title={uiText("components.ActionsPanel.text034")}>{uiText("components.ActionsPanel.label007")}</button>
                                    <button data-i18n="components.ActionsPanel.text035" className={styles.stepSmallBtn} onClick={() => removeStep(si)} title={uiText("components.ActionsPanel.text035")} style={{ color: '#dc2626' }}>{uiText("components.ActionsPanel.label008")}</button>
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
                                                    <option data-i18n="components.ActionsPanel.text036" value="">{uiText("components.ActionsPanel.text036")}</option>
                                                    {availableTools.map(t => (
                                                        <option key={t} value={t}>{t}</option>
                                                    ))}
                                                </select>
                                                {(step.calls || []).length > 1 && (
                                                    <button data-i18n="components.ActionsPanel.text037"
                                                        className={styles.removeCallBtn}
                                                        onClick={() => removeCall(si, ci)}
                                                        title={uiText("components.ActionsPanel.text037")}
                                                    >
                                                        {uiText("components.ActionsPanel.label009")}</button>
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
                                    <button data-i18n="components.ActionsPanel.text038" className={styles.addCallBtn} onClick={() => addCall(si)}>{uiText("components.ActionsPanel.text038")}</button>
                                </>
                            )}

                            {step.type === 'wait' && (
                                <div>
                                    <label data-i18n="components.ActionsPanel.text039 components.ActionsPanel.text040 components.ActionsPanel.text041" className={styles.fieldLabel}>{uiText("components.ActionsPanel.text039")}{'{'}{uiText("components.ActionsPanel.text040")}{'}'}{uiText("components.ActionsPanel.text041")}</label>
                                    <input
                                        className={styles.waitInput}
                                        value={step.duration_ms ?? 300}
                                        onChange={e => updateStep(si, { duration_ms: parseDurationInput(e.target.value) })}
                                        placeholder={uiText("components.ActionsPanel.label010")}
                                    />
                                    {paramNames.length > 0 && (
                                        <div className={styles.paramChipRow}>
                                            {paramNames.map(name => (
                                                <button data-i18n="components.ActionsPanel.text042"
                                                    key={name}
                                                    type="button"
                                                    className={styles.paramInsertChip}
                                                    onClick={() => updateStep(si, { duration_ms: `\${${name}}` })}
                                                    title={uiText("components.ActionsPanel.text042", { p1: name })}
                                                >{'${'}{name}{'}'}</button>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    ))}

                    <div className={styles.addStepRow}>
                        <button data-i18n="components.ActionsPanel.text043" className={styles.addStepBtn} onClick={() => addStep('parallel')}>{uiText("components.ActionsPanel.text043")}</button>
                        <button data-i18n="components.ActionsPanel.text044" className={styles.addStepBtn} onClick={() => addStep('wait')}>{uiText("components.ActionsPanel.text044")}</button>
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
                    <button data-i18n="components.ActionsPanel.text045 components.ActionsPanel.text046" className={styles.btnSecondary} onClick={onTest} disabled={busy}>
                        {busy ? uiText("components.ActionsPanel.text045") : uiText("components.ActionsPanel.text046")}
                    </button>
                    <button data-i18n="components.ActionsPanel.text047" className={styles.btnSecondary} onClick={onClose}>{uiText("components.ActionsPanel.text047")}</button>
                    <button data-i18n="components.ActionsPanel.text048" className={styles.btnPrimary} onClick={onSave} disabled={busy}>{uiText("components.ActionsPanel.text048")}</button>
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
    useLocale();
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
                <option data-i18n="components.ActionsPanel.text049" value="">{uiText("components.ActionsPanel.text049")}</option>
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
                <option data-i18n="components.ActionsPanel.text050" value="">{uiText("components.ActionsPanel.text050")}</option>
                <option value="true">{uiText("components.ActionsPanel.label011")}</option>
                <option value="false">{uiText("components.ActionsPanel.label012")}</option>
            </select>
        );
    } else {
        control = (
            <input data-i18n="components.ActionsPanel.text051"
                className={styles.argFieldInput}
                value={display}
                onChange={e => onChange(isNum ? parseArgNumber(e.target.value) : (e.target.value === '' ? undefined : e.target.value))}
                placeholder={isNum ? uiText("components.ActionsPanel.text051") : prop.description || ''}
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
                        <button data-i18n="components.ActionsPanel.text052"
                            key={p}
                            type="button"
                            className={styles.paramInsertChip}
                            onClick={() => onChange(`\${${p}}`)}
                            title={uiText("components.ActionsPanel.text052", { p1: name, p2: p })}
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
    useLocale();
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
                    <button data-i18n="components.ActionsPanel.text053" type="button" className={styles.argModeBtn} onClick={() => setRaw(false)}>{uiText("components.ActionsPanel.text053")}</button>
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
            <button data-i18n="components.ActionsPanel.text054" type="button" className={styles.argModeBtn} onClick={() => setRaw(true)}>{uiText("components.ActionsPanel.text054")}</button>
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
    useLocale();
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
            placeholder={uiText("components.ActionsPanel.label013")}
        />
    );
}
