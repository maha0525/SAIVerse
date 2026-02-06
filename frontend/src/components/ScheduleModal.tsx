import React, { useState, useEffect } from 'react';
import { X, Calendar, Play, Clock, Repeat, Trash2, Power, Plus, Edit2 } from 'lucide-react';
import styles from './ScheduleModal.module.css';
import ModalOverlay from './common/ModalOverlay';

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
    playbook_params: Record<string, any> | null;
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

    // Playbook Parameters
    const [formPlaybookParams, setFormPlaybookParams] = useState<Record<string, any>>({});
    const [playbookParamSpecs, setPlaybookParamSpecs] = useState<PlaybookParam[]>([]);

    useEffect(() => {
        if (isOpen) {
            loadSchedules();
            loadPlaybooks();
        }
    }, [isOpen, personaId]);

    // Fetch playbook params when playbook changes
    useEffect(() => {
        if (formPlaybook) {
            fetchPlaybookParams(formPlaybook);
        } else {
            setPlaybookParamSpecs([]);
            setFormPlaybookParams({});
        }
    }, [formPlaybook]);

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

    const fetchPlaybookParams = async (playbookName: string) => {
        try {
            const res = await fetch(`/api/config/playbooks/${encodeURIComponent(playbookName)}/params`);
            if (res.ok) {
                const data = await res.json();
                setPlaybookParamSpecs(data.params || []);
            } else {
                setPlaybookParamSpecs([]);
            }
        } catch (e) {
            console.error('Failed to fetch playbook params', e);
            setPlaybookParamSpecs([]);
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
        setFormPlaybookParams({});
        setPlaybookParamSpecs([]);
    };

    const handleEdit = async (s: ScheduleItem) => {
        setEditingId(s.schedule_id);
        setFormType(s.schedule_type as 'periodic' | 'oneshot' | 'interval');
        setFormPlaybook(s.meta_playbook);
        setFormDesc(s.description || '');
        setFormPriority(s.priority);
        setFormEnabled(s.enabled);
        setFormDays(s.days_of_week || []);
        setFormTime(s.time_of_day || '09:00');
        setFormInterval(s.interval_seconds || 600);
        setFormPlaybookParams(s.playbook_params || {});

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

        // Fetch playbook params for editing
        await fetchPlaybookParams(s.meta_playbook);
    };

    const handlePlaybookParamChange = (paramName: string, value: any) => {
        setFormPlaybookParams(prev => ({ ...prev, [paramName]: value }));
    };

    const handleSave = async () => {
        const payload: any = {
            schedule_type: formType,
            meta_playbook: formPlaybook,
            description: formDesc,
            priority: formPriority,
            enabled: formEnabled,
            playbook_params: Object.keys(formPlaybookParams).length > 0 ? formPlaybookParams : null
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
        if (!confirm('このスケジュールを削除しますか？')) return;
        try {
            await fetch(`/api/people/${personaId}/schedules/${id}`, { method: 'DELETE' });
            loadSchedules();
        } catch (e) { console.error(e); }
    };

    const formatDetail = (s: ScheduleItem) => {
        if (s.schedule_type === 'periodic') {
            const days = ["月", "火", "水", "木", "金", "土", "日"];
            const ds = s.days_of_week ? s.days_of_week.map(d => days[d]).join(', ') : '毎日';
            return `${ds} @ ${s.time_of_day}`;
        }
        if (s.schedule_type === 'oneshot') {
            return `${new Date(s.scheduled_datetime || '').toLocaleString()} ${s.completed ? '(完了)' : ''}`;
        }
        if (s.schedule_type === 'interval') {
            return `${s.interval_seconds}秒ごと`;
        }
        return '?';
    };

    const formatPlaybookParams = (params: Record<string, any> | null) => {
        if (!params || Object.keys(params).length === 0) return '-';
        return Object.entries(params)
            .map(([k, v]) => `${k}: ${v}`)
            .join(', ');
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>スケジュール管理: {personaId}</h2>
                    <button className={styles.closeButton} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {/* List Section */}
                    <div className={styles.listSection}>
                        <div className={styles.sectionTitle}>
                            <span>登録済みスケジュール</span>
                            <button onClick={loadSchedules} style={{ background: 'none', border: 'none', color: '#4dabf7', cursor: 'pointer' }}>更新</button>
                        </div>
                        <div className={styles.tableContainer}>
                            {schedules.length === 0 ? (
                                <div className={styles.emptyState}>スケジュールがありません</div>
                            ) : (
                                <table className={styles.table}>
                                    <thead>
                                        <tr>
                                            <th>種別</th>
                                            <th>Playbook</th>
                                            <th>説明</th>
                                            <th>詳細</th>
                                            <th>パラメータ</th>
                                            <th>状態</th>
                                            <th>操作</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {schedules.map(s => (
                                            <tr key={s.schedule_id}>
                                                <td style={{ textTransform: 'capitalize' }}>{s.schedule_type}</td>
                                                <td>{s.meta_playbook}</td>
                                                <td>{s.description}</td>
                                                <td>{formatDetail(s)}</td>
                                                <td style={{ fontSize: '0.85em', maxWidth: '150px', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                                                    {formatPlaybookParams(s.playbook_params)}
                                                </td>
                                                <td>
                                                    <span className={s.enabled ? styles.enabled : styles.disabled}>
                                                        {s.enabled ? '有効' : '無効'}
                                                    </span>
                                                </td>
                                                <td>
                                                    <div className={styles.actions}>
                                                        <button className={`${styles.actionBtn} ${styles.editBtn}`} onClick={() => handleEdit(s)} title="編集">
                                                            <Edit2 size={14} />
                                                        </button>
                                                        <button className={`${styles.actionBtn} ${styles.toggleBtn}`} onClick={() => handleToggle(s.schedule_id)} title="切替">
                                                            <Power size={14} />
                                                        </button>
                                                        <button className={`${styles.actionBtn} ${styles.deleteBtn}`} onClick={() => handleDelete(s.schedule_id)} title="削除">
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
                            {editingId !== null ? 'スケジュールを編集' : '新規スケジュール追加'}
                            {editingId !== null && (
                                <button
                                    onClick={resetForm}
                                    style={{ marginLeft: '10px', background: 'none', border: 'none', color: '#868e96', cursor: 'pointer', fontSize: '0.9em' }}
                                >
                                    (キャンセル)
                                </button>
                            )}
                        </div>
                        <div className={styles.formGrid}>
                            <div className={styles.formGroup}>
                                <label className={styles.label}>スケジュール種別</label>
                                <select
                                    className={styles.select}
                                    value={formType}
                                    onChange={e => setFormType(e.target.value as any)}
                                >
                                    <option value="periodic">定期（週次）</option>
                                    <option value="oneshot">単発（1回のみ）</option>
                                    <option value="interval">インターバル（繰り返し）</option>
                                </select>
                            </div>

                            <div className={styles.formGroup}>
                                <label className={styles.label}>Meta Playbook</label>
                                <select
                                    className={styles.select}
                                    value={formPlaybook}
                                    onChange={e => {
                                        setFormPlaybook(e.target.value);
                                        setFormPlaybookParams({}); // Reset params when playbook changes
                                    }}
                                >
                                    {playbooks.map(p => <option key={p} value={p}>{p}</option>)}
                                </select>
                            </div>

                            <div className={styles.formGroup} style={{ gridColumn: '1 / -1' }}>
                                <label className={styles.label}>説明</label>
                                <input
                                    className={styles.input}
                                    value={formDesc}
                                    onChange={e => setFormDesc(e.target.value)}
                                    placeholder="タスクの説明..."
                                />
                            </div>

                            {/* Playbook Parameters Section */}
                            {playbookParamSpecs.length > 0 && (
                                <div className={styles.formGroup} style={{ gridColumn: '1 / -1' }}>
                                    <label className={styles.label}>Playbook パラメータ</label>
                                    <div className={styles.paramGrid}>
                                        {playbookParamSpecs.map(param => (
                                            <div key={param.name} className={styles.paramItem}>
                                                <label className={styles.paramLabel}>
                                                    {param.description || param.name}
                                                    {!param.required && <span style={{ fontSize: '0.8em', color: '#888' }}> （任意）</span>}
                                                </label>

                                                {param.resolved_options && param.resolved_options.length > 0 ? (
                                                    <select
                                                        className={styles.select}
                                                        value={formPlaybookParams[param.name] ?? param.default ?? ''}
                                                        onChange={e => handlePlaybookParamChange(param.name, e.target.value || null)}
                                                    >
                                                        <option value="">{param.required ? '（選択...）' : '（自動）'}</option>
                                                        {param.resolved_options.map(opt => (
                                                            <option key={opt.value} value={opt.value}>{opt.label}</option>
                                                        ))}
                                                    </select>
                                                ) : param.param_type === 'boolean' ? (
                                                    <input
                                                        type="checkbox"
                                                        checked={formPlaybookParams[param.name] ?? param.default ?? false}
                                                        onChange={e => handlePlaybookParamChange(param.name, e.target.checked)}
                                                    />
                                                ) : param.param_type === 'number' ? (
                                                    <input
                                                        type="number"
                                                        className={styles.input}
                                                        value={formPlaybookParams[param.name] ?? param.default ?? ''}
                                                        onChange={e => handlePlaybookParamChange(param.name, parseFloat(e.target.value))}
                                                    />
                                                ) : (
                                                    <input
                                                        type="text"
                                                        className={styles.input}
                                                        value={formPlaybookParams[param.name] ?? param.default ?? ''}
                                                        onChange={e => handlePlaybookParamChange(param.name, e.target.value)}
                                                        placeholder={param.description}
                                                    />
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {formType === 'periodic' && (
                                <>
                                    <div className={styles.formGroup} style={{ gridColumn: '1 / -1' }}>
                                        <label className={styles.label}>曜日</label>
                                        <div className={styles.checkboxGroup}>
                                            {["月", "火", "水", "木", "金", "土", "日"].map((day, idx) => (
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
                                        <label className={styles.label}>時刻 (HH:MM)</label>
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
                                    <label className={styles.label}>日時 (YYYY-MM-DD HH:MM)</label>
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
                                    <label className={styles.label}>インターバル（秒）</label>
                                    <input
                                        className={styles.input}
                                        type="number"
                                        value={formInterval}
                                        onChange={e => setFormInterval(parseInt(e.target.value))}
                                    />
                                </div>
                            )}

                            <div className={styles.formGroup}>
                                <label className={styles.label}>優先度</label>
                                <input
                                    className={styles.input}
                                    type="number"
                                    value={formPriority}
                                    onChange={e => setFormPriority(parseInt(e.target.value))}
                                />
                            </div>
                        </div>

                        <button className={styles.submitBtn} onClick={handleSave}>
                            {editingId !== null ? 'スケジュールを更新' : 'スケジュールを追加'}
                        </button>
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
