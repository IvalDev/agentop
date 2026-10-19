import { esES, type Messages } from './es-ES';

/** Supported locales; `es-ES` is the only one for now and the default. */
export type Locale = 'es-ES';
export const DEFAULT_LOCALE: Locale = 'es-ES';

const catalogs: Record<Locale, Messages> = { 'es-ES': esES };
const activeLocale: Locale = DEFAULT_LOCALE;

/** Dot paths to every string in the catalog, e.g. `"login.errors.network"`. */
type Leaves<T> = {
  [K in keyof T & string]: T[K] extends string ? K : `${K}.${Leaves<T[K]>}`;
}[keyof T & string];

export type MessageKey = Leaves<Messages>;
export type MessageParams = Record<string, string | number>;

export function getLocale(): Locale {
  return activeLocale;
}

/** Translate `key` in the active locale, filling `{name}` placeholders from `params`. */
export function t(key: MessageKey, params?: MessageParams): string {
  let node: unknown = catalogs[activeLocale];
  for (const part of key.split('.')) {
    node = (node as Record<string, unknown>)[part];
  }
  const template = typeof node === 'string' ? node : key;
  if (!params) {
    return template;
  }
  return template.replace(/\{(\w+)\}/g, (placeholder, name: string) => {
    const value = params[name];
    if (value === undefined) {
      return placeholder;
    }
    return typeof value === 'number' ? formatNumber(value) : value;
  });
}

export function formatNumber(value: number, options?: Intl.NumberFormatOptions): string {
  return new Intl.NumberFormat(activeLocale, options).format(value);
}

export function formatDate(value: Date | string | number, options?: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(activeLocale, options ?? { dateStyle: 'medium' }).format(new Date(value));
}
