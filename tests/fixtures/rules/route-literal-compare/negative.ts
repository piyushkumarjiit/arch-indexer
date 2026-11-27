// SPDX-License-Identifier: FSL-1.1-ALv2
export function uiCompare(path: string, method: string) {
  if (path === "/api/fixture" && method === "GET") {
    return true;
  }
  return false;
}
