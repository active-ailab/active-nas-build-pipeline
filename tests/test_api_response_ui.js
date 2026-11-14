const fs = require('fs');
const vm = require('vm');
const path = require('path');
const assert = require('assert');
const source = fs.readFileSync(path.join(__dirname, '../static/js/index.js'), 'utf8');
const ctx = {};
vm.createContext(ctx);
vm.runInContext(source.slice(source.indexOf('async function readApiJsonResponse('), source.indexOf('let currentStep = 1;')), ctx);
const response = (body, status = 200) => ({status, ok: status >= 200 && status < 300, text: async () => body});
(async () => {
  assert.strictEqual((await ctx.readApiJsonResponse(response('{"ok":true,"task_id":"offline"}'))).task_id, 'offline');
  assert.strictEqual((await ctx.readApiJsonResponse(response('{"ok":false,"error":"config error"}', 400))).error, 'config error');
  await assert.rejects(ctx.readApiJsonResponse(response('<!doctype html><title>Error</title>', 500)), /HTML.*HTTP 500/);
  await assert.rejects(ctx.readApiJsonResponse(response('Bad gateway', 502)), /HTTP 502/);
  await assert.rejects(ctx.readApiJsonResponse(response('null')), /HTTP 200/);
  const save = source.slice(source.indexOf('async function nextStep7()'));
  assert(save.includes('const result = await readApiJsonResponse(resp);'));
  console.log('API error UI checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
