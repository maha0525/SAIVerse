"use client";

import React, { useState, useEffect } from 'react';
import { X, Loader2, CheckCircle, ArrowRight, ArrowLeft, MessageSquare, Settings, SlidersHorizontal } from 'lucide-react';
import styles from './PersonaWizard.module.css';
import MemoryImport from './memory/MemoryImport';
import ModalOverlay from './common/ModalOverlay';
import SettingsModal from './SettingsModal';

interface PersonaWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete?: (personaId: string, roomId: string) => void;
    /** When true, hides "チャットする" button to prevent page reload (used inside TutorialWizard). */
    embedded?: boolean;
}

interface City {
    CITYID: number;
    /** 内部の識別子。ペルソナ ID のサフィックスに使う */
    CITY_SLUG: string;
    /** 表示名。選択肢のラベルに使う */
    CITYNAME: string;
    DESCRIPTION?: string;
}

type Step = 1 | 2 | 3;

/**
 * manager/ids.py slugify_identifier のフロント写し。名前を ID 用の ASCII slug に
 * 落とす (小文字化・空白→'_'・契約外文字は除去)。日本語名のように何も残らない
 * 名前では空文字列を返し、呼び出し元が persona_<連番> へ切り替える。
 */
function slugifyIdentifier(text: string): string {
    return text
        .toLowerCase()
        .trim()
        .replace(/\s+/g, '_')
        .replace(/[^a-z0-9_-]/g, '')
        .replace(/_{2,}/g, '_')
        .replace(/^[_-]+|[_-]+$/g, '');
}

