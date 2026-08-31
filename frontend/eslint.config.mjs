// ESLint flat config (Next.js 16 では `next lint` が削除されたため eslint を直接実行する)
// eslint-config-next v16 はフラット設定の配列をネイティブに export している。
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const config = [
    {
        ignores: [
            ".next/**",
            "node_modules/**",
            "public/**",
            // sync-addon-panels.mjs が expansion_data/ からコピー・生成するファイル
            // (アドオン作者側のコードなので本体の lint 対象にしない)
            "src/addon-panels/**",
            "src/addon-panels.generated.ts",
            "next-env.d.ts",
        ],
    },
    ...nextCoreWebVitals,
    ...nextTypescript,
    {
        rules: {
            // 既存コードベース全域に散在するため warn に降格。
            // 新規コードでは解消を推奨 (エラー 0 のベースラインを維持するための措置)。
            "@typescript-eslint/no-explicit-any": "warn",
            // eslint-plugin-react-hooks v7 で追加された React Compiler 由来の検査群。
            // 既存コードへの指摘が多いため warn 運用にする。
            "react-hooks/set-state-in-effect": "warn",
            "react-hooks/immutability": "warn",
            "react-hooks/static-components": "warn",
            "react-hooks/refs": "warn",
            "react-hooks/purity": "warn",
        },
    },
];

export default config;
