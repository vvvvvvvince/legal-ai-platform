import { useCallback, useEffect, useState } from "react";
import { getSession, login, logout, type SessionIdentity } from "../api/authApi";

export function useAuth() {
  const [identity, setIdentity] = useState<SessionIdentity | null>(null);
  const [isReady, setIsReady] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void getSession().then(setIdentity).catch((reason: unknown) => {
      setError(reason instanceof Error ? reason.message : "无法读取登录状态。");
    }).finally(() => setIsReady(true));
  }, []);

  const signIn = useCallback(async (username: string, phone: string, password: string) => {
    setError(null);
    try {
      const next = await login(username, phone, password);
      setIdentity(next);
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "登录失败。";
      setError(message);
      throw reason;
    }
  }, []);

  const signOut = useCallback(async () => {
    await logout();
    setIdentity(null);
  }, []);

  return { identity, isReady, error, signIn, signOut };
}
