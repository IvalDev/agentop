import { describe, expect, it } from 'vitest';
import { formatDate, formatNumber, getLocale, t } from '.';

describe('i18n', () => {
  it('uses Spain Spanish by default', () => {
    expect(getLocale()).toBe('es-ES');
    expect(t('header.logout')).toBe('Cerrar sesión');
  });

  it('fills placeholders and formats numbers for the locale', () => {
    expect(t('app.pageTitle', { page: 'Chat', app: 'Asistente' })).toBe('Chat · Asistente');
    expect(t('app.pageTitle', { page: 12345.5, app: 'X' })).toBe('12.345,5 · X');
  });

  it('leaves unknown placeholders visible instead of dropping them', () => {
    expect(t('app.pageTitle', { page: 'Chat' })).toBe('Chat · {app}');
  });

  it('formats numbers and dates with Intl for es-ES', () => {
    expect(formatNumber(1234567.891, { maximumFractionDigits: 2 })).toBe('1.234.567,89');
    expect(formatDate('2026-10-14T12:00:00Z', { dateStyle: 'long', timeZone: 'UTC' })).toBe('14 de octubre de 2026');
  });
});
