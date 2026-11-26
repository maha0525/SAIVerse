// Unit test for frontend/scripts/sync-addon-panels.mjs:
// Verifies discovery and registry codegen for Panel.tsx, PersonaPanel.tsx,
// and combined UI bundles, plus file copying of companion CSS/TSX files.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

async function main() {
    const modUrl = pathToFileURL(path.resolve(__dirname, 'sync-addon-panels.mjs')).href;
    const { listAddonPanels, syncAddonPanels, buildRegistrySource } = await import(modUrl);

    const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'saiverse-sync-panels-'));
    try {
        const expansionDir = path.join(tmpRoot, 'expansion_data');
        const targetDir = path.join(tmpRoot, 'src', 'addon-panels');
        const registryFile = path.join(tmpRoot, 'src', 'addon-panels.generated.ts');

        // 1. Panel.tsx only
        fs.mkdirSync(path.join(expansionDir, 'addon-global-only', 'ui'), { recursive: true });
        fs.writeFileSync(
            path.join(expansionDir, 'addon-global-only', 'ui', 'Panel.tsx'),
            'export default function Panel() { return null; }\n',
        );

        // 2. PersonaPanel.tsx only (with companion theme CSS)
        fs.mkdirSync(path.join(expansionDir, 'addon-persona-only', 'ui'), { recursive: true });
        fs.writeFileSync(
            path.join(expansionDir, 'addon-persona-only', 'ui', 'PersonaPanel.tsx'),
            'export default function PersonaPanel() { return null; }\n',
        );
        fs.writeFileSync(
            path.join(expansionDir, 'addon-persona-only', 'ui', 'theme.module.css'),
            ':global(:root) { --x: #000; }\n',
        );

        // 3. Both Panel.tsx and PersonaPanel.tsx
        fs.mkdirSync(path.join(expansionDir, 'addon-both', 'ui'), { recursive: true });
        fs.writeFileSync(
            path.join(expansionDir, 'addon-both', 'ui', 'Panel.tsx'),
            'export default function Panel() { return null; }\n',
        );
        fs.writeFileSync(
            path.join(expansionDir, 'addon-both', 'ui', 'PersonaPanel.tsx'),
            'export default function PersonaPanel() { return null; }\n',
        );

        // 4. Addon with ui/ dir but no Panel.tsx / PersonaPanel.tsx (should be skipped)
        fs.mkdirSync(path.join(expansionDir, 'addon-ignored', 'ui'), { recursive: true });
        fs.writeFileSync(
            path.join(expansionDir, 'addon-ignored', 'ui', 'Helper.tsx'),
            'export const helper = 1;\n',
        );

        fs.mkdirSync(path.dirname(registryFile), { recursive: true });

        const discovered = listAddonPanels(expansionDir);
        assert.deepEqual(
            discovered.map((p) => ({
                name: p.name,
                hasPanel: p.hasPanel,
                hasPersonaPanel: p.hasPersonaPanel,
            })),
            [
                { name: 'addon-both', hasPanel: true, hasPersonaPanel: true },
                { name: 'addon-global-only', hasPanel: true, hasPersonaPanel: false },
                { name: 'addon-persona-only', hasPanel: false, hasPersonaPanel: true },
            ],
        );

        syncAddonPanels({
            expansionDir,
            targetDir,
            registryFile,
            frontendDir: tmpRoot,
        });

        // Companion CSS and PersonaPanel copied
        assert.ok(fs.existsSync(path.join(targetDir, 'addon-persona-only', 'PersonaPanel.tsx')));
        assert.ok(fs.existsSync(path.join(targetDir, 'addon-persona-only', 'theme.module.css')));
        assert.ok(!fs.existsSync(path.join(targetDir, 'addon-ignored')));

        // Generated registry contains both maps and props interfaces
        const generated = fs.readFileSync(registryFile, 'utf8');
        assert.ok(generated.includes('export interface AddonPanelProps'));
        assert.ok(generated.includes('export interface AddonPersonaPanelProps'));
        assert.ok(generated.includes('flushPendingSaves?: () => Promise<void>;'));
        assert.ok(generated.includes('configRevision?: number;'));
        assert.ok(generated.includes('getConfigRevision?: () => number;'));
        assert.ok(generated.includes('const addon_global_only_Panel = lazy(() => import("./addon-panels/addon-global-only/Panel"));'));
        assert.ok(generated.includes('const addon_persona_only_PersonaPanel = lazy(() => import("./addon-panels/addon-persona-only/PersonaPanel"));'));
        assert.ok(generated.includes('const addon_both_Panel = lazy(() => import("./addon-panels/addon-both/Panel"));'));
        assert.ok(generated.includes('const addon_both_PersonaPanel = lazy(() => import("./addon-panels/addon-both/PersonaPanel"));'));
        assert.ok(generated.includes('"addon-global-only": addon_global_only_Panel,'));
        assert.ok(!generated.includes('"addon-persona-only": addon_persona_only_Panel'));
        assert.ok(generated.includes('"addon-persona-only": addon_persona_only_PersonaPanel,'));
        assert.ok(generated.includes('"addon-both": addon_both_PersonaPanel,'));

        // Empty expansion_data still produces valid empty maps
        const emptySource = buildRegistrySource([]);
        assert.ok(emptySource.includes('export const ADDON_PANELS:'));
        assert.ok(emptySource.includes('export const ADDON_PERSONA_PANELS:'));

        console.log('test-sync-addon-panels: PASS');
    } finally {
        fs.rmSync(tmpRoot, { recursive: true, force: true });
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
