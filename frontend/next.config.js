/**
 * Run `build` or `dev` with `SKIP_ENV_VALIDATION` to skip env validation. This is especially useful
 * for Docker builds.
 */
import "./src/env.js";

function getInternalServiceURL(envKey, fallbackURL) {
  const configured = process.env[envKey]?.trim();
  return configured && configured.length > 0
    ? configured.replace(/\/+$/, "")
    : fallbackURL;
}
import nextra from "nextra";

const withNextra = nextra({});

/** @type {import("next").NextConfig} */
const config = {
  i18n: {
    locales: ["en", "zh"],
    defaultLocale: "en",
  },
  devIndicators: false,
  allowedDevOrigins: process.env.NEXT_DEV_ALLOWED_ORIGINS
    ? process.env.NEXT_DEV_ALLOWED_ORIGINS.split(",")
        .map((s) => s.trim())
        .filter(Boolean)
    : [],
  async rewrites() {
    const beforeFiles = [];
    const langgraphURL = getInternalServiceURL(
      "DEER_FLOW_INTERNAL_LANGGRAPH_BASE_URL",
      "http://127.0.0.1:2024",
    );
    const gatewayURL = getInternalServiceURL(
      "DEER_FLOW_INTERNAL_GATEWAY_BASE_URL",
      "http://127.0.0.1:8001",
    );

    if (!process.env.NEXT_PUBLIC_LANGGRAPH_BASE_URL) {
      beforeFiles.push({
        source: "/api/langgraph",
        destination: langgraphURL,
      });
      beforeFiles.push({
        source: "/api/langgraph/:path*",
        destination: `${langgraphURL}/:path*`,
      });
    }

    // Auth endpoints: explicit v1/auth prefix only (deny-by-default)
    beforeFiles.push({
      source: "/api/v1/auth/:path*",
      destination: `${gatewayURL}/api/v1/auth/:path*`,
    });

    // LangGraph-compat: handled by route handler at /api/langgraph-compat/[...path]
    // with allowlist, header sanitization, and timeout — no rewrite needed.

    if (!process.env.NEXT_PUBLIC_BACKEND_BASE_URL) {
      // Explicit gateway API prefixes (deny-by-default, no catch-all)
      const GATEWAY_PREFIXES = [
        "agents",
        "models",
        "threads",
        "memory",
        "skills",
        "mcp",
      ];
      for (const prefix of GATEWAY_PREFIXES) {
        beforeFiles.push({
          source: `/api/${prefix}`,
          destination: `${gatewayURL}/api/${prefix}`,
        });
        beforeFiles.push({
          source: `/api/${prefix}/:path*`,
          destination: `${gatewayURL}/api/${prefix}/:path*`,
        });
      }
    }

    return { beforeFiles, afterFiles: [], fallback: [] };
  },
};

export default withNextra(config);
