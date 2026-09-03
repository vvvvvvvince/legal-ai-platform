import { formatApiErrorDetail } from "./errorDetails";

export type SessionIdentity = {
  user_id: string;
  username: string;
  phone: string;
  display_name: string;
  workspace_id: string;
  is_admin: boolean;
};

export type OperationLog = {
  username: string;
  display_name: string;
  action: string;
  detail: string;
  occurred_at: string;
};

export type ModelConfig = { active_model: string; allowed_models: string[] };
export type ModelProfileInput = { display_name: string; model_id: string; base_url: string; api_key: string };

async function readError(response: Response, fallback: string): Promise<Error> {
  const payload = await response.json().catch(() => null) as { detail?: unknown } | null;
  return new Error(formatApiErrorDetail(payload?.detail, fallback));
}

export async function login(username: string, phone: string, password: string): Promise<SessionIdentity> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ username, phone, password }),
  });
  if (!response.ok) throw await readError(response, "登录失败，请检查用户名和密码。");
  return await response.json() as SessionIdentity;
}

export async function getSession(): Promise<SessionIdentity | null> {
  const response = await fetch("/api/auth/session", { credentials: "same-origin" });
  if (response.status === 401) return null;
  if (!response.ok) throw await readError(response, "无法读取登录状态。");
  return await response.json() as SessionIdentity;
}

export async function logout(): Promise<void> {
  await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
}

export async function getOperationLogs(): Promise<OperationLog[]> {
  const response = await fetch("/api/admin/operation-logs", { credentials: "same-origin" });
  if (!response.ok) throw await readError(response, "无法读取操作日志。");
  return await response.json() as OperationLog[];
}

export async function getModelConfig(): Promise<ModelConfig> {
  const response = await fetch("/api/admin/model-config", { credentials: "same-origin" });
  if (!response.ok) throw await readError(response, "无法读取模型配置。");
  return await response.json() as ModelConfig;
}

export async function updateModelConfig(model: string): Promise<ModelConfig> {
  const response = await fetch("/api/admin/model-config", {
    method: "PUT", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
    body: JSON.stringify({ model }),
  });
  if (!response.ok) throw await readError(response, "模型切换失败。");
  return await response.json() as ModelConfig;
}

export async function addModelProfile(profile: ModelProfileInput): Promise<ModelConfig> {
  const response = await fetch("/api/admin/model-config/profiles", { method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin", body: JSON.stringify(profile) });
  if (!response.ok) throw await readError(response, "新增模型失败。");
  return await response.json() as ModelConfig;
}

export async function getCurrentModel(): Promise<string> {
  const response = await fetch("/api/model", { credentials: "same-origin" });
  if (!response.ok) throw await readError(response, "无法读取当前模型。");
  return (await response.json() as { active_model: string }).active_model;
}
