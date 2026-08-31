export type FuzzyMatch = {
  from: number;
  to: number;
  matchedText: string;
  similarity: number;
};

export const MISSING_SENTINEL = "【缺失该约定】";

const clausePrefixPattern =
  /^\s*(?:section\s+|article\s+)?(?:\(?\d+\)?|\(?[a-zA-Z]\)|[ivxlcdmIVXLCDM]+[.)]?|\d+(?:\.\d+)*[.)]?)\s*[:.)-]*\s*/i;

export function isMissingClause(originalText: string | undefined | null): boolean {
  if (!originalText) return true;
  const trimmed = originalText.trim();
  return trimmed === "" || trimmed === MISSING_SENTINEL || trimmed === "缺失该约定";
}

export function editDistance(s1: string, s2: string): number {
  const left = s1.toLowerCase();
  const right = s2.toLowerCase();
  const costs: number[] = [];

  for (let i = 0; i <= left.length; i += 1) {
    let lastValue = i;
    for (let j = 0; j <= right.length; j += 1) {
      if (i === 0) {
        costs[j] = j;
      } else if (j > 0) {
        let newValue = costs[j - 1];
        if (left.charAt(i - 1) !== right.charAt(j - 1)) {
          newValue = Math.min(Math.min(newValue, lastValue), costs[j]) + 1;
        }
        costs[j - 1] = lastValue;
        lastValue = newValue;
      }
    }
    if (i > 0) {
      costs[right.length] = lastValue;
    }
  }

  return costs[right.length];
}

export function getSimilarity(s1: string, s2: string): number {
  let longer = s1;
  let shorter = s2;
  if (s1.length < s2.length) {
    longer = s2;
    shorter = s1;
  }

  const longerLength = longer.length;
  if (longerLength === 0) {
    return 1;
  }

  return (longerLength - editDistance(longer, shorter)) / longerLength;
}

export function normalizeMatchText(text: string) {
  return text.toLowerCase().replace(/[\W_]+/g, " ").trim().replace(/\s+/g, " ");
}

export function stripClausePrefix(text: string) {
  return text.replace(clausePrefixPattern, "").trim();
}

export function extractHeadingCandidate(text: string) {
  const stripped = stripClausePrefix(text);
  if (!stripped) {
    return "";
  }

  const [heading] = stripped.split(/[.:\n;。；：]/, 1);
  return heading && heading.length <= 80 ? heading.trim() : "";
}

export function getParagraphMatchScore(paragraphText: string, query: string): number {
  const paragraphFull = normalizeMatchText(paragraphText);
  const queryFull = normalizeMatchText(query);

  if (!paragraphFull || !queryFull) {
    return 0;
  }

  if (paragraphFull === queryFull) {
    return 1;
  }

  if (paragraphFull.includes(queryFull)) {
    return 0.97;
  }

  const fullSimilarity = getSimilarity(paragraphFull, queryFull);
  const paragraphHeading = normalizeMatchText(extractHeadingCandidate(paragraphText));
  const queryHeading = normalizeMatchText(extractHeadingCandidate(query)) || queryFull;
  let headingSimilarity = 0;

  if (paragraphHeading) {
    if (paragraphHeading === queryHeading) {
      headingSimilarity = 0.96;
    } else if (paragraphHeading.includes(queryHeading) || queryHeading.includes(paragraphHeading)) {
      headingSimilarity = 0.93;
    } else {
      headingSimilarity = getSimilarity(paragraphHeading, queryHeading);
    }
  }

  return Math.max(fullSimilarity, headingSimilarity);
}

export function findFuzzyMatch(fullText: string, query: string, threshold = 0.8): FuzzyMatch | null {
  if (!query) {
    return null;
  }

  const exactIdx = fullText.indexOf(query);
  if (exactIdx >= 0) {
    return { from: exactIdx, to: exactIdx + query.length, matchedText: query, similarity: 1 };
  }

  const paragraphs = fullText.split("\n");
  let bestSim = 0;
  let bestParagraph = "";
  let currentOffset = 0;
  let bestOffset = -1;

  for (const paragraph of paragraphs) {
    const trimmed = paragraph.trim();
    if (trimmed.length > 0) {
      const sim = getParagraphMatchScore(trimmed, query);
      if (sim > bestSim) {
        bestSim = sim;
        bestParagraph = paragraph;
        bestOffset = currentOffset;
      }
    }
    currentOffset += paragraph.length + 1;
  }

  if (bestSim >= threshold && bestOffset >= 0) {
    return {
      from: bestOffset,
      to: bestOffset + bestParagraph.length,
      matchedText: bestParagraph,
      similarity: bestSim
    };
  }

  return null;
}

export type SavedModification = {
  original: string;
  modified: string;
  revision_id?: string;
  paragraph_context?: string | null;
  anchor_text?: string | null;
  insert_after_text?: string | null;
};

