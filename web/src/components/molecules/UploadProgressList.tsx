import { uploadStateLabel, type UploadItem } from '../../knowledge/documents';
import { t } from '../../i18n';

const TITLE_ID = 'upload-progress-title';

/** Each file of the batch and how far it got. Not a live region: the page announces changes. */
export function UploadProgressList({ items }: { items: UploadItem[] }) {
  return (
    <div className="upload-progress">
      <h3 id={TITLE_ID} className="upload-progress__title">
        {t('knowledge.upload.progressTitle')}
      </h3>
      <ul className="upload-progress__list" aria-labelledby={TITLE_ID}>
        {items.map((item) => (
          <li key={item.id} className="upload-progress__item">
            <span className="upload-progress__name">{item.name}</span>
            <span className={`upload-progress__state upload-progress__state--${item.state}`}>
              {uploadStateLabel(item)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
