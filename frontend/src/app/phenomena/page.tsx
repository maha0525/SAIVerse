"use client";

import { useState, useEffect, useCallback, useRef } from 'react';
import styles from './page.module.css';
import Sidebar from '@/components/Sidebar';
import { Plus, Edit2, Trash2, Check, X, AlertCircle } from 'lucide-react';

interface PhenomenonRule {
    rule_id: number;
    trigger_type: string;
    condition_json: string | null;
    phenomenon_name: string;
    argument_mapping_json: string | null;
    enabled: boolean;
    priority: number;
    description: string;
    updated_at: string;
}

interface TriggerInfo {
    type: string;
    fields: Record<string, string>;
}

interface PhenomenonInfo {
    name: string;
    description: string;
    parameters: any;
    is_async: boolean;
}

export default function PhenomenaPage() {
    const [rules, setRules] = useState<PhenomenonRule[]>([]);
    const [triggers, setTriggers] = useState<TriggerInfo[]>([]);
    const [phenomena, setPhenomena] = useState<PhenomenonInfo[]>([]);
    const [isSidebarOpen, setIsSidebarOpen] = useState(false);

    // Modal State
    const [isModalOpen, setIsModalOpen] = useState(false);
    const [editingRule, setEditingRule] = useState<PhenomenonRule | null>(null);

    // Form State
    const [formData, setFormData] = useState({
        trigger_type: '',
        condition_json: '{}',
        phenomenon_name: '',
        argument_mapping_json: '{}',
        enabled: true,
        priority: 0,
        description: ''
    });
    const [jsonErrors, setJsonErrors] = useState<{ condition?: string, mapping?: string }>({});
    const [isSaving, setIsSaving] = useState(false);
    const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);

    // Track if the close gesture originated from overlay (not drag)
    const overlayMouseDownRef = useRef(false);

    const fetchData = useCallback(async () => {
        try {
            const [rulesRes, triggersRes, phenomenaRes] = await Promise.all([
                fetch('/api/phenomena/rules'),
                fetch('/api/phenomena/triggers'),
                fetch('/api/phenomena/available')
            ]);

            if (rulesRes.ok) setRules(await rulesRes.json());
            if (triggersRes.ok) setTriggers(await triggersRes.json());
            if (phenomenaRes.ok) setPhenomena(await phenomenaRes.json());
        } catch (error) {
            console.error('Failed to fetch data', error);
        }
    }, []);

    useEffect(() => {
        fetchData();
    }, [fetchData]);

    const handleOpenModal = (rule?: PhenomenonRule) => {
        if (rule) {
            setEditingRule(rule);
            setFormData({
                trigger_type: rule.trigger_type,
                condition_json: rule.condition_json || '{}',
                phenomenon_name: rule.phenomenon_name,
                argument_mapping_json: rule.argument_mapping_json || '{}',
                enabled: rule.enabled,
                priority: rule.priority,
                description: rule.description || ''
            });
        } else {
            setEditingRule(null);
            setFormData({
                trigger_type: triggers[0]?.type || '',
                condition_json: '{}',
                phenomenon_name: phenomena[0]?.name || '',
                argument_mapping_json: '{}',
                enabled: true,
                priority: 0,
                description: ''
            });
        }
        setJsonErrors({});
        setHasUnsavedChanges(false);
        setIsModalOpen(true);
    };

    const handleCloseModal = () => {
        if (hasUnsavedChanges) {
            if (!confirm('編集内容が保存されていません。閉じますか？')) {
                return;
            }
        }
        setIsModalOpen(false);
        setHasUnsavedChanges(false);
    };

    const handleFormChange = (updates: Partial<typeof formData>) => {
        setFormData(prev => ({ ...prev, ...updates }));
        setHasUnsavedChanges(true);
    };

    const validateJson = (key: 'condition' | 'mapping', value: string) => {
        try {
            if (!value) return true;
            JSON.parse(value);
            setJsonErrors(prev => ({ ...prev, [key]: undefined }));
            return true;
        } catch (e) {
            setJsonErrors(prev => ({ ...prev, [key]: 'Invalid JSON format' }));
            return false;
        }
    };

    const handleSave = async () => {
        // Validate JSON fields before submit
        const validCondition = validateJson('condition', formData.condition_json);
        const validMapping = validateJson('mapping', formData.argument_mapping_json);

        if (!validCondition || !validMapping) return;

        setIsSaving(true);
        try {
            const url = editingRule
                ? `/api/phenomena/rules/${editingRule.rule_id}`
                : '/api/phenomena/rules';

            const method = editingRule ? 'PUT' : 'POST';

            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(formData)
            });

            if (res.ok) {
                setIsModalOpen(false);
                fetchData();
            } else {
                const err = await res.json();
                alert(`Error: ${err.detail}`);
            }
        } catch (error) {
            console.error('Save failed', error);
            alert('Failed to save rule');
        } finally {
            setIsSaving(false);
        }
    };

    const handleDelete = async (id: number) => {
        if (!confirm('Are you sure you want to delete this rule?')) return;

        try {
            const res = await fetch(`/api/phenomena/rules/${id}`, { method: 'DELETE' });
            if (res.ok) {
                fetchData();
            } else {
                alert('Failed to delete rule');
            }
        } catch (error) {
            console.error('Delete failed', error);
        }
    };

    return (
        <div className={styles.container}>
            <Sidebar
                onMove={() => {
                    // Navigate back to main chat page
                    window.location.href = '/';
                }}
                isOpen={isSidebarOpen}
                onOpen={() => setIsSidebarOpen(true)}
                onClose={() => setIsSidebarOpen(false)}
            />

            <main className={styles.contentWrapper}>
                <header className={styles.header}>
                    <div className={styles.headerLeft}>
                        <h1>Phenomenon Rules</h1>
                    </div>
                </header>

                <div className={styles.scrollArea}>
                    <div className={styles.card}>
                        <div className={styles.sectionTitle}>
                            <span>Defined Rules</span>
                            <button className={styles.createBtn} onClick={() => handleOpenModal()}>
                                <Plus size={16} /> New Rule
                            </button>
                        </div>

                        <table className={styles.table}>
                            <thead>
                                <tr>
                                    <th>ID</th>
                                    <th>Trigger</th>
                                    <th>Condition</th>
                                    <th>Phenomenon</th>
                                    <th>Mapping</th>
                                    <th>State</th>
                                    <th>Actions</th>
                                </tr>
                            </thead>
                            <tbody>
                                {rules.length === 0 ? (
                                    <tr>
                                        <td colSpan={7} style={{ textAlign: 'center', padding: '2rem', color: '#94a3b8' }}>
                                            No rules defined yet.
                                        </td>
                                    </tr>
                                ) : (
                                    rules.map(rule => (
                                        <tr key={rule.rule_id}>
                                            <td>#{rule.rule_id}</td>
                                            <td>
                                                <div style={{ fontWeight: 500 }}>{rule.trigger_type}</div>
                                                <div style={{ fontSize: '0.8rem', color: '#64748b' }}>{rule.description}</div>
                                            </td>
                                            <td>
                                                <code style={{ fontSize: '0.8rem', background: 'rgba(0, 0, 0, 0.3)', padding: '2px 6px', borderRadius: '4px', color: '#e5e7eb' }}>
                                                    {rule.condition_json && rule.condition_json.length > 50
                                                        ? rule.condition_json.substring(0, 50) + '...'
                                                        : rule.condition_json || '-'}
                                                </code>
                                            </td>
                                            <td>
                                                <span style={{ fontWeight: 500, color: '#818cf8' }}>{rule.phenomenon_name}</span>
                                            </td>
                                            <td>
                                                <code style={{ fontSize: '0.8rem', background: 'rgba(0, 0, 0, 0.3)', padding: '2px 6px', borderRadius: '4px', color: '#e5e7eb' }}>
                                                    {rule.argument_mapping_json && rule.argument_mapping_json.length > 50
                                                        ? rule.argument_mapping_json.substring(0, 50) + '...'
                                                        : rule.argument_mapping_json || '-'}
                                                </code>
                                            </td>
                                            <td>
                                                <span className={`${styles.badge} ${rule.enabled ? styles.badgeEnabled : styles.badgeDisabled}`}>
                                                    {rule.enabled ? 'Active' : 'Disabled'}
                                                </span>
                                            </td>
                                            <td>
                                                <button className={styles.actionBtn} onClick={() => handleOpenModal(rule)}>
                                                    <Edit2 size={14} /> Edit
                                                </button>
                                                <button className={styles.deleteBtn} onClick={() => handleDelete(rule.rule_id)}>
                                                    <Trash2 size={14} /> Delete
                                                </button>
                                            </td>
                                        </tr>
                                    ))
                                )}
                            </tbody>
                        </table>
                    </div>
                </div>
            </main>

            {isModalOpen && (
                <div
                    className={styles.modalOverlay}
                    onMouseDown={(e) => {
                        // Only mark as overlay click if target is the overlay itself
                        if (e.target === e.currentTarget) {
                            overlayMouseDownRef.current = true;
                        }
                    }}
                    onMouseUp={(e) => {
                        // Only close if mousedown was on overlay AND mouseup is on overlay
                        if (overlayMouseDownRef.current && e.target === e.currentTarget) {
                            handleCloseModal();
                        }
                        overlayMouseDownRef.current = false;
                    }}
                >
                    <div className={styles.modalContent} onMouseDown={e => e.stopPropagation()}>
                        <h2 style={{ fontSize: '1.2rem', marginBottom: '1.5rem' }}>
                            {editingRule ? 'Edit Rule' : 'Create New Rule'}
                        </h2>

                        <div className={styles.formGroup}>
                            <label>Description</label>
                            <input
                                className={styles.formInput}
                                value={formData.description}
                                onChange={e => handleFormChange({ description: e.target.value })}
                                placeholder="e.g. Log when user enters room"
                            />
                        </div>

                        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
                            <div className={styles.formGroup}>
                                <label>Trigger Type</label>
                                <select
                                    className={styles.formSelect}
                                    value={formData.trigger_type}
                                    onChange={e => handleFormChange({ trigger_type: e.target.value })}
                                >
                                    <option value="">Select Trigger...</option>
                                    {triggers.map(t => (
                                        <option key={t.type} value={t.type}>{t.type}</option>
                                    ))}
                                </select>
                            </div>

                            <div className={styles.formGroup}>
                                <label>Result Phenomenon</label>
                                <select
                                    className={styles.formSelect}
                                    value={formData.phenomenon_name}
                                    onChange={e => handleFormChange({ phenomenon_name: e.target.value })}
                                >
                                    <option value="">Select Phenomenon...</option>
                                    {phenomena.map(p => (
                                        <option key={p.name} value={p.name}>{p.name}</option>
                                    ))}
                                </select>
                            </div>
                        </div>

                        <div className={styles.formGroup}>
                            <label>
                                Trigger Condition (JSON)
                                {jsonErrors.condition && <span className={styles.jsonError}> - {jsonErrors.condition}</span>}
                            </label>
                            <textarea
                                className={styles.formTextarea}
                                value={formData.condition_json}
                                onChange={e => handleFormChange({ condition_json: e.target.value })}
                                onBlur={(e) => validateJson('condition', e.target.value)}
                            />
                            <div style={{ fontSize: '0.8rem', color: '#64748b', marginTop: '0.2rem' }}>
                                Filter fields: {triggers.find(t => t.type === formData.trigger_type)?.fields ? JSON.stringify(triggers.find(t => t.type === formData.trigger_type)?.fields) : ''}
                            </div>
                        </div>

                        <div className={styles.formGroup}>
                            <label>
                                Argument Mapping (JSON)
                                {jsonErrors.mapping && <span className={styles.jsonError}> - {jsonErrors.mapping}</span>}
                            </label>
                            <textarea
                                className={styles.formTextarea}
                                value={formData.argument_mapping_json}
                                onChange={e => handleFormChange({ argument_mapping_json: e.target.value })}
                                onBlur={(e) => validateJson('mapping', e.target.value)}
                            />
                            <div style={{ fontSize: '0.8rem', color: '#64748b', marginTop: '0.2rem' }}>
                                Use e.g. <code>{"{ \"arg\": \"$trigger.field\" }"}</code>. Available: {triggers.find(t => t.type === formData.trigger_type)?.fields ? Object.keys(triggers.find(t => t.type === formData.trigger_type)!.fields).map(f => `$trigger.${f}`).join(', ') : ''}
                            </div>
                        </div>

                        <div style={{ display: 'flex', gap: '2rem' }}>
                            <div className={`${styles.formGroup} ${styles.checkboxGroup}`}>
                                <input
                                    type="checkbox"
                                    id="enabled"
                                    checked={formData.enabled}
                                    onChange={e => handleFormChange({ enabled: e.target.checked })}
                                />
                                <label htmlFor="enabled" style={{ marginBottom: 0 }}>Rule Enabled</label>
                            </div>

                            <div className={styles.formGroup} style={{ flex: 1 }}>
                                <label>Priority (Higher runs first)</label>
                                <input
                                    type="number"
                                    className={styles.formInput}
                                    value={formData.priority}
                                    onChange={e => handleFormChange({ priority: parseInt(e.target.value) || 0 })}
                                />
                            </div>
                        </div>

                        <div className={styles.modalActions}>
                            <button className={styles.cancelBtn} onClick={handleCloseModal}>Cancel</button>
                            <button
                                className={styles.saveBtn}
                                onClick={handleSave}
                                disabled={isSaving || !!jsonErrors.condition || !!jsonErrors.mapping}
                            >
                                {isSaving ? 'Saving...' : 'Save Rule'}
                            </button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}
