// Offline UI checks: all platforms expose both modes and submit the selected mode.
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../static/js/index.js'), 'utf8');
const section = source.slice(source.indexOf('function toggleBuildReleaseModeVisibility()'), source.indexOf('function handleBuildReleaseModeChange()'));
for (const platform of ['MHS003', 'MHS003S', 'NXP595/Apollo4/Apollo5']) {
  for (const mode of ['single', 'split']) {
    let visible = false;
    const ctx = {selectedPlatform: platform, handleBuildReleaseModeChange() {}, document: {
      getElementById() {return {classList: {remove(name) {visible = name === 'hidden';}}};},
      querySelector() {return {value: mode};}
    }};
    vm.createContext(ctx);
    vm.runInContext(section, ctx);
    ctx.toggleBuildReleaseModeVisibility();
    assert(visible, platform);
    assert.strictEqual(ctx.isBuildReleaseSingle(), mode === 'single', platform);
    assert.strictEqual(ctx.getBuildReleaseMode(), mode, platform);
  }
}
assert(!source.includes('isMhsPlatform'));
assert.strictEqual((source.match(/const build_release_mode = getBuildReleaseMode\(\);/g) || []).length, 2);
console.log('Platform trigger mode UI checks passed.');
