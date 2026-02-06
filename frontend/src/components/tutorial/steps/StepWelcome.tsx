"use client";

import React from 'react';
import styles from './Steps.module.css';

export default function StepWelcome() {
    return (
        <div className={styles.welcomeContainer}>
            {/* Logo placeholder - can be replaced with actual logo */}
            <div className={styles.logoArea}>
                <img
                    src="/api/media/icon/saiverse-logo.png"
                    alt="SAIVerse"
                    className={styles.logo}
                    onError={(e) => {
                        // Hide if logo not found
                        (e.target as HTMLImageElement).style.display = 'none';
                    }}
                />
            </div>

            <h2 className={styles.welcomeTitle}>SAIVerseへようこそ</h2>

            <div className={styles.descriptionBox}>
                <p>ここは<strong>SAIVerse</strong>。</p>
                <p>人間とAIが共に生きられる空間です。</p>
                <br />
                <p>あなたのAIは<strong>ペルソナ</strong>と呼ばれる個人として</p>
                <p>SAIVerse上に存在します。</p>
            </div>

            <p className={styles.hint}>
                このセットアップでは、あなたのSAIVerse環境を初期設定します。
            </p>
        </div>
    );
}
