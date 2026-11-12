'use client';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useId, useMemo, useRef, useState } from 'react';
import { ChevronDown, ChevronRight, Star } from 'lucide-react';
import type { PickerModel } from '@/lib/modelList';
import styles from './ModelPicker.module.css';

/**
 * モデルを 1 つ選ぶ欄 (docs/intent/model_picker.md)。
 *
 * 閉じているときは、いま選ばれているモデルの名前が出たボタン 1 つ。押すと、
 * その場で下に広がる (浮かぶメニューにはしない — モーダルの中で切れたり、
 * スマホで扱いにくくなったりするため)。広がった中身は上から、
 * お気に入りといま選ばれているモデル → 「選ばない」の選択肢 → プロバイダの
 * ボタンの列 → 押されているプロバイダの最初から出すモデル → 「他のモデル」。
 *
 * 並べ替えはしない。サーバーが返した順番のまま、グループ分けと is_current での
 * 振り分けだけをする。
 */
export interface ModelPickerProps {
    /** 一覧。呼ぶ側が、その欄に出してよいものだけに絞って渡す。 */
    models: PickerModel[];
    /** いま選ばれているモデルの id (空文字 = 選ばれていない)。 */
    value: string;
    onChange: (id: string) => void;
    /** 渡されたときだけ、「選ばない」の選択肢 (値は空文字) をこの文言で出す。 */
    emptyLabel?: string;
    /** value が一覧に無いとき、value の前に付けて見せる文言。 */
    unknownValueLabel?: string;
    /** 渡されたときだけ、お気に入りを一番上に出す。 */
    favorites?: string[];
    /** 渡されたときだけ、各行に星のボタンを出す。 */
    onToggleFavorite?: (id: string) => void;
    disabled?: boolean;
    ariaLabel?: string;
    /** 閉じたときのボタンを出さず、いつも広がった状態で見せる。開閉を呼ぶ側の
     *  ボタンが持つ画面 (モデルロール設定の「変更」) のためのもの。 */
    alwaysOpen?: boolean;
}

interface GroupInfo {
    key: string;
    /** サーバーが付けた表示名の予備。表示名は文言の切り替えに追従させるため描画のたびに引く。 */
    fallbackLabel?: string;
    order: number;
    firstIndex: number;
}

function groupKeyOf(m: PickerModel): string {
    return m.picker_group || m.group || m.provider || 'other';
}

function isCurrent(m: PickerModel): boolean {
    // サーバーが印を付けていない行は、畳まずに最初から出す
    return m.is_current !== false;
}

/** プロバイダのボタンの表示名。知っているグループは文言から、知らないグループは
 *  サーバーが付けた picker_group_label を使う。 */
function groupLabel(key: string, fallback?: string): string {
    switch (key) {
        case 'anthropic':
            return uiText('components.common.ModelPicker.groupAnthropic');
        case 'openai':
            return uiText('components.common.ModelPicker.groupOpenai');
        case 'openai_codex':
            return uiText('components.common.ModelPicker.groupOpenaiCodex');
        case 'gemini_paid':
            return uiText('components.common.ModelPicker.groupGeminiPaid');
        case 'gemini_free':
            return uiText('components.common.ModelPicker.groupGeminiFree');
        case 'xai':
            return uiText('components.common.ModelPicker.groupXai');
        case 'openrouter':
            return uiText('components.common.ModelPicker.groupOpenrouter');
        case 'nvidia_nim':
            return uiText('components.common.ModelPicker.groupNvidiaNim');
        case 'custom':
            return uiText('components.common.ModelPicker.groupCustom');
        case 'reflex':
            return uiText('components.common.ModelPicker.groupReflex');
        default:
            return fallback || key;
    }
}

function modelText(m: PickerModel): string {
    return m.config_error ? `${m.name} (${uiText('providerConfig.invalid')})` : m.name;
}

