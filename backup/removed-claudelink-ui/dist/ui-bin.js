#!/usr/bin/env node
"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
// Entry point for the detached UI server process.
const ui_server_js_1 = require("./ui-server.js");
const port = parseInt(process.argv[2] || "7878", 10);
(0, ui_server_js_1.startUIServer)(port);
// Keep stderr quiet (parent stdio is "ignore" anyway, but in case someone runs this directly).
console.error(`ClaudeLink UI listening on http://127.0.0.1:${port}`);
//# sourceMappingURL=ui-bin.js.map