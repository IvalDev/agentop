import axe from 'axe-core';
import { expect } from 'vitest';

/**
 * Timeout for a test that runs axe over a full page (tables included). Both axe and role
 * queries are CPU-bound in jsdom: about 0.5 s on an idle machine, but well over the 5 s default
 * when the machine is busy, so these tests get more time instead of failing at random.
 */
export const AXE_TEST_TIMEOUT_MS = 15_000;

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
