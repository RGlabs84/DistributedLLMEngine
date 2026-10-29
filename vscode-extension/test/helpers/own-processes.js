'use strict';
/**
 * The processes a test started itself, found by a marker: a `sleep` time no other process has ("sleep 54.12371"), and
 * the shells that run it.
 *
 * Never `pgrep -f <marker>` with a kill after it. pgrep searches every process on the machine with a regular expression,
 * so in "59.12377" the dot matches any character; on 2026-10-28 at 14:21 one such marker matched 16 of VS Code's own
 * helper processes (their command lines share long strings of digits), and the test's clean-up SIGKILLed all of them:
 * every window, the terminals and the extensions went down. Here the marker is matched as the literal argument of a
 * `sleep`, in this user's processes, and nothing else can match it. scripts/run-tests.js also runs the tests in a PID
 * namespace of their own, where nothing but the tests can be seen.
 */
const childProcess = require('child_process');

const escaped = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** The ids of this user's processes whose command line runs `sleep <marker>`, the test's own process left out. */
function ownProcesses(marker) {
  if (!/^\d+(\.\d+)?$/.test(marker)) throw new Error(`a marker is a sleep time, not ${JSON.stringify(marker)}`);
  const runsIt = new RegExp(`(^|[\\s;&|(])sleep ${escaped(marker)}(?=$|[\\s;&|)])`);
  let listing;
  try {
    listing = childProcess.execFileSync('ps', ['-U', String(process.getuid()), '-o', 'pid=,args='], { encoding: 'utf8' });
  } catch {
    return [];
  }
  return listing.split('\n').map((line) => /^\s*(\d+)\s(.*)$/.exec(line)).filter(Boolean)
    .filter(([, pid, args]) => Number(pid) !== process.pid && runsIt.test(args))
    .map(([, pid]) => pid);
}

/** SIGKILLs what ownProcesses(marker) finds: a test's clean-up. */
function killOwn(marker) {
  for (const pid of ownProcesses(marker)) {
    try { process.kill(Number(pid), 'SIGKILL'); } catch { /* gone */ }
  }
}

module.exports = { ownProcesses, killOwn };
