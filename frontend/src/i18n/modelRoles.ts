import { t } from './core';

export function getModelRoleLabel(role: string, fallback?: string): string {
    switch (role) {
        case 'default_model':
            return t('modelRole.default_model.label');
        case 'lightweight_model':
            return t('modelRole.lightweight_model.label');
        case 'memory_weave_model':
            return t('modelRole.memory_weave_model.label');
        case 'image_summary_model':
            return t('modelRole.image_summary_model.label');
        case 'audio_summary_model':
            return t('modelRole.audio_summary_model.label');
        case 'video_summary_model':
            return t('modelRole.video_summary_model.label');
        default:
            return fallback || role;
    }
}

export function getModelRoleDescription(role: string, fallback?: string): string {
    switch (role) {
        case 'default_model':
            return t('modelRole.default_model.desc');
        case 'lightweight_model':
            return t('modelRole.lightweight_model.desc');
        case 'memory_weave_model':
            return t('modelRole.memory_weave_model.desc');
        case 'image_summary_model':
            return t('modelRole.image_summary_model.desc');
        case 'audio_summary_model':
            return t('modelRole.audio_summary_model.desc');
        case 'video_summary_model':
            return t('modelRole.video_summary_model.desc');
        default:
            return fallback || '';
    }
}

export function getProviderPresetDisplayName(provider: string, fallback?: string): string {
    switch (provider) {
        case 'openai':
            return t('modelPreset.openai');
        case 'gemini_free':
            return t('modelPreset.gemini_free');
        case 'gemini':
            return t('modelPreset.gemini');
        case 'anthropic':
            return t('modelPreset.anthropic');
        case 'grok':
            return t('modelPreset.grok');
        case 'openrouter':
            return t('modelPreset.openrouter');
        case 'openrouter_free':
            return t('modelPreset.openrouter_free');
        case 'nvidia':
        case 'nvidia_nim':
            return t('modelPreset.nvidia');
        case 'ollama':
            return t('modelPreset.ollama');
        case 'llama_cpp':
            return t('modelPreset.llama_cpp');
        default:
            return fallback || provider;
    }
}

export function getWatermarkPresetLabel(presetId: string, fallback?: string): string {
    switch (presetId) {
        case 'large':
            return t('components.GlobalSettingsModal.presetLarge');
        case 'default':
            return t('components.GlobalSettingsModal.presetDefault');
        case 'small':
            return t('components.GlobalSettingsModal.presetSmall');
        default:
            return fallback || presetId;
    }
}
