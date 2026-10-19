import js from '@eslint/js';
import { defineConfig } from 'eslint/config';
import jsxA11y from 'eslint-plugin-jsx-a11y';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default defineConfig(
  { ignores: ['dist', 'node_modules'] },
  js.configs.recommended,
  tseslint.configs.recommended,
  jsxA11y.flatConfigs.strict,
  reactHooks.configs.flat.recommended,
  {
    languageOptions: {
      globals: globals.browser,
    },
    rules: {
      // Security rules: the token never reaches localStorage, server text is never HTML.
      'no-restricted-properties': [
        'error',
        { object: 'window', property: 'localStorage', message: 'Never persist the token in localStorage.' },
      ],
      'no-restricted-globals': [
        'error',
        { name: 'localStorage', message: 'Never persist the token in localStorage.' },
      ],
      'no-restricted-syntax': [
        'error',
        {
          selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
          message: 'Render server-provided text as text, never as HTML.',
        },
      ],
    },
  },
);