export default function ModelPicker({
    models,
    value,
    onChange,
    emptyLabel,
    unknownValueLabel,
    favorites,
    onToggleFavorite,
    disabled = false,
    ariaLabel,
    alwaysOpen = false,
}: ModelPickerProps) {
    useLocale();
    const panelId = useId();
    const triggerRef = useRef<HTMLButtonElement>(null);
    const [open, setOpen] = useState(false);
    // null のあいだは「いま選ばれているモデルのグループ (無ければ先頭)」を押された状態にする
    const [activeGroup, setActiveGroup] = useState<string | null>(null);
    // null のあいだは「いま選ばれているモデルが畳まれる側にあれば開く」
    const [othersOpen, setOthersOpen] = useState<boolean | null>(null);

    const expanded = alwaysOpen || open;
    const selected = value ? models.find(m => m.id === value) : undefined;
    const isUnknownValue = !!value && !selected;

    const groups = useMemo(() => {
        const byKey = new Map<string, GroupInfo>();
        models.forEach((m, index) => {
            const key = groupKeyOf(m);
            if (!byKey.has(key)) {
                byKey.set(key, {
                    key,
                    fallbackLabel: m.picker_group_label,
                    order: typeof m.picker_group_order === 'number' ? m.picker_group_order : Number.MAX_SAFE_INTEGER,
                    firstIndex: index,
                });
            }
        });
        return Array.from(byKey.values()).sort((a, b) => a.order - b.order || a.firstIndex - b.firstIndex);
    }, [models]);

    const selectedGroup = selected ? groupKeyOf(selected) : null;
    const defaultGroup = selectedGroup ?? groups[0]?.key ?? null;
    const shownGroup = activeGroup && groups.some(g => g.key === activeGroup) ? activeGroup : defaultGroup;
    const groupModels = shownGroup ? models.filter(m => groupKeyOf(m) === shownGroup) : [];
    const currentModels = groupModels.filter(isCurrent);
    const otherModels = groupModels.filter(m => !isCurrent(m));
    const selectedIsFolded = !!selected && !isCurrent(selected) && groupKeyOf(selected) === shownGroup;
    const showOthers = othersOpen ?? selectedIsFolded;

    // 一番上: いま選ばれているモデルとお気に入り (お気に入りは一覧の順番のまま)
    const favoriteSet = new Set(favorites ?? []);
    const topModels: PickerModel[] = [];
    if (selected && !favoriteSet.has(selected.id)) topModels.push(selected);
    if (favorites) {
        for (const m of models) {
            if (favoriteSet.has(m.id)) topModels.push(m);
        }
    }

    const resetView = () => {
        setActiveGroup(null);
        setOthersOpen(null);
    };

    const toggleOpen = () => {
        if (disabled) return;
        if (!open) resetView();
        setOpen(!open);
    };

    const choose = (id: string) => {
        onChange(id);
        if (!alwaysOpen) {
            setOpen(false);
            triggerRef.current?.focus();
        }
    };

    const handleKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        if (e.key === 'Escape' && open && !alwaysOpen) {
            e.stopPropagation();
            setOpen(false);
            triggerRef.current?.focus();
        }
    };

    const isFavorite = (id: string) => favoriteSet.has(id);

    const renderRow = (m: PickerModel, keyPrefix: string) => {
        const isSelected = m.id === value;
        return (
            <div key={`${keyPrefix}-${m.id}`} className={styles.row}>
                <button
                    type="button"
                    className={`${styles.option} ${isSelected ? styles.optionSelected : ''}`}
                    onClick={() => choose(m.id)}
                    disabled={disabled || !!m.config_error}
                    aria-current={isSelected ? 'true' : undefined}
                >
                    {modelText(m)}
                </button>
                {onToggleFavorite && (
                    <button
                        type="button"
                        data-i18n="components.common.ModelPicker.removeFavorite components.common.ModelPicker.addFavorite"
                        className={`${styles.favoriteBtn} ${isFavorite(m.id) ? styles.favoriteBtnActive : ''}`}
                        onClick={() => onToggleFavorite(m.id)}
                        disabled={disabled}
                        aria-pressed={isFavorite(m.id)}
                        aria-label={isFavorite(m.id)
                            ? uiText('components.common.ModelPicker.removeFavorite')
                            : uiText('components.common.ModelPicker.addFavorite')}
                        title={isFavorite(m.id)
                            ? uiText('components.common.ModelPicker.removeFavorite')
                            : uiText('components.common.ModelPicker.addFavorite')}
                    >
                        <Star size={16} fill={isFavorite(m.id) ? 'currentColor' : 'none'} />
                    </button>
                )}
            </div>
        );
    };

    // 複数のプロバイダをまとめるグループでは picker_heading が変わるところに見出しを入れる
    const renderRowsWithHeadings = (list: PickerModel[], keyPrefix: string) => {
        const out: React.ReactNode[] = [];
        let lastHeading: string | null = null;
        list.forEach((m, index) => {
            const heading = m.picker_heading || null;
            if (heading && heading !== lastHeading) {
                out.push(<div key={`${keyPrefix}-h-${index}`} className={styles.heading}>{heading}</div>);
            }
            lastHeading = heading;
            out.push(renderRow(m, keyPrefix));
        });
        return out;
    };

    const triggerText = (() => {
        if (selected) return modelText(selected);
        if (isUnknownValue) return `${unknownValueLabel ?? ''}${value}`;
        return emptyLabel ?? uiText('components.common.ModelPicker.notSelected');
    })();

    return (
        <div className={styles.root} onKeyDown={handleKeyDown}>
            {!alwaysOpen && (
                <button
                    ref={triggerRef}
                    type="button"
                    className={`${styles.trigger} ${open ? styles.triggerOpen : ''}`}
                    onClick={toggleOpen}
                    disabled={disabled}
                    aria-expanded={open}
                    aria-controls={panelId}
                    aria-label={ariaLabel ? `${ariaLabel}: ${triggerText}` : undefined}
                >
                    <span className={styles.triggerText}>{triggerText}</span>
                    <ChevronDown size={16} className={`${styles.chevron} ${open ? styles.chevronOpen : ''}`} />
                </button>
            )}
            {expanded && (
                <div
                    id={panelId}
                    className={styles.panel}
                    role="group"
                    aria-label={ariaLabel}
                >
                    {(topModels.length > 0 || isUnknownValue) && (
                        <div className={styles.topSection}>
                            {isUnknownValue && (
                                <div className={styles.row}>
                                    <button
                                        type="button"
                                        className={`${styles.option} ${styles.optionSelected}`}
                                        onClick={() => choose(value)}
                                        disabled={disabled}
                                        aria-current="true"
                                    >
                                        {`${unknownValueLabel ?? ''}${value}`}
                                    </button>
                                </div>
                            )}
                            {topModels.map(m => renderRow(m, 'top'))}
                        </div>
                    )}

                    {emptyLabel !== undefined && (
                        <div className={styles.row}>
                            <button
                                type="button"
                                className={`${styles.option} ${!value ? styles.optionSelected : ''}`}
                                onClick={() => choose('')}
                                disabled={disabled}
                                aria-current={!value ? 'true' : undefined}
                            >
                                {emptyLabel}
                            </button>
                        </div>
                    )}

                    {groups.length === 0 ? (
                        <div data-i18n="components.common.ModelPicker.noModels" className={styles.empty}>
                            {uiText('components.common.ModelPicker.noModels')}
                        </div>
                    ) : (
                        <>
                            <div
                                className={styles.groupButtons}
                                role="group"
                                aria-label={uiText('components.common.ModelPicker.providerGroupsLabel')}
                            >
                                {groups.map(g => (
                                    <button
                                        key={g.key}
                                        type="button"
                                        className={`${styles.groupBtn} ${g.key === shownGroup ? styles.groupBtnActive : ''}`}
                                        onClick={() => {
                                            setActiveGroup(g.key);
                                            setOthersOpen(null);
                                        }}
                                        disabled={disabled}
                                        aria-pressed={g.key === shownGroup}
                                    >
                                        {groupLabel(g.key, g.fallbackLabel)}
                                    </button>
                                ))}
                            </div>

                            <div className={styles.list}>
                                {renderRowsWithHeadings(currentModels, 'cur')}
                            </div>

                            {otherModels.length > 0 && (
                                <div className={styles.others}>
                                    <button
                                        type="button"
                                        data-i18n="components.common.ModelPicker.otherModels"
                                        className={styles.othersToggle}
                                        onClick={() => setOthersOpen(!showOthers)}
                                        aria-expanded={showOthers}
                                    >
                                        {showOthers ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                        <span>{uiText('components.common.ModelPicker.otherModels', { p1: otherModels.length })}</span>
                                    </button>
                                    {showOthers && (
                                        <div className={styles.list}>
                                            {renderRowsWithHeadings(otherModels, 'other')}
                                        </div>
                                    )}
                                </div>
                            )}
                        </>
                    )}
                </div>
            )}
        </div>
    );
}
