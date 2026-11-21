import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import DocumentsPage from './components/DocumentsPage';
import ReviewQueue from './components/ReviewQueue';
import ReceiptEntry from './components/ReceiptEntry';
import Approvals from './components/Approvals';
import Metrics from './components/Metrics';
import './styles.css';

const SECTIONS = [
  { key: 'documents', label: 'Documents' },
  { key: 'review', label: 'Review' },
  { key: 'receipts', label: 'Receipts' },
  { key: 'approvals', label: 'Approvals' },
  { key: 'metrics', label: 'Metrics' },
];

function App() {
  const [section, setSection] = useState('documents');

  return (
    <main className="min-h-screen bg-slate-950 text-slate-100">
      <div className="mx-auto max-w-7xl px-6 py-8">
        <header className="mb-7 flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-white">3-Way Match · PO ↔ GRN ↔ Invoice</h1>
            <p className="text-sm text-slate-400">
              Upload any mix of documents. Extractions are judged by arithmetic and format checks, and every decision is
              made by deterministic rules — with a human in the loop while the system earns trust.
            </p>
          </div>
          <nav className="flex flex-wrap gap-1 rounded-xl border border-slate-800 bg-slate-900/70 p-1">
            {SECTIONS.map((entry) => (
              <button
                key={entry.key}
                onClick={() => setSection(entry.key)}
                className={`rounded-lg px-3.5 py-2 text-xs font-semibold transition ${
                  section === entry.key
                    ? 'bg-cyan-400 text-slate-950'
                    : 'text-slate-400 hover:bg-slate-800/60 hover:text-slate-200'
                }`}
              >
                {entry.label}
              </button>
            ))}
          </nav>
        </header>

        {section === 'documents' && <DocumentsPage />}
        {section === 'review' && <ReviewQueue />}
        {section === 'receipts' && <ReceiptEntry />}
        {section === 'approvals' && <Approvals />}
        {section === 'metrics' && <Metrics />}
      </div>
    </main>
  );
}

createRoot(document.getElementById('root')).render(<App />);
 