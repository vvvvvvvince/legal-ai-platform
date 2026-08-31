export type SessionIdentity = {
  user_id: string;
  username: string;
  display_name: string;
  workspace_id: string;
};

async function readError(response: Response, fallback: string): Promise<Error> {
  const payload = await response.json().catch(() => null) as { detail?: string } | null;
  return new Error(payload?.detail ?? fallback);
}

export async function login(username: string, password: string): Promise<SessionIdentity> {
  const response = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ username, password }),
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
