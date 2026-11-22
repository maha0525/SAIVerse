import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import styles from './RoomView.module.css';
import type { BuildingDetails, Occupant } from '@/lib/useBuildingDetails';
import type { RoomViewPlacement } from '@/lib/roomViewPlacement';
import { keepRoomViewPick, type RoomViewPick } from '@/lib/roomViewAutoSwitch';

interface RoomViewProps {
    /** 閲覧中の部屋の詳細 (useBuildingDetails の結果)。まだ無い間は null。 */
    details: BuildingDetails | null;
    /** 喋っているペルソナ。いなければ最後に喋ったペルソナ。 */
    speakerId?: string;
    placement: RoomViewPlacement;
    /** 枠の下側に重ねて出す中身。'overlay' の置き方でログが見えない間に、
     * ペルソナがいま喋っている内容を読めるようにするためのもの (呼び出し側が渡す)。 */
    caption?: ReactNode;
}

/** ルームビュー: いまいる Building の内装画像を背景に、そこにいるペルソナ一人の姿を立てる
 * (docs/intent/screen_avatar.md 設計 1・設計 2 の「内装画像と立ち絵」)。 */
export default function RoomView({ details, speakerId, placement, caption }: RoomViewProps) {
    useLocale();
    const hasCaption = caption !== undefined && caption !== null && caption !== false;
    const captionBoxRef = useRef<HTMLDivElement>(null);
    const captionBodyRef = useRef<HTMLDivElement>(null);
    // 本文が伸びるたびに、字幕の箱を一番下へスクロールする。
    useEffect(() => {
        if (!hasCaption) return;
        const box = captionBoxRef.current;
        const body = captionBodyRef.current;
        if (!box || !body || typeof ResizeObserver === 'undefined') return;
        const observer = new ResizeObserver(() => {
            box.scrollTop = box.scrollHeight;
        });
        observer.observe(body);
        return () => observer.disconnect();
    }, [hasCaption]);
    // 押して選んだペルソナ。選んだ時点の speakerId を添えて持ち、speakerId が
    // 別の人に変わったら選択を捨てる (喋っているペルソナへ戻る)。捨てた選択は、
    // speakerId が元の人に戻っても復活しない。
    const [picked, setPicked] = useState<RoomViewPick | null>(null);
    const keptPick = keepRoomViewPick(picked, speakerId);
    if (keptPick !== picked) setPicked(keptPick);
    // 読み込めなかった内装画像の URL。URL ごとに持つので、部屋が変われば新しい画像を試す。
    const [failedBackgrounds, setFailedBackgrounds] = useState<Set<string>>(new Set());

    const occupants = details?.occupants ?? [];
    const pickedOccupant = keptPick
        ? occupants.find(o => o.id === keptPick.personaId)
        : undefined;
    const shown: Occupant | undefined = pickedOccupant
        ?? (speakerId ? occupants.find(o => o.id === speakerId) : undefined)
        ?? occupants[0];

    const backgroundSrc = details?.image_path && !failedBackgrounds.has(details.image_path)
        ? details.image_path
        : null;

    return (
        <div
            className={`${styles.roomView} ${styles[placement]}${hasCaption ? ` ${styles.withCaption}` : ''}`}
            role="region"
            aria-label={uiText("components.RoomView.label001")}
        >
            {backgroundSrc ? (
                <img
                    className={styles.background}
                    src={backgroundSrc}
                    alt={uiText("components.RoomView.alt001", { p1: details?.name ?? '' })}
                    onError={() => setFailedBackgrounds(prev => {
                        const next = new Set(prev);
                        next.add(backgroundSrc);
                        return next;
                    })}
                />
            ) : (
                <div className={styles.backgroundFallback} />
            )}

            {shown && (
                <div className={styles.figureArea}>
                    {/* 立ち絵を 3D のアバターへ差し替えるときは、この部品を替える (設計 3) */}
                    <PersonaFigure key={shown.id} occupant={shown} />
                </div>
            )}

            {occupants.length > 1 && (
                <div className={styles.switcher}>
                    {occupants.map(o => (
                        <button
                            key={o.id}
                            type="button"
                            className={`${styles.switcherButton} ${shown?.id === o.id ? styles.switcherActive : ''}`}
                            onClick={() => setPicked({ personaId: o.id, speakerAtPick: speakerId })}
                            title={uiText("components.RoomView.text001", { p1: o.name })}
                            aria-label={uiText("components.RoomView.text001", { p1: o.name })}
                            aria-pressed={shown?.id === o.id}
                        >
                            <SwitcherIcon occupant={o} />
                        </button>
                    ))}
                </div>
            )}

            {hasCaption && (
                <div
                    ref={captionBoxRef}
                    className={styles.caption}
                    role="region"
                    aria-label={uiText("components.RoomView.caption001")}
                >
                    <div ref={captionBodyRef} className={styles.captionBody}>
                        {caption}
                    </div>
                </div>
            )}
        </div>
    );
}

/** ペルソナの姿を描く部分。見た目の画像 (appearance_image)、無ければアイコン (avatar)、
 * それも駄目なら名前の頭文字の札。 */
function PersonaFigure({ occupant }: { occupant: Occupant }) {
    useLocale();
    const [failed, setFailed] = useState<Set<string>>(new Set());
    const candidates = [occupant.appearance_image, occupant.avatar]
        .filter((src): src is string => !!src && !failed.has(src));
    const src = candidates[0];

    return (
        <>
            <div className={styles.figureSlot}>
                {src ? (
                    <img
                        key={src}
                        className={styles.figureCard}
                        src={src}
                        alt={uiText("components.RoomView.alt002", { p1: occupant.name })}
                        onError={() => setFailed(prev => {
                            const next = new Set(prev);
                            next.add(src);
                            return next;
                        })}
                    />
                ) : (
                    <div className={`${styles.figureCard} ${styles.initialCard}`} aria-hidden="true">
                        {initialOf(occupant.name)}
                    </div>
                )}
            </div>
            <div className={styles.nameTag}>{occupant.name}</div>
        </>
    );
}

function SwitcherIcon({ occupant }: { occupant: Occupant }) {
    const [failed, setFailed] = useState(false);
    if (!occupant.avatar || failed) {
        return <span className={styles.switcherInitial}>{initialOf(occupant.name)}</span>;
    }
    return <img src={occupant.avatar} alt="" onError={() => setFailed(true)} />;
}

function initialOf(name: string): string {
    return Array.from(name.trim())[0] ?? '?';
}
