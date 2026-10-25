import * as vscode from 'vscode';
import * as path from 'path';

export interface DiffHunkLine {
  type: 'add' | 'del' | 'ctx' | 'header';
  text: string;
  oldLineNumber?: number;
  newLineNumber?: number;
}

export interface FileDiffData {
  path: string;
  action: 'created' | 'modified' | 'deleted';
  additions: number;
  deletions: number;
  lines: DiffHunkLine[];
  originalContent?: string;
  modifiedContent?: string;
}

export interface DiffResult {
  success: boolean;
  message: string;
  originalContent?: string;
  modifiedContent?: string;
  fileDiff?: FileDiffData;
}

export class DiffManager {
  /**
   * Applies a surgical search-and-replace block with indentation tolerance.
   */
  static applyFuzzyDiff(original: string, searchBlock: string, replaceBlock: string, filePath: string = ''): DiffResult {
    // 1. Direct exact match
    if (original.includes(searchBlock)) {
      const modified = original.replace(searchBlock, replaceBlock);
      const fileDiff = this.generateFileDiff(filePath, original, modified, 'modified');
      return {
        success: true,
        message: 'Exact match replaced',
        originalContent: original,
        modifiedContent: modified,
        fileDiff
      };
    }

    // 2. Line-by-line whitespace-trimmed fuzzy match
    const origLines = original.split(/\r?\n/);
    const searchLines = searchBlock.split(/\r?\n/).filter(l => l.length > 0);

    if (searchLines.length === 0) {
      return { success: false, message: 'Search block is empty' };
    }

    const trimmedSearch = searchLines.map(l => l.trim());
    let matchIndex = -1;

    for (let i = 0; i <= origLines.length - trimmedSearch.length; i++) {
      let matched = true;
      for (let j = 0; j < trimmedSearch.length; j++) {
        if (origLines[i + j].trim() !== trimmedSearch[j]) {
          matched = false;
          break;
        }
      }
      if (matched) {
        matchIndex = i;
        break;
      }
    }

    if (matchIndex === -1) {
      return {
        success: false,
        message: 'Could not find matching code block in file. Ensure context lines match the target file.'
      };
    }

    const replaceLines = replaceBlock.split(/\r?\n/);
    const newLines = [
      ...origLines.slice(0, matchIndex),
      ...replaceLines,
      ...origLines.slice(matchIndex + searchLines.length)
    ];

    const modified = newLines.join('\n');
    const fileDiff = this.generateFileDiff(filePath, original, modified, 'modified');

    return {
      success: true,
      message: 'Fuzzy match applied',
      originalContent: original,
      modifiedContent: modified,
      fileDiff
    };
  }

  /**
   * Generates a clean unified diff structure suitable for Claude Code / Antigravity styled display.
   */
  static generateFileDiff(
    filePath: string,
    original: string,
    modified: string,
    action: 'created' | 'modified' | 'deleted' = 'modified'
  ): FileDiffData {
    const origLines = original ? original.split(/\r?\n/) : [];
    const modLines = modified ? modified.split(/\r?\n/) : [];

    const lines: DiffHunkLine[] = [];
    let additions = 0;
    let deletions = 0;

    if (action === 'created' || origLines.length === 0) {
      lines.push({ type: 'header', text: `@@ +1,${modLines.length} @@` });
      for (let i = 0; i < modLines.length; i++) {
        lines.push({
          type: 'add',
          text: `+ ${modLines[i]}`,
          newLineNumber: i + 1
        });
        additions++;
      }
      return { path: filePath, action: 'created', additions, deletions: 0, lines, originalContent: original, modifiedContent: modified };
    }

    // Find leading common lines
    let startCommon = 0;
    while (
      startCommon < origLines.length &&
      startCommon < modLines.length &&
      origLines[startCommon] === modLines[startCommon]
    ) {
      startCommon++;
    }

    // Find trailing common lines
    let endCommonOrig = origLines.length - 1;
    let endCommonMod = modLines.length - 1;
    while (
      endCommonOrig >= startCommon &&
      endCommonMod >= startCommon &&
      origLines[endCommonOrig] === modLines[endCommonMod]
    ) {
      endCommonOrig--;
      endCommonMod--;
    }

    // Context padding (2 lines before and after)
    const ctxStart = Math.max(0, startCommon - 2);
    const ctxEndOrig = Math.min(origLines.length, endCommonOrig + 3);
    const ctxEndMod = Math.min(modLines.length, endCommonMod + 3);

    const oldRangeLen = ctxEndOrig - ctxStart;
    const newRangeLen = ctxEndMod - ctxStart;

    lines.push({
      type: 'header',
      text: `@@ -${ctxStart + 1},${oldRangeLen} +${ctxStart + 1},${newRangeLen} @@`
    });

    // Before context
    for (let i = ctxStart; i < startCommon; i++) {
      lines.push({
        type: 'ctx',
        text: `  ${origLines[i]}`,
        oldLineNumber: i + 1,
        newLineNumber: i + 1
      });
    }

    // Deleted lines
    for (let i = startCommon; i <= endCommonOrig; i++) {
      lines.push({
        type: 'del',
        text: `- ${origLines[i]}`,
        oldLineNumber: i + 1
      });
      deletions++;
    }

    // Added lines
    for (let i = startCommon; i <= endCommonMod; i++) {
      lines.push({
        type: 'add',
        text: `+ ${modLines[i]}`,
        newLineNumber: i + 1
      });
      additions++;
    }

    // After context
    for (let i = endCommonOrig + 1; i < ctxEndOrig; i++) {
      const modIdx = endCommonMod + 1 + (i - (endCommonOrig + 1));
      lines.push({
        type: 'ctx',
        text: `  ${origLines[i]}`,
        oldLineNumber: i + 1,
        newLineNumber: modIdx + 1
      });
    }

    return {
      path: filePath,
      action,
      additions,
      deletions,
      lines,
      originalContent: original,
      modifiedContent: modified
    };
  }

  /**
   * Write file safely to workspace with diff review capability.
   */
  static async applyToFile(relativeOrAbsolutePath: string, newContent: string): Promise<boolean> {
    const workspaceFolders = vscode.workspace.workspaceFolders;
    if (!workspaceFolders || workspaceFolders.length === 0) {
      throw new Error('No open workspace');
    }

    const fullPath = path.isAbsolute(relativeOrAbsolutePath)
      ? relativeOrAbsolutePath
      : path.join(workspaceFolders[0].uri.fsPath, relativeOrAbsolutePath);

    const uri = vscode.Uri.file(fullPath);
    const encoder = new TextEncoder();
    await vscode.workspace.fs.writeFile(uri, encoder.encode(newContent));
    return true;
  }
}
