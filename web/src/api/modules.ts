import type { ApiClient } from './client';

/** One catalog module with the caller tenant's activation flag (`ModuleResponse`). */
export interface CatalogModule {
  key: string;
  name: string;
  description: string;
  version: string;
  permissions: string[];
  tools: string[];
  active: boolean;
}

/** The resulting state of activating or deactivating a module (`ModuleActivationResponse`). */
export interface ModuleActivation {
  key: string;
  active: boolean;
}

interface ModuleBody {
  key: string;
  name: string;
  description: string;
  version: string;
  permissions: string[];
  tools: string[];
  active: boolean;
}

interface ModuleActivationBody {
  key: string;
  active: boolean;
}

function toModule(body: ModuleBody): CatalogModule {
  return {
    key: body.key,
    name: body.name,
    description: body.description,
    version: body.version,
    permissions: [...body.permissions],
    tools: [...body.tools],
    active: body.active,
  };
}

function toActivation(body: ModuleActivationBody): ModuleActivation {
  return { key: body.key, active: body.active };
}

/** `GET /api/v1/modules`: the catalog in registration order with this tenant's activation flags. */
export async function listModules(api: ApiClient): Promise<CatalogModule[]> {
  const body = await api.get<{ modules: ModuleBody[] }>('/api/v1/modules');
  return body.modules.map(toModule);
}

/** `POST /api/v1/modules/{key}/activate`: the resulting `{key, active}` state (idempotent). */
export async function activateModule(api: ApiClient, key: string): Promise<ModuleActivation> {
  const path = `/api/v1/modules/${encodeURIComponent(key)}/activate`;
  return toActivation(await api.post<ModuleActivationBody>(path));
}

/** `DELETE /api/v1/modules/{key}/activate`: deactivation is the removal of the activation (idempotent). */
export async function deactivateModule(api: ApiClient, key: string): Promise<ModuleActivation> {
  const path = `/api/v1/modules/${encodeURIComponent(key)}/activate`;
  return toActivation(await api.request<ModuleActivationBody>(path, { method: 'DELETE' }));
}
