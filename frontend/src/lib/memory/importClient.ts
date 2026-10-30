
import { apiFetch } from '@/i18n/api';
import {
  ClaudeImportRequest,
  ClaudeMemoryImportResult,
  ClaudePreviewData,
  ClaudePreviewErrorDetail,
  MemoryImportProgress,
  MemoryImportRequest,
  NativePreviewData,
  PreviewData,
  ThreadSummary,
} from '@/components/memory/types';

async function parseJson<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

export async function fetchThreads(personaId: string): Promise<ThreadSummary[]> {
  const res = await apiFetch(`/api/people/${personaId}/threads`);
  return res.ok ? parseJson<ThreadSummary[]>(res) : [];
}

export async function activateThread(personaId: string, threadId: string): Promise<void> {
  await apiFetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}/activate`, { method: 'PUT' });
}

export async function previewOfficialImport(personaId: string, file: File): Promise<{ ok: boolean; data: PreviewData | { detail?: string } }> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await apiFetch(`/api/people/${personaId}/import/official/preview`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function importOfficial(personaId: string, request: MemoryImportRequest): Promise<{ ok: boolean; data: { detail?: string } }> {
  const res = await apiFetch(`/api/people/${personaId}/import/official`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function importExtension(personaId: string, file: File, skipEmbedding: boolean): Promise<{ ok: boolean; data: { detail?: string } }> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('skip_embedding', String(skipEmbedding));
  const res = await apiFetch(`/api/people/${personaId}/import/extension`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function previewNativeImport(personaId: string, file: File): Promise<{ ok: boolean; data: NativePreviewData | { detail?: string } }> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await apiFetch(`/api/people/${personaId}/import/native/preview`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function importNative(personaId: string, file: File, skipEmbedding: boolean): Promise<{ ok: boolean; data: { detail?: string } }> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('skip_embedding', String(skipEmbedding));
  const res = await apiFetch(`/api/people/${personaId}/import/native`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function previewClaudeImport(
  personaId: string,
  file: File,
  expectedCategory?: string,
): Promise<{ ok: boolean; data: ClaudePreviewData | { detail?: string | ClaudePreviewErrorDetail } }> {
  const formData = new FormData();
  formData.append('file', file);
  if (expectedCategory) formData.append('expected_category', expectedCategory);
  const res = await apiFetch(`/api/people/${personaId}/import/claude/preview`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function importClaude(personaId: string, request: ClaudeImportRequest): Promise<{ ok: boolean; data: { detail?: string } }> {
  const res = await apiFetch(`/api/people/${personaId}/import/claude`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function importClaudeMemories(
  personaId: string,
  cacheKey: string,
  itemIds: string[],
): Promise<{ ok: boolean; data: Partial<ClaudeMemoryImportResult> & { detail?: string } }> {
  const res = await apiFetch(`/api/people/${personaId}/import/claude/memories`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cache_key: cacheKey, item_ids: itemIds }),
  });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function getImportStatus(personaId: string, type: 'extension' | 'official' | 'native' | 'claude'): Promise<MemoryImportProgress> {
  const res = await apiFetch(`/api/people/${personaId}/import/${type}/status`);
  return parseJson<MemoryImportProgress>(res);
}

export async function startReembed(personaId: string, force: boolean): Promise<{ success?: boolean; detail?: string; message?: string }> {
  const res = await apiFetch(`/api/people/${personaId}/reembed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force }),
  });
  return parseJson(res);
}

export async function getReembedStatus(personaId: string): Promise<MemoryImportProgress> {
  const res = await apiFetch(`/api/people/${personaId}/reembed/status`);
  return parseJson<MemoryImportProgress>(res);
}
