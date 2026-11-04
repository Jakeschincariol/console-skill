const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const root = __dirname;
const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
};

http.createServer((request, response) => {
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
  } catch {
    response.writeHead(400).end("Bad request");
    return;
  }

  const filePath = path.resolve(root, `.${pathname === "/" ? "/index.html" : pathname}`);
  if (!filePath.startsWith(`${root}${path.sep}`)) {
    response.writeHead(403).end("Forbidden");
    return;
  }

  fs.readFile(filePath, (error, content) => {
    if (error) {
      response.writeHead(error.code === "ENOENT" ? 404 : 500).end("File unavailable");
      return;
    }
    response.writeHead(200, {
      "Content-Type": contentTypes[path.extname(filePath)] ?? "application/octet-stream",
    });
    response.end(content);
  });
}).listen(8000, "127.0.0.1", () => {
  console.log("Vale do Orvalho: http://127.0.0.1:8000");
});