export default function PersonaWizard({ isOpen, onClose, onComplete, embedded }: PersonaWizardProps) {
    const [step, setStep] = useState<Step>(1);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Step 1: Basic Info
    const [name, setName] = useState('');
    const [customId, setCustomId] = useState('');
    // ID 欄をユーザーが手で触ったか。触るまでは名前に追随して自動で埋める
    const [idTouched, setIdTouched] = useState(false);
    // 連番 (persona_N) の空きを調べるための既存 ID 一覧。バックエンドの
    // manager/ids.py と同じ席 (AIID と私室 Building の両方) を見る
    const [takenAiIds, setTakenAiIds] = useState<Set<string>>(new Set());
    const [takenBuildingIds, setTakenBuildingIds] = useState<Set<string>>(new Set());
    const [systemPrompt, setSystemPrompt] = useState('');
    const [cities, setCities] = useState<City[]>([]);
    const [selectedCityId, setSelectedCityId] = useState<number | null>(null);
    // ペルソナ ID のサフィックスは City の内部識別子 (CITY_SLUG)。表示名ではない
    // — 表示名は日本語も取りうるため ID に混ぜられない (docs/intent/city_identity.md)
    const [citySlug, setCitySlug] = useState('');

    // Created persona info
    const [createdPersonaId, setCreatedPersonaId] = useState<string | null>(null);
    const [createdRoomId, setCreatedRoomId] = useState<string | null>(null);
    const [showSettings, setShowSettings] = useState(false);

    // Reset on open
    useEffect(() => {
        if (isOpen) {
            setStep(1);
            setName('');
            setCustomId('');
            setIdTouched(false);
            setSystemPrompt('');
            setError(null);
            setCreatedPersonaId(null);
            setCreatedRoomId(null);
            setShowSettings(false);
            loadCities();
            loadTakenIds();
        }
    }, [isOpen]);

    const loadCities = async () => {
        try {
            const res = await fetch('/api/db/tables/city');
            if (res.ok) {
                const data = await res.json();
                setCities(data);
                if (data.length > 0) {
                    setSelectedCityId(data[0].CITYID);
                    setCitySlug(data[0].CITY_SLUG);
                }
            }
        } catch (e) {
            console.error('Failed to load cities', e);
        }
    };

    const loadTakenIds = async () => {
        try {
            const [aiRes, buildingRes] = await Promise.all([
                fetch('/api/db/tables/ai?limit=100000'),
                fetch('/api/db/tables/building?limit=100000'),
            ]);
            if (aiRes.ok) {
                const ais = await aiRes.json();
                setTakenAiIds(new Set(ais.map((a: { AIID: string }) => a.AIID)));
            }
            if (buildingRes.ok) {
                const buildings = await buildingRes.json();
                setTakenBuildingIds(new Set(buildings.map((b: { BUILDINGID: string }) => b.BUILDINGID)));
            }
        } catch (e) {
            // 取れなくても致命ではない (初期値の提案が素朴になるだけで、
            // 確定はバックエンドの契約が行う)
            console.error('Failed to load existing ids', e);
        }
    };

    // ID 欄の自動入力: 名前が英数字に落とせるならその slug、何も残らない名前
    // (日本語など) では空いている persona_<連番>。ユーザーが手で触ったら追随を
    // やめる。最終的な確定と検証はバックエンド (manager/ids.py の契約) が行い、
    // ここは「送信前に実際の ID が見える」ための初期値
    useEffect(() => {
        if (!isOpen || idTouched) return;
        const slug = slugifyIdentifier(name);
        if (slug) {
            setCustomId(slug);
            return;
        }
        if (!citySlug) return;
        let n = 1;
        while (
            takenAiIds.has(`persona_${n}_${citySlug}`) ||
            takenBuildingIds.has(`persona_${n}_${citySlug}_room`)
        ) {
            n += 1;
        }
        setCustomId(`persona_${n}`);
    }, [isOpen, idTouched, name, citySlug, takenAiIds, takenBuildingIds]);

    const handleCityChange = (cityId: number) => {
        setSelectedCityId(cityId);
        const city = cities.find(c => c.CITYID === cityId);
        if (city) {
            setCitySlug(city.CITY_SLUG);
        }
    };

    const validateStep1 = (): boolean => {
        if (!name.trim()) {
            setError('ペルソナの名前を入力してください');
            return false;
        }
        if (!selectedCityId) {
            setError('Cityを選択してください');
            return false;
        }
        // Validate custom ID if provided (manager/ids.py の契約と同じ形)
        if (customId && !/^[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(customId)) {
            setError('IDは英数字で始まり、英数字・_・- のみ使用できます');
            return false;
        }
        setError(null);
        return true;
    };

    const createPersona = async () => {
        if (!validateStep1()) return;

        setIsLoading(true);
        setError(null);

        try {
            const res = await fetch('/api/world/ais', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: name.trim(),
                    system_prompt: systemPrompt.trim() || `あなたは${name}です。`,
                    home_city_id: selectedCityId,
                    // ID 欄をユーザーが触っていなければ null を送り、生成を
                    // バックエンド (manager/ids.py の契約 + 連番予約) に任せる。
                    // 自動入力値をそのまま custom ID として送ると、フロントの
                    // 一覧スナップショット (取得失敗・件数上限・作成競合で
                    // 不完全になりうる) が予約の根拠になってしまう
                    ai_id: idTouched ? customId.trim() || null : null,
                }),
            });

            const data = await res.json().catch(() => ({ detail: res.statusText }));

            if (!res.ok) {
                setError(data.detail || '不明なエラーが発生しました');
                return;
            }

            // Use IDs from API response (authoritative source)
            const personaId = data.ai_id;
            const roomId = data.room_id;

            if (!personaId || !roomId) {
                // Fallback: reconstruct locally (should not happen with updated API)
                const baseId = customId.trim() || name.trim().toLowerCase().replace(/\s+/g, '_');
                setCreatedPersonaId(`${baseId}_${citySlug}`);
                setCreatedRoomId(`${baseId}_${citySlug}_room`);
            } else {
                setCreatedPersonaId(personaId);
                setCreatedRoomId(roomId);
            }
            setStep(2);
        } catch (e) {
            console.error('Failed to create persona', e);
            setError('ペルソナの作成に失敗しました');
        } finally {
            setIsLoading(false);
        }
    };

    const handleComplete = () => {
        if (createdPersonaId && createdRoomId && onComplete) {
            onComplete(createdPersonaId, createdRoomId);
        }
        onClose();
    };

    const goToRoom = async () => {
        if (!createdRoomId) return;

        try {
            const res = await fetch('/api/user/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_building_id: createdRoomId }),
            });

            if (res.ok) {
                handleComplete();
                // Refresh page to load new room
                window.location.reload();
            } else {
                setError('部屋への移動に失敗しました');
            }
        } catch (e) {
            console.error('Failed to move to room', e);
            setError('部屋への移動に失敗しました');
        }
    };

    if (!isOpen) return null;

    const renderStep1 = () => (
        <>
            <div className={styles.field}>
                <label>ペルソナの名前 *</label>
                <input
                    type="text"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="例: エア"
                    autoFocus
                />
            </div>

            <div className={styles.field}>
                <label>ID (英数字)</label>
                <div className={styles.idFieldRow}>
                    <input
                        type="text"
                        value={customId}
                        onChange={(e) => {
                            setIdTouched(true);
                            setCustomId(e.target.value.replace(/[^a-zA-Z0-9_-]/g, ''));
                        }}
                        placeholder="persona_1"
                    />
                    <span className={styles.idSuffix}>_{citySlug}</span>
                </div>
                <p className={styles.hint}>
                    名前から自動で入ります (日本語名は連番)。編集もできます
                </p>
            </div>

            {cities.length > 1 && (
                <div className={styles.field}>
                    <label>City</label>
                    <select
                        value={selectedCityId || ''}
                        onChange={(e) => handleCityChange(parseInt(e.target.value))}
                    >
                        {cities.map(c => (
                            <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME || c.CITY_SLUG}</option>
                        ))}
                    </select>
                </div>
            )}

            <div className={styles.field}>
                <label>システムプロンプト</label>
                <textarea
                    value={systemPrompt}
                    onChange={(e) => setSystemPrompt(e.target.value)}
                    placeholder="ペルソナの性格や設定を入力 (後から編集可能)"
                />
                <p className={styles.hint}>
                    空欄の場合はデフォルトのプロンプトが設定されます
                </p>
            </div>

            {error && <p className={styles.error}>{error}</p>}
        </>
    );

    const renderStep2 = () => (
        <div className={styles.importStep}>
            <h3>ChatGPTからログをインポート</h3>
            <p>
                ChatGPTのエクスポートデータをインポートすると、過去の会話を記憶として引き継げます。
                この手順はスキップして後から行うこともできます。
            </p>
            {createdPersonaId && (
                <MemoryImport
                    personaId={createdPersonaId}
                    onImportComplete={() => {
                        // Import completed with thread selection done
                    }}
                />
            )}
        </div>
    );

    const renderStep3 = () => (
        <div className={styles.completeContainer}>
            <CheckCircle size={64} className={styles.successIcon} />
            <h2 className={styles.completeTitle}>ペルソナを作成しました</h2>
            <p className={styles.completeSubtitle}>
                {name} の部屋が作成されました。{embedded ? 'セットアップを続けましょう！' : 'チャットを始めましょう！'}
            </p>
            <div className={styles.completeActions}>
                {!embedded && (
                    <button className={styles.primaryButton} onClick={goToRoom}>
                        <MessageSquare size={18} />
                        チャットを始める
                    </button>
                )}
                {!embedded && (
                    <button className={styles.secondaryButton} onClick={() => setShowSettings(true)}>
                        <SlidersHorizontal size={18} />
                        もっと設定する
                    </button>
                )}
                {embedded && (
                    <button className={styles.primaryButton} onClick={handleComplete}>
                        <ArrowRight size={18} />
                        セットアップに戻る
                    </button>
                )}
            </div>
            {error && <p className={styles.error}>{error}</p>}
        </div>
    );

    const renderContent = () => {
        switch (step) {
            case 1:
                return renderStep1();
            case 2:
                return renderStep2();
            case 3:
                return renderStep3();
        }
    };

    const renderActions = () => {
        if (step === 3) return null; // Step 3 has its own actions

        return (
            <div className={styles.actions}>
                <div className={styles.actionsLeft}>
                    {step > 1 && (
                        <button className={styles.backButton} onClick={() => setStep((step - 1) as Step)}>
                            <ArrowLeft size={16} /> 戻る
                        </button>
                    )}
                </div>
                <div className={styles.actionsRight}>
                    {step === 1 && (
                        <button
                            className={styles.nextButton}
                            onClick={createPersona}
                            disabled={isLoading || !name.trim()}
                        >
                            {isLoading ? <Loader2 size={16} className={styles.loader} /> : null}
                            次へ <ArrowRight size={16} />
                        </button>
                    )}
                    {step === 2 && (
                        <>
                            <button className={styles.skipButton} onClick={() => setStep(3)}>
                                スキップ
                            </button>
                            <button className={styles.nextButton} onClick={() => setStep(3)}>
                                完了 <ArrowRight size={16} />
                            </button>
                        </>
                    )}
                </div>
            </div>
        );
    };

    return (
        <>
            <ModalOverlay onClose={onClose} className={styles.overlay}>
                <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                    <div className={styles.header}>
                        <h2 className={styles.title}>ペルソナを作成</h2>
                        <button className={styles.closeButton} onClick={onClose}>
                            <X size={20} />
                        </button>
                    </div>

                    <div className={styles.stepper}>
                        <div className={`${styles.step} ${step >= 1 ? styles.active : ''} ${step > 1 ? styles.completed : ''}`}>
                            <span className={styles.stepNumber}>{step > 1 ? '✓' : '1'}</span>
                            <span>基本情報</span>
                        </div>
                        <div className={`${styles.step} ${step >= 2 ? styles.active : ''} ${step > 2 ? styles.completed : ''}`}>
                            <span className={styles.stepNumber}>{step > 2 ? '✓' : '2'}</span>
                            <span>ログインポート</span>
                        </div>
                        <div className={`${styles.step} ${step >= 3 ? styles.active : ''}`}>
                            <span className={styles.stepNumber}>3</span>
                            <span>完了</span>
                        </div>
                    </div>

                    <div className={styles.content}>
                        {renderContent()}
                    </div>

                    {renderActions()}
                </div>
            </ModalOverlay>

            {showSettings && createdPersonaId && (
                <SettingsModal
                    isOpen={showSettings}
                    onClose={() => {
                        setShowSettings(false);
                        handleComplete();
                    }}
                    personaId={createdPersonaId}
                />
            )}
        </>
    );
}
