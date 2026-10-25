'use strict';
/**
 * What a machine contributes (dlne.worker.device) and how a CPU-only machine is brought up to
 * the coordinator's floor before it starts, instead of being refused with nothing on screen.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

require('./helpers/vscode-stub');
const {
  planContribution, parseRefusal, CPU_MIN_RAM_GB, CPU_OFFER_RAM_GB, REFUSAL_MARKER, CPU_BELOW_MINIMUM_EXIT_CODE,
} = require('../dist/worker/contribution');
const pkg = require('../package.json');

test('auto with a GPU contributes the GPU, as before', () => {
  assert.deepEqual(planContribution('auto', true, '', 4), { cpuOnly: false, allowedGpus: '', needsMoreRam: false });
  assert.deepEqual(planContribution(undefined, true, '0,1', 4), { cpuOnly: false, allowedGpus: '0,1', needsMoreRam: false });
});

test('auto without a GPU contributes the CPU and asks for RAM below the floor', () => {
  assert.deepEqual(planContribution('auto', false, '', 4), { cpuOnly: true, allowedGpus: '', needsMoreRam: true });
  assert.equal(planContribution('auto', false, '', 8).needsMoreRam, false);
  assert.equal(planContribution('auto', false, '', 7.9).needsMoreRam, true);
});

test('cpu hides the GPU from the engine even when there is one', () => {
  assert.deepEqual(planContribution('cpu', true, '0', 12), { cpuOnly: true, allowedGpus: 'none', needsMoreRam: false });
  assert.equal(planContribution('cpu', true, '', 4).needsMoreRam, true);
});

test('gpu without a GPU refuses to start, and says how to fix it', () => {
  const plan = planContribution('gpu', false, '', 12);
  assert.match(plan.error, /no GPU/);
  assert.match(plan.error, /"auto" or "cpu"/);
});

test('an unknown device setting behaves as auto', () => {
  assert.deepEqual(planContribution('tpu', true, '', 4), planContribution('auto', true, '', 4));
});

test('a missing or broken RAM setting counts as below the floor', () => {
  assert.equal(planContribution('cpu', false, '', NaN).needsMoreRam, true);
  assert.equal(planContribution('cpu', false, '', undefined).needsMoreRam, true);
});

test('the floor and the offer match the coordinator and the setting', () => {
  assert.equal(CPU_MIN_RAM_GB, 8);
  assert.ok(CPU_OFFER_RAM_GB >= CPU_MIN_RAM_GB);
  const device = pkg.contributes.configuration.properties['dlne.worker.device'];
  assert.deepEqual(device.enum, ['auto', 'gpu', 'cpu']);
  assert.equal(device.default, 'auto');
  const configPy = fs.readFileSync(path.join(__dirname, '..', '..', 'engine', 'config.py'), 'utf8');
  assert.match(configPy, /"cpu_min_ram_gb": 8\.0/);
});

test('the refusal marker and exit code match the worker', () => {
  const workerPy = fs.readFileSync(path.join(__dirname, '..', '..', 'engine', 'worker.py'), 'utf8');
  assert.match(workerPy, new RegExp(`REFUSAL_MARKER = "${REFUSAL_MARKER}"`));
  assert.match(workerPy, new RegExp(`CPU_BELOW_MINIMUM_EXIT_CODE = ${CPU_BELOW_MINIMUM_EXIT_CODE}\\b`));
});

test('the refusal reason is read out of the worker output', () => {
  const out = 'INFO something\nDLNE_REFUSAL: a CPU-only node needs 4 physical CPU cores (this machine has 2).\nmore\n';
  assert.equal(parseRefusal(out), 'a CPU-only node needs 4 physical CPU cores (this machine has 2).');
  assert.equal(parseRefusal('[12:00] DLNE_REFUSAL: reason with prefix\r\n'), 'reason with prefix');
  assert.equal(parseRefusal('nothing here'), null);
  assert.equal(parseRefusal('DLNE_REFUSAL:   '), null);
  assert.equal(parseRefusal(`DLNE_REFUSAL: ${'x'.repeat(1000)}`).length, 400);
});
