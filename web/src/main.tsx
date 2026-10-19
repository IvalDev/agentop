import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router';
import { App } from './App';
import { getLocale, t } from './i18n';
import './styles/global.css';

document.documentElement.lang = getLocale();
document.title = t('app.name');

const container = document.getElementById('root');
if (container === null) {
  throw new Error('index.html must contain #root');
}

createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
