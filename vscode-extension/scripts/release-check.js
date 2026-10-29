#!/usr/bin/env node
// Run before every package and publish (package.json "vscode:prepublish"): the version being packaged must have its
// own dated entry at the top of CHANGELOG.md. On the branch the next version may read "## [1.4.0] - Unreleased" while
// it is being finished; nothing goes to a store like that.
'use strict';

const fs = require('fs');
const path = require('path');

/** Why `changelog` is not ready to ship `version`, or '' when it is. */
function releaseProblem(changelog, version) {
  const headings = [...changelog.matchAll(/^## \[([^\]]+)\](?: - (.*))?$/gm)].filter((m) => m[1] !== 'Unreleased');
  if (headings.length === 0) return 'CHANGELOG.md has no version entries.';
  const [, newest, when] = headings[0];
  if (newest !== version) return `The newest CHANGELOG.md entry is ${newest}, but package.json says ${version}.`;
  if (!/^\d{4}-\d{2}-\d{2}$/.test((when || '').trim())) {
    return `CHANGELOG.md still says "## [${version}] - ${(when || '').trim() || '(no date)'}": set the release date before packaging.`;
  }
  return '';
}

module.exports = { releaseProblem };

if (require.main === module) {
  const root = path.join(__dirname, '..');
  const { version } = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  const problem = releaseProblem(fs.readFileSync(path.join(root, 'CHANGELOG.md'), 'utf8'), version);
  if (problem) {
    console.error(`release check: ${problem}`);
    process.exit(1);
  }
}
