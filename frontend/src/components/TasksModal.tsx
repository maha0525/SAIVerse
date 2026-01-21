import React, { useState, useEffect } from 'react';
import { X, CheckCircle, Clock, AlertCircle, Plus, RefreshCw, ChevronRight } from 'lucide-react';
import styles from './TasksModal.module.css';
import ModalOverlay from './common/ModalOverlay';

interface TaskStep {
    id: string;
    position: number;
    title: string;
    description?: string;
    status: string;
    notes?: string;
    updated_at: string;
}

interface TaskRecord {
    id: string;
    title: string;
    description: string;
    goal: string;
    summary: string;
    status: string;
    priority: string;
    active_step_id: string | null;
    updated_at: string;
    steps: TaskStep[];
}

interface HistoryEntry {
    id: string;
    event_type: string;
    payload: any;
    actor: string;
    created_at: string;
}

interface TasksModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

export default function TasksModal({ isOpen, onClose, personaId }: TasksModalProps) {
    const [tasks, setTasks] = useState<TaskRecord[]>([]);
    const [selectedTask, setSelectedTask] = useState<TaskRecord | null>(null);
    const [history, setHistory] = useState<HistoryEntry[]>([]);
    const [view, setView] = useState<'details' | 'create'>('details');

    // Create Form
    const [formTitle, setFormTitle] = useState('');
    const [formGoal, setFormGoal] = useState('');
    const [formSummary, setFormSummary] = useState('');
    const [formSteps, setFormSteps] = useState(''); // Textarea, parsed by newline

    useEffect(() => {
        if (isOpen) {
            loadTasks();
        }
    }, [isOpen, personaId]);

    useEffect(() => {
        if (selectedTask) {
            loadHistory(selectedTask.id);
        } else {
            setHistory([]);
        }
    }, [selectedTask]);

    const loadTasks = async () => {
        try {
            const res = await fetch(`/api/people/${personaId}/tasks`);
            if (res.ok) {
                const data = await res.json();
                setTasks(data);
                if (data.length > 0 && !selectedTask) {
                    setSelectedTask(data[0]);
                }
            }
        } catch (e) { console.error(e); }
    };

    const loadHistory = async (taskId: string) => {
        try {
            const res = await fetch(`/api/people/${personaId}/tasks/${taskId}/history`);
            if (res.ok) {
                setHistory(await res.json());
            }
        } catch (e) { console.error(e); }
    };

    const handleCreate = async () => {
        const steps = formSteps.split('\n').filter(s => s.trim()).map(s => ({ title: s.trim() }));
        const payload = {
            title: formTitle,
            goal: formGoal,
            summary: formSummary,
            steps: steps
        };

        try {
            const res = await fetch(`/api/people/${personaId}/tasks`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                await loadTasks();
                setView('details');
                setFormTitle('');
                setFormGoal('');
                setFormSummary('');
                setFormSteps('');
            } else {
                alert('Failed to create task');
            }
        } catch (e) { console.error(e); }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 className={styles.title}>Tasks: {personaId}</h2>
                    <button className={styles.closeButton} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.container}>
                    {/* Sidebar: List */}
                    <div className={styles.sidebar}>
                        <div className={styles.sidebarHeader}>
                            <span style={{ fontWeight: 600 }}>All Tasks</span>
                            <button onClick={loadTasks} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#aaa' }}><RefreshCw size={14} /></button>
                            <button onClick={() => setView('create')} style={{ background: 'none', border: 'none', cursor: 'pointer', color: '#4dabf7' }}><Plus size={16} /></button>
                        </div>
                        <div className={styles.taskList}>
                            {tasks.map(t => (
                                <div
                                    key={t.id}
                                    className={`${styles.taskItem} ${selectedTask?.id === t.id ? styles.selected : ''}`}
                                    onClick={() => { setSelectedTask(t); setView('details'); }}
                                >
                                    <div className={styles.taskItemTitle}>{t.title}</div>
                                    <div className={styles.taskItemMeta}>
                                        <span>{t.status}</span>
                                        <span>{new Date(t.updated_at).toLocaleDateString()}</span>
                                    </div>
                                </div>
                            ))}
                        </div>
                    </div>

                    {/* Main Area */}
                    <div className={styles.mainArea}>
                        {view === 'create' ? (
                            <div className={styles.createUi}>
                                <h3>Create New Task</h3>
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>Title</label>
                                    <input className={styles.input} value={formTitle} onChange={e => setFormTitle(e.target.value)} />
                                </div>
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>Goal (Detailed)</label>
                                    <textarea className={styles.textarea} value={formGoal} onChange={e => setFormGoal(e.target.value)} />
                                </div>
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>Summary (Short)</label>
                                    <input className={styles.input} value={formSummary} onChange={e => setFormSummary(e.target.value)} />
                                </div>
                                <div className={styles.formGroup}>
                                    <label className={styles.label}>Steps (One per line)</label>
                                    <textarea className={styles.textarea} style={{ height: 150 }} value={formSteps} onChange={e => setFormSteps(e.target.value)} placeholder="Step 1...&#10;Step 2..." />
                                </div>
                                <button className={styles.createBtn} onClick={handleCreate}>Create Task</button>
                                <button style={{ marginLeft: '1rem', background: 'none', color: '#aaa', border: 'none', cursor: 'pointer' }} onClick={() => setView('details')}>Cancel</button>
                            </div>
                        ) : selectedTask ? (
                            <div className={styles.detailsContainer}>
                                <div className={styles.detailsHeader}>
                                    <div>
                                        <h1 className={styles.detailsTitle}>{selectedTask.title}</h1>
                                        <div className={styles.detailsMeta}>
                                            <span>Goal: {selectedTask.goal}</span>
                                        </div>
                                    </div>
                                    <span className={`${styles.statusBadge} ${styles[`status_${selectedTask.status}`]}`}>
                                        {selectedTask.status}
                                    </span>
                                </div>

                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>Summary</div>
                                    <p style={{ color: '#ccc' }}>{selectedTask.summary}</p>
                                </div>

                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>Steps</div>
                                    <div className={styles.stepList}>
                                        {selectedTask.steps.map(s => (
                                            <div key={s.id} className={styles.stepItem} style={{ opacity: s.id === selectedTask.active_step_id ? 1 : 0.6 }}>
                                                <div className={styles.stepContent}>
                                                    <div className={styles.stepTitle}>
                                                        {s.position}. {s.title}
                                                        {s.id === selectedTask.active_step_id && <span style={{ color: '#4dabf7', marginLeft: 8 }}>(Active)</span>}
                                                    </div>
                                                    {s.notes && <div className={styles.stepDesc}>Note: {s.notes}</div>}
                                                </div>
                                                <div className={styles.statusBadge} style={{ backgroundColor: '#333', color: '#aaa' }}>
                                                    {s.status}
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>

                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>History</div>
                                    <div className={styles.historyList}>
                                        {history.map(h => (
                                            <div key={h.id} className={styles.historyItem}>
                                                <div className={styles.historyMeta}>
                                                    [{new Date(h.created_at).toLocaleString()}] <span style={{ color: '#ffd43b' }}>{h.actor || 'System'}</span>
                                                </div>
                                                <div className={styles.historyEvent}>
                                                    {h.event_type}
                                                </div>
                                                <div style={{ color: '#888' }}>
                                                    {JSON.stringify(h.payload)}
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        ) : (
                            <div className={styles.emptyState}>Select a task to view details</div>
                        )}
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
