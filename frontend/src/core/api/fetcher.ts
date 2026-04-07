import { buildLoginUrl } from "@/core/auth/types";

/**
 * Fetch with credentials. Automatically redirects to login on 401.
 */
export async function fetchWithAuth(
  input: RequestInfo | string,
  init?: RequestInit,
): Promise<Response> {
  const url = typeof input === "string" ? input : input.url;
  const res = await fetch(url, {
    ...init,
    credentials: "include",
  });

  if (res.status === 401) {
    window.location.href = buildLoginUrl(window.location.pathname);
    throw new Error("Unauthorized");
  }

  return res;
}

/**
 * Build headers for CSRF-protected requests
 * Per RFC-001: Double Submit Cookie pattern
 */
export function getCsrfHeaders(): HeadersInit {
  const token = getCsrfToken();
  return token ? { "X-CSRF-Token": token } : {};
}

/**
 * Get CSRF token from cookie
 */
function getCsrfToken(): string | null {
  const match = /csrf_token=([^;]+)/.exec(document.cookie);
  return match?.[1] ?? null;
}
