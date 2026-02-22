import { MemoryImportProgress, MemoryImportRequest, NativePreviewData, PreviewData, ThreadSummary } from '@/components/memory/types';

async function parseJson<T>(res: Response): Promise<T> {
  return (await res.json()) as T;
}

export async function fetchThreads(personaId: string): Promise<ThreadSummary[]> {
  const res = await fetch(`/api/people/${personaId}/threads`);
  return res.ok ? parseJson<ThreadSummary[]>(res) : [];
}

export async function activateThread(personaId: string, threadId: string): Promise<void> {
  await fetch(`/api/people/${personaId}/threads/${encodeURIComponent(threadId)}/activate`, { method: 'PUT' });
}

export async function previewOfficialImport(personaId: string, file: File): Promise<{ ok: boolean; data: PreviewData | { detail?: string } }> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`/api/people/${personaId}/import/official/preview`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function importOfficial(personaId: string, request: MemoryImportRequest): Promise<{ ok: boolean; data: { detail?: string } }> {
  const res = await fetch(`/api/people/${personaId}/import/official`, {
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
  const res = await fetch(`/api/people/${personaId}/import/extension`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function previewNativeImport(personaId: string, file: File): Promise<{ ok: boolean; data: NativePreviewData | { detail?: string } }> {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`/api/people/${personaId}/import/native/preview`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function importNative(personaId: string, file: File, skipEmbedding: boolean): Promise<{ ok: boolean; data: { detail?: string } }> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('skip_embedding', String(skipEmbedding));
  const res = await fetch(`/api/people/${personaId}/import/native`, { method: 'POST', body: formData });
  return { ok: res.ok, data: await parseJson(res) };
}

export async function getImportStatus(personaId: string, type: 'extension' | 'official' | 'native'): Promise<MemoryImportProgress> {
  const res = await fetch(`/api/people/${personaId}/import/${type}/status`);
  return parseJson<MemoryImportProgress>(res);
}

export async function startReembed(personaId: string, force: boolean): Promise<{ success?: boolean; detail?: string; message?: string }> {
  const res = await fetch(`/api/people/${personaId}/reembed`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ force }),
  });
  return parseJson(res);
}

export async function getReembedStatus(personaId: string): Promise<MemoryImportProgress> {
  const res = await fetch(`/api/people/${personaId}/reembed/status`);
  return parseJson<MemoryImportProgress>(res);
}
