/** Convert FastAPI's string, object, or validation-list details to UI text. */
export function formatApiErrorDetail(detail: unknown, fallback: string): string {
  if (typeof detail === "string" && detail.trim()) return detail.trim();

  if (Array.isArray(detail)) {
    const messages = detail.flatMap((item) => {
      if (typeof item === "string") return item;
      if (!item || typeof item !== "object") return [];
      const source = item as Record<string, unknown>;
      const message = typeof source.msg === "string" ? source.msg : source.message;
      return typeof message === "string" && message.trim() ? message.trim() : [];
    });
    if (messages.length) return messages.join("；");
  }

  if (detail && typeof detail === "object") {
    const source = detail as Record<string, unknown>;
    const message = typeof source.message === "string" ? source.message : source.msg;
    if (typeof message === "string" && message.trim()) return message.trim();
  }
  return fallback;
}
