import React, { useState, useEffect } from 'react';
import { X, Calendar, Play, Clock, Repeat, Trash2, Power, Plus, Edit2 } from 'lucide-react';
import styles from './ScheduleModal.module.css';

interface ScheduleItem {
    schedule_id: number;
    schedule_type: string;
    meta_playbook: string;
    description: string;
    priority: number;
    enabled: boolean;
    days_of_week: number[] | null;
    time_of_day: string | null;
    scheduled_datetime: string | null;
    interval_seconds: number | null;
    completed: boolean;
}

interface ScheduleModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

export default function ScheduleModal({ isOpen, onClose, personaId }: ScheduleModalProps) {
    const [schedules, setSchedules] = useState<ScheduleItem[]>([]);
    const [playbooks, setPlaybooks] = useState<string[]>([]);
    const [loading, setLoading] = useState(false);

    // Edit mode state
    const [editingId, setEditingId] = useState<number | null>(null);

    // Form State
    const [formType, setFormType] = useState<'periodic' | 'oneshot' | 'interval'>('periodic');
    const [formPlaybook, setFormPlaybook] = useState('');
    const [formDesc, setFormDesc] = useState('');
    const [formPriority, setFormPriority] = useState(0);
    const [formEnabled, setFormEnabled] = useState(true);

    // Periodic
    const [formDays, setFormDays] = useState<number[]>([]); // 0-6
    const [formTime, setFormTime] = useState('09:00');

    // Oneshot
    const [formDateTime, setFormDateTime] = useState(''); // YYYY-MM-DD HH:MM

    // Interval
    const [formInterval, setFormInterval] = useState(600);

    useEffect(() => {
        if (isOpen) {
            loadSchedules();
            loadPlaybooks();
        }
    }, [isOpen, personaId]);

    const loadSchedules = async () => {
        setLoading(true);
        try {
            const res = await fetch(`/api/people/${personaId}/schedules`);
            if (res.ok) {
                setSchedules(await res.json());
            }
        } catch (e) {
            console.error(e);
        } finally {
            setLoading(false);
        }
    };

    const loadPlaybooks = async () => {
        try {
            const res = await fetch(`/api/people/meta_playbooks`);
            if (res.ok) {
                const data = await res.json();
                setPlaybooks(data);
                if (data.length > 0 && !formPlaybook) setFormPlaybook(data[0]);
            }
        } catch (e) {
            console.error(e);
        }
    };

    const resetForm = () => {
        setEditingId(null);
        setFormType('periodic');
        setFormPlaybook(playbooks.length > 0 ? playbooks[0] : '');
        setFormDesc('');
        setFormPriority(0);
        setFormEnabled(true);
        setFormDays([]);
        setFormTime('09:00');
        setFormDateTime('');
        setFormInterval(600);
    };

    const handleEdit = (s: ScheduleItem) => {
        setEditingId(s.schedule_id);
        setFormType(s.schedule_type as 'periodic' | 'oneshot' | 'interval');
        setFormPlaybook(s.meta_playbook);
        setFormDesc(s.description || '');
        setFormPriority(s.priority);
        setFormEnabled(s.enabled);
        setFormDays(s.days_of_week || []);
        setFormTime(s.time_of_day || '09:00');
        setFormInterval(s.interval_seconds || 600);

        // Convert UTC datetime to local format for oneshot
        if (s.scheduled_datetime) {
            try {
                const dt = new Date(s.scheduled_datetime);
                const year = dt.getFullYear();
                const month = String(dt.getMonth() + 1).padStart(2, '0');
                const day = String(dt.getDate()).padStart(2, '0');
                const hours = String(dt.getHours()).padStart(2, '0');
                const minutes = String(dt.getMinutes()).padStart(2, '0');
                setFormDateTime(`${year}-${month}-${day} ${hours}:${minutes}`);
            } catch {
                setFormDateTime('');
            }
        } else {
            setFormDateTime('');
        }
    };

    const handleSave = async () => {
        const payload: any = {
            schedule_type: formType,
            meta_playbook: formPlaybook,
            description: formDesc,
            priority: formPriority,
            enabled: formEnabled
        };

        if (formType === 'periodic') {
            payload.days_of_week = formDays;
            payload.time_of_day = formTime;
        } else if (formType === 'oneshot') {
            payload.scheduled_datetime = formDateTime;
        } else if (formType === 'interval') {
            payload.interval_seconds = formInterval;
        }

        try {
            const isEdit = editingId !== null;
            const url = isEdit
                ? `/api/people/${personaId}/schedules/${editingId}`
                : `/api/people/${personaId}/schedules`;
            const method = isEdit ? 'PUT' : 'POST';

            const res = await fetch(url, {
                method,
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                loadSchedules();
                resetForm();
            } else {
                const errorData = await res.json().catch(() => ({}));
                alert(errorData.detail || `Failed to ${isEdit ? 'update' : 'create'} schedule`);
            }
        } catch (e) {
            console.error(e);
        }
    };

    const handleToggle = async (id: number) => {
        try {
            await fetch(`/api/people/${personaId}/schedules/${id}/toggle`, { method: 'POST' });
            loadSchedules();
        } catch (e) { console.error(e); }
    };

    const handleDelete = async (id: number) => {
        if (!confirm('Are you sure?')) return;
        try {
            await fetch(`/api/people/${personaId}/schedules/${id}`, { method: 'DELETE' });
            loadSchedules();
        } catch (e) { console.error(e); }
    };

    const formatDetail = (s: ScheduleItem) => {
        if (s.schedule_type === 'periodic') {
            const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
            const ds = s.days_of_week ? s.days_of_week.map(d => days[d]).join(', ') : 'Daily';
            return `${ds} @ ${s.time_of_day}`;
        }
        if (s.schedule_type === 'oneshot') {
            return `${new Date(s.scheduled_datetime || '').toLocaleString()} ${s.completed ? '(Done)' : ''}`;
        }
        if (s.schedule_type === 'interval') {
            return `Every ${s.interval_seconds}s`;
        }
        return '?';
    };

    if (!isOpen) return null;

    return (
        <div
            className={styles.overlay}
            onClick={onClose}
            onTouchStart={(e) => e.stopPropagation()}
            onTouchMove={(e) => e.stopPropagation()}
        >
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>Schedule Management: {personaId}</h2>
                    <button className={styles.closeButton} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {/* List Section */}
                    <div className={styles.listSection}>
                        <div className={styles.sectionTitle}>
                            <span>Current Schedules</span>
                            <button onClick={loadSchedules} style={{ background: 'none', border: 'none', color: '#4dabf7', cursor: 'pointer' }}>Refresh</button>
                        </div>
                        <div className={styles.tableContainer}>
                            {schedules.length === 0 ? (
                                <div className={styles.emptyState}>No schedules found.</div>
                            ) : (
                                <table className={styles.table}>
                                    <thead>
                                        <tr>
                                            <th>Type</th>
                                            <th>Playbook</th>
                                            <th>Description</th>
                                            <th>Detail</th>
                                            <th>State</th>
                                            <th>Actions</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {schedules.map(s => (
                                            <tr key={s.schedule_id}>
                                                <td style={{ textTransform: 'capitalize' }}>{s.schedule_type}</td>
                                                <td>{s.meta_playbook}</td>
                                                <td>{s.description}</td>
                                                <td>{formatDetail(s)}</td>
                                                <td>
                                                    <span className={s.enabled ? styles.enabled : styles.disabled}>
                                                        {s.enabled ? 'Enabled' : 'Disabled'}
                                                    </span>
                                                </td>
                                                <td>
                                                    <div className={styles.actions}>
                                                        <button className={`${styles.actionBtn} ${styles.editBtn}`} onClick={() => handleEdit(s)} title="Edit">
                                                            <Edit2 size={14} />
                                                        </button>
                                                        <button className={`${styles.actionBtn} ${styles.toggleBtn}`} onClick={() => handleToggle(s.schedule_id)} title="Toggle">
                                                            <Power size={14} />
                                                        </button>
                                                        <button className={`${styles.actionBtn} ${styles.deleteBtn}`} onClick={() => handleDelete(s.schedule_id)} title="Delete">
                                                            <Trash2 size={14} />
                                                        </button>
                                                    </div>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            )}
                        </div>
                    </div>

                    {/* Form Section */}
                    <div className={styles.formSection}>
                        <div className={styles.sectionTitle}>
                            {editingId !== null ? 'Edit Schedule' : 'Add New Schedule'}
                            {editingId !== null && (
                                <button
                                    onClick={resetForm}
                                    style={{ marginLeft: '10px', background: 'none', border: 'none', color: '#868e96', cursor: 'pointer', fontSize: '0.9em' }}
                                >
                                    (Cancel)
                                </button>
                            )}
                        </div>
                        <div className={styles.formGrid}>
                            <div className={styles.formGroup}>
                                <label className={styles.label}>Schedule Type</label>
                                <select
                                    className={styles.select}
                                    value={formType}
                                    onChange={e => setFormType(e.target.value as any)}
                                >
                                    <option value="periodic">Periodic (Weekly)</option>
                                    <option value="oneshot">One-shot (Once)</option>
                                    <option value="interval">Interval (Recurring)</option>
                                </select>
                            </div>

                            <div className={styles.formGroup}>
                                <label className={styles.label}>Meta Playbook</label>
                                <select
                                    className={styles.select}
                                    value={formPlaybook}
                                    onChange={e => setFormPlaybook(e.target.value)}
                                >
                                    {playbooks.map(p => <option key={p} value={p}>{p}</option>)}
                                </select>
                            </div>

                            <div className={styles.formGroup} style={{ gridColumn: '1 / -1' }}>
                                <label className={styles.label}>Description</label>
                                <input
                                    className={styles.input}
                                    value={formDesc}
                                    onChange={e => setFormDesc(e.target.value)}
                                    placeholder="Task description..."
                                />
                            </div>

                            {formType === 'periodic' && (
                                <>
                                    <div className={styles.formGroup} style={{ gridColumn: '1 / -1' }}>
                                        <label className={styles.label}>Days of Week</label>
                                        <div className={styles.checkboxGroup}>
                                            {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((day, idx) => (
                                                <label key={day} className={styles.checkboxLabel}>
                                                    <input
                                                        type="checkbox"
                                                        checked={formDays.includes(idx)}
                                                        onChange={e => {
                                                            if (e.target.checked) setFormDays([...formDays, idx]);
                                                            else setFormDays(formDays.filter(d => d !== idx));
                                                        }}
                                                    />
                                                    {day}
                                                </label>
                                            ))}
                                        </div>
                                    </div>
                                    <div className={styles.formGroup}>
                                        <label className={styles.label}>Time (HH:MM)</label>
                                        <input
                                            className={styles.input}
                                            value={formTime}
                                            onChange={e => setFormTime(e.target.value)}
                                            type="time"
                                        />
                                    </div>
                                </>
                            )}

                            {formType === 'oneshot' && (
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>DateTime (YYYY-MM-DD HH:MM)</label>
                                    <input
                                        className={styles.input}
                                        value={formDateTime}
                                        onChange={e => setFormDateTime(e.target.value)}
                                        placeholder="2025-12-07 09:00"
                                    />
                                </div>
                            )}

                            {formType === 'interval' && (
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>Interval (Seconds)</label>
                                    <input
                                        className={styles.input}
                                        type="number"
                                        value={formInterval}
                                        onChange={e => setFormInterval(parseInt(e.target.value))}
                                    />
                                </div>
                            )}

                            <div className={styles.formGroup}>
                                <label className={styles.label}>Priority</label>
                                <input
                                    className={styles.input}
                                    type="number"
                                    value={formPriority}
                                    onChange={e => setFormPriority(parseInt(e.target.value))}
                                />
                            </div>
                        </div>

                        <button className={styles.submitBtn} onClick={handleSave}>
                            {editingId !== null ? 'Update Schedule' : 'Add Schedule'}
                        </button>
                    </div>
                </div>
            </div>
        </div>
    );
}