function escapeHtml(value: string) {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function splitParagraphs(text: string) {
  return text.split(/\r?\n/).map((paragraph) => paragraph.trim()).filter(Boolean);
}

function locateReplacement(paragraphs: string[], modification: SavedModification) {
  const context = modification.paragraph_context?.trim();
  if (context) {
    const contextMatches = paragraphs
      .map((paragraph, paragraphIndex) => ({ paragraph, paragraphIndex }))
      .filter(({ paragraph }) => paragraph === context || paragraph.includes(context));
    if (contextMatches.length === 1) {
      const index = contextMatches[0].paragraph.indexOf(modification.original);
      if (index >= 0) return { paragraphIndex: contextMatches[0].paragraphIndex, index };
    }
  }
  const matches: Array<{ paragraphIndex: number; index: number }> = [];
  paragraphs.forEach((paragraph, paragraphIndex) => {
    let index = paragraph.indexOf(modification.original);
    while (index >= 0) {
      matches.push({ paragraphIndex, index });
      index = paragraph.indexOf(modification.original, index + modification.original.length);
    }
  });
  return matches.length === 1 ? matches[0] : null;
}

export function applySavedModifications(sourceText: string, modifications: SavedModification[]) {
  const paragraphs = splitParagraphs(sourceText);
  const htmlParagraphs = paragraphs.length
    ? paragraphs.map((paragraph) => `<p>${escapeHtml(paragraph)}</p>`)
    : ["<p></p>"];
  const replacements: Array<{ paragraphIndex: number; index: number; original: string; modified: string; revisionId: string }> = [];
  const inserts: Array<{ afterIndex: number; text: string; revisionId: string }> = [];
  let appliedCount = 0;
  let skippedCount = 0;

  modifications.forEach((modification, index) => {
    const revisionId = modification.revision_id || `recovered-${index + 1}`;
    if (isMissingClause(modification.original)) {
      const anchor = (modification.insert_after_text || modification.anchor_text || "").trim();
      if (!anchor) {
        inserts.push({ afterIndex: Math.max(paragraphs.length - 1, -1), text: modification.modified, revisionId });
        appliedCount += 1;
        return;
      }
      const matches = paragraphs
        .map((paragraph, paragraphIndex) => ({ paragraph, paragraphIndex }))
        .filter(({ paragraph }) => paragraph === anchor || paragraph.includes(anchor));
      if (matches.length !== 1) {
        skippedCount += 1;
        return;
      }
      inserts.push({ afterIndex: matches[0].paragraphIndex, text: modification.modified, revisionId });
      appliedCount += 1;
      return;
    }

    const located = locateReplacement(paragraphs, modification);
    if (!located) {
      skippedCount += 1;
      return;
    }
    replacements.push({ ...located, original: modification.original, modified: modification.modified, revisionId });
    appliedCount += 1;
  });

  const byParagraph = new Map<number, typeof replacements>();
  for (const change of replacements) {
    const list = byParagraph.get(change.paragraphIndex) ?? [];
    list.push(change);
    byParagraph.set(change.paragraphIndex, list);
  }

  for (const [paragraphIndex, changes] of byParagraph) {
    const originalParagraph = paragraphs[paragraphIndex];
    const accepted: typeof changes = [];
    let occupiedUntil = -1;
    for (const change of [...changes].sort((left, right) => left.index - right.index || right.original.length - left.original.length)) {
      const end = change.index + change.original.length;
      if (change.index < occupiedUntil) {
        skippedCount += 1;
        appliedCount -= 1;
        continue;
      }
      accepted.push(change);
      occupiedUntil = end;
    }

    let textCursor = 0;
    let correctedParagraph = "";
    let revisionHtml = "<p>";
    for (const change of accepted) {
      const prefix = originalParagraph.slice(textCursor, change.index);
      correctedParagraph += prefix + change.modified;
      revisionHtml += `${escapeHtml(prefix)}<del class="del-mark" data-revision-id="${escapeHtml(change.revisionId)}">${escapeHtml(change.original)}</del><ins class="ins-mark" data-revision-id="${escapeHtml(change.revisionId)}">${escapeHtml(change.modified)}</ins>`;
      textCursor = change.index + change.original.length;
    }
    correctedParagraph += originalParagraph.slice(textCursor);
    revisionHtml += `${escapeHtml(originalParagraph.slice(textCursor))}</p>`;
    paragraphs[paragraphIndex] = correctedParagraph;
    htmlParagraphs[paragraphIndex] = revisionHtml;
  }

  for (const insert of [...inserts].sort((left, right) => right.afterIndex - left.afterIndex)) {
    const at = insert.afterIndex + 1;
    paragraphs.splice(at, 0, insert.text);
    htmlParagraphs.splice(
      at,
      0,
      `<p data-revision-id="${escapeHtml(insert.revisionId)}"><ins class="ins-mark" data-revision-id="${escapeHtml(insert.revisionId)}">${escapeHtml(insert.text)}</ins></p>`,
    );
  }

  return {
    correctedText: paragraphs.join("\n"),
    revisionHtml: htmlParagraphs.join(""),
    appliedCount,
    skippedCount,
  };
}

export function describeSourceDocxFailure(detail?: string | null) {
  const suffix = detail?.trim() ? `（${detail.trim()}）` : "";
  return `原始 Word 未能保存到共享工作区${suffix}。审查结果仍可用，同事导出审阅版前需要重新上传该文件。`;
}
