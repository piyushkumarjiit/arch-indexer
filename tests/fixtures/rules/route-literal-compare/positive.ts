// SPDX-License-Identifier: FSL-1.1-ALv2
import { createServer } from "node:http";

function handle(req: any, res: any, path: string, method: string) {
  if (path === "/api/fixture" && method === "GET") {
    res.end("ok");
  }
}

export function startServer() {
  return createServer((req, res) => handle(req, res, req.url, req.method));
}
