import fs from "fs";
import path from "path";

import type { NextRequest } from "next/server";

function getArtifactContentType(filePath: string) {
  switch (path.extname(filePath).toLowerCase()) {
    case ".html":
    case ".htm":
      return "text/html; charset=utf-8";
    case ".css":
      return "text/css; charset=utf-8";
    case ".js":
    case ".mjs":
      return "application/javascript; charset=utf-8";
    case ".json":
      return "application/json; charset=utf-8";
    case ".svg":
      return "image/svg+xml; charset=utf-8";
    case ".png":
      return "image/png";
    case ".jpg":
    case ".jpeg":
      return "image/jpeg";
    case ".gif":
      return "image/gif";
    case ".webp":
      return "image/webp";
    case ".mp4":
      return "video/mp4";
    default:
      return "application/octet-stream";
  }
}

export async function GET(
  request: NextRequest,
  {
    params,
  }: {
    params: Promise<{
      thread_id: string;
      artifact_path?: string[] | undefined;
    }>;
  },
) {
  const threadId = (await params).thread_id;
  let artifactPath = (await params).artifact_path?.join("/") ?? "";
  if (artifactPath.startsWith("mnt/")) {
    artifactPath = path.resolve(
      process.cwd(),
      artifactPath.replace("mnt/", `public/demo/threads/${threadId}/`),
    );
    if (fs.existsSync(artifactPath)) {
      const contentType = getArtifactContentType(artifactPath);
      if (request.nextUrl.searchParams.get("download") === "true") {
        // Attach the file to the response
        const headers = new Headers();
        headers.set(
          "Content-Disposition",
          `attachment; filename="${path.basename(artifactPath)}"`,
        );
        headers.set("Content-Type", contentType);
        return new Response(fs.readFileSync(artifactPath), {
          status: 200,
          headers,
        });
      }
      const headers = new Headers();
      headers.set("Content-Type", contentType);
      return new Response(fs.readFileSync(artifactPath), {
        status: 200,
        headers,
      });
    }
  }
  return new Response("File not found", { status: 404 });
}
