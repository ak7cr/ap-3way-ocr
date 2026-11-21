const parse = async (response, path = '') => {
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }
  if (!response.ok) {
    const message = (payload && (payload.detail || payload.error)) || text || response.statusText;
    const textMessage = typeof message === 'string' ? message : JSON.stringify(message);
    if (response.status === 404 && /not found/i.test(textMessage) && path.startsWith('/api/')) {
      throw new Error(
        `${textMessage} — the UI is proxying to a different backend. ` +
          'Start the UI with API_PORT set to this app\'s API port, e.g. API_PORT=8010 WEB_PORT=5180 npm run dev:web'
      );
    }
    throw new Error(textMessage);
  }
  return payload;
};

export const api = {
  get: (path) => fetch(path).then((response) => parse(response, path)),
  post: (path, body) =>
    fetch(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body ?? {}),
    }).then((response) => parse(response, path)),
  postForm: (path, formData) =>
    fetch(path, { method: 'POST', body: formData }).then((response) => parse(response, path)),
  downloadBlob: async (path, filename) => {
    const response = await fetch(path);
    if (!response.ok) throw new Error(`Download failed (${response.status})`);
    downloadBlob(await response.blob(), filename);
  },
};

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

export const money = (value, currency = 'INR') => {
  const number = Number(value ?? 0);
  if (Number.isNaN(number)) return String(value ?? '—');
  return `${currency === 'INR' ? '₹' : currency + ' '}${number.toLocaleString('en-IN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
};

export const pct = (value, digits = 1) => `${(Number(value ?? 0) * 100).toFixed(digits)}%`;

export const reasonLabel = (code) => String(code || '').replaceAll('_', ' ').toLowerCase();
