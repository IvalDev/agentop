import axe from 'axe-core';
import { expect } from 'vitest';

/**
 * Run axe over the whole document (landmarks, headings, labels, names, ARIA).
 * jsdom has no layout or canvas, so axe cannot measure color contrast there:
 * that rule is off here and contrast is covered by the palette in `styles/global.css`.
 */
export async function expectNoAxeViolations(): Promise<void> {
  const results = await axe.run(document, { rules: { 'color-contrast': { enabled: false } } });
  const summary = results.violations.map(
    (violation) => `${violation.id}: ${violation.nodes.map((node) => node.target.join(' ')).join(', ')}`,
  );
  expect(summary).toEqual([]);
}
