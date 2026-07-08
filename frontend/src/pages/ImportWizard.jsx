import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { get, post, upload } from '../api';
import { useToast } from '../components/Toast';
import { Loading } from '../components/ui';
import { IMPORT_TYPES } from '../constants/options';

const PREVIEW_PAGE = 100;

export default function ImportWizard() {
  const toast = useToast();
  const [params] = useSearchParams();
  const googleSource = params.get('source') === 'google';
  const [step, setStep] = useState(1);
  const [type, setType] = useState('people');
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [preview, setPreview] = useState(null);
  const [rows, setRows] = useState([]);
  const [page, setPage] = useState(1);
  const [result, setResult] = useState(null);
  const [progress, setProgress] = useState(null);
  const [dupsOnly, setDupsOnly] = useState(false);

  function applyPreview(p) {
    setPreview(p);
    setRows(
      (p.rows || []).map((r) => ({
        ...r,
        // second occurrence inside the same file is almost never wanted twice
        action: r.intra_file_duplicate_of != null ? 'skip' : 'create',
        merge_id: r.duplicates?.[0]?.id || '',
      }))
    );
    setPage(1);
    setStep(2);
  }

  async function doPreview(e) {
    e.preventDefault();
    if (!file) return;
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('type', type);
      applyPreview(await upload('/imports/preview', fd));
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  async function doGooglePreview(e) {
    e.preventDefault();
    setBusy(true);
    try {
      setType('people');
      applyPreview(await get('/integrations/google/contacts/preview'));
    } catch (err) {
      toast.error(err.message);
    }
    setBusy(false);
  }

  function bulk(action) {
    setRows((rs) => rs.map((r) => (r.duplicates?.length ? { ...r, action } : r)));
  }
  function skipIntraDupes() {
    setRows((rs) => rs.map((r) => (r.intra_file_duplicate_of != null ? { ...r, action: 'skip' } : r)));
  }
  function setRow(index, patchObj) {
    setRows((rs) => rs.map((r, i) => (i === index ? { ...r, ...patchObj } : r)));
  }

  async function commit() {
    setBusy(true);
    setStep(3);
    try {
      const body = {
        type,
        rows: rows.map((r) => ({
          action: r.action,
          ...(r.action === 'merge' && r.merge_id ? { merge_id: r.merge_id } : {}),
          data: r.data,
          tags: r.tags || [],
          custom_fields: r.custom_fields || {},
        })),
      };
      const { job_id } = await post('/imports/commit', body);
      // The import runs server-side; poll until it lands.
      let job = null;
      for (;;) {
        await new Promise((r) => setTimeout(r, 1500));
        job = await get(`/imports/jobs/${job_id}`);
        setProgress(job);
        if (job.status !== 'running') break;
      }
      if (job.status === 'failed') {
        throw new Error(job.error || 'Import failed');
      }
      setResult(job);
      toast.success('Import complete');
    } catch (err) {
      toast.error(err.message);
      setStep(2);
    }
    setBusy(false);
    setProgress(null);
  }

  function reset() {
    setStep(1);
    setFile(null);
    setPreview(null);
    setRows([]);
    setResult(null);
    setPage(1);
  }

  const dataCols = useMemo(() => (rows.length ? Object.keys(rows[0].data || {}) : []), [rows]);
  const dupCount = rows.filter((r) => r.duplicates?.length).length;
  const intraCount = rows.filter((r) => r.intra_file_duplicate_of != null).length;
  const counts = rows.reduce(
    (acc, r) => {
      acc[r.action] = (acc[r.action] || 0) + 1;
      return acc;
    },
    { create: 0, skip: 0, merge: 0 }
  );
  const isDup = (r) => r.duplicates?.length > 0 || r.intra_file_duplicate_of != null;
  const visible = rows.map((r, i) => ({ row: r, index: i })).filter(({ row }) => !dupsOnly || isDup(row));
  const totalPages = Math.max(1, Math.ceil(visible.length / PREVIEW_PAGE));
  const pageRows = visible.slice((page - 1) * PREVIEW_PAGE, page * PREVIEW_PAGE);

  return (
    <div className="page">
      <div className="page-head">
        <h1>Import</h1>
      </div>

      <div className="wizard-steps">
        {['Choose file', 'Review & resolve', 'Results'].map((label, i) => (
          <div key={label} className={'wizard-step' + (step === i + 1 ? ' active' : '') + (step > i + 1 ? ' done' : '')}>
            <span className="wizard-num">{i + 1}</span> {label}
          </div>
        ))}
      </div>

      {step === 1 && googleSource && (
        <form className="card import-start" onSubmit={doGooglePreview}>
          <h3>Import from Google Contacts</h3>
          <p className="muted">
            Pulls the contacts from your connected Google account as People, with the same duplicate
            review as a CSV import. Nothing is saved until you commit in step 3.
          </p>
          <div className="form-actions">
            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy ? 'Loading contacts…' : 'Load Google contacts'}
            </button>
          </div>
        </form>
      )}
      {step === 1 && !googleSource && (
        <form className="card import-start" onSubmit={doPreview}>
          <label className="field">
            <span>What are you importing?</span>
            <select value={type} onChange={(e) => setType(e.target.value)}>
              {IMPORT_TYPES.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>CSV file</span>
            <input type="file" accept=".csv,text/csv" onChange={(e) => setFile(e.target.files?.[0] || null)} required />
          </label>
          <p className="muted">
            The first row should contain column headers. Unrecognized headers become custom fields on commit.
          </p>
          <div className="form-actions">
            <button className="btn btn-primary" type="submit" disabled={!file || busy}>
              {busy ? 'Uploading…' : 'Preview import'}
            </button>
          </div>
        </form>
      )}

      {step === 2 && preview && (
        <>
          <div className="card import-summary">
            <div>
              <strong>{preview.total}</strong> rows parsed
              {dupCount + intraCount > 0 ? (
                <span className="badge badge-warn dup-headline">
                  ⚠ {dupCount + intraCount} possible duplicate{dupCount + intraCount === 1 ? '' : 's'}
                  {dupCount > 0 && ` — ${dupCount} match existing records`}
                  {intraCount > 0 && ` — ${intraCount} repeated within the file (defaulted to Skip)`}
                </span>
              ) : (
                <span className="muted"> · no duplicates detected</span>
              )}
            </div>
            {preview.unmapped_headers?.length > 0 && (
              <div className="import-warning">
                Unmapped headers (will be created as custom fields): {preview.unmapped_headers.join(', ')}
              </div>
            )}
            <div className="bulk-bar">
              <span className="muted">Rows with duplicates:</span>
              <button className="btn btn-small" onClick={() => bulk('skip')} disabled={dupCount === 0}>
                Skip all
              </button>
              <button className="btn btn-small" onClick={() => bulk('merge')} disabled={dupCount === 0}>
                Merge all
              </button>
              <button className="btn btn-small" onClick={() => bulk('create')} disabled={dupCount === 0}>
                Create anyway
              </button>
              {intraCount > 0 && (
                <button className="btn btn-small" onClick={skipIntraDupes}>
                  Skip in-file duplicates
                </button>
              )}
              {dupCount + intraCount > 0 && (
                <button
                  className={'btn btn-small' + (dupsOnly ? ' btn-primary' : '')}
                  onClick={() => {
                    setDupsOnly((v) => !v);
                    setPage(1);
                  }}
                >
                  {dupsOnly ? 'Show all rows' : `Show only duplicates (${rows.filter(isDup).length})`}
                </button>
              )}
            </div>
          </div>

          <div className="card table-card">
            <div className="table-wrap">
              <table className="table import-table">
                <thead>
                  <tr>
                    <th className="no-sort">#</th>
                    {dataCols.map((c) => (
                      <th key={c} className="no-sort">
                        {c}
                      </th>
                    ))}
                    <th className="no-sort">Tags</th>
                    <th className="no-sort">Duplicates</th>
                    <th className="no-sort">Action</th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.map(({ row: r, index: i }) => (
                    <tr key={i} className={'import-row' + (isDup(r) ? ' dup-row' : '')}>
                      <td className="muted">{i + 1}</td>
                      {dataCols.map((c) => (
                        <td key={c}>{r.data?.[c] ?? ''}</td>
                      ))}
                      <td>{(r.tags || []).join(', ')}</td>
                      <td>
                        {r.intra_file_duplicate_of != null && (
                          <span className="badge badge-warn">Dup of row {r.intra_file_duplicate_of + 1}</span>
                        )}
                        {(r.duplicates || []).map((d) => (
                          <span key={d.id} className="badge badge-dup" title={d.reason}>
                            {d.label} ({d.reason})
                          </span>
                        ))}
                      </td>
                      <td>
                        <select value={r.action} onChange={(e) => setRow(i, { action: e.target.value })}>
                          <option value="create">Create</option>
                          <option value="skip">Skip</option>
                          {r.duplicates?.length > 0 && <option value="merge">Merge</option>}
                        </select>
                        {r.action === 'merge' && r.duplicates?.length > 1 && (
                          <select value={r.merge_id} onChange={(e) => setRow(i, { merge_id: e.target.value })}>
                            {r.duplicates.map((d) => (
                              <option key={d.id} value={d.id}>
                                into: {d.label}
                              </option>
                            ))}
                          </select>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="table-foot">
              <span className="muted">
                Page {page} of {totalPages} ({rows.length} rows)
              </span>
              <div className="pager">
                <button className="btn btn-small" disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>
                  ‹ Prev
                </button>
                <button className="btn btn-small" disabled={page >= totalPages} onClick={() => setPage((p) => p + 1)}>
                  Next ›
                </button>
              </div>
            </div>
          </div>

          <div className="import-commit-bar">
            <span className="muted">
              Will create {counts.create}, merge {counts.merge}, skip {counts.skip}.
            </span>
            <div>
              <button className="btn" onClick={reset}>
                Cancel
              </button>{' '}
              <button className="btn btn-primary" onClick={commit} disabled={busy}>
                Import {counts.create + counts.merge} rows
              </button>
            </div>
          </div>
        </>
      )}

      {step === 3 &&
        (busy || !result ? (
          <div className="card">
            {progress && progress.total > 0 ? (
              <div className="import-progress">
                <div className="progress-track">
                  <div
                    className="progress-fill"
                    style={{ width: `${Math.round((progress.processed / progress.total) * 100)}%` }}
                  />
                </div>
                <p className="muted">
                  {progress.processed} of {progress.total} rows — {progress.created} created,{' '}
                  {progress.merged} merged, {progress.skipped} skipped. You can leave this page;
                  the import keeps running.
                </p>
              </div>
            ) : (
              <Loading label="Starting import…" />
            )}
          </div>
        ) : (
          <div className="card import-result">
            <h2>Import finished</h2>
            <div className="result-stats">
              <div className="result-stat">
                <strong>{result.created}</strong>
                <span className="muted">created</span>
              </div>
              <div className="result-stat">
                <strong>{result.merged}</strong>
                <span className="muted">merged</span>
              </div>
              <div className="result-stat">
                <strong>{result.skipped}</strong>
                <span className="muted">skipped</span>
              </div>
            </div>
            {result.custom_fields_created?.length > 0 && (
              <p className="muted">New custom fields created: {result.custom_fields_created.join(', ')}</p>
            )}
            <div className="form-actions">
              <button className="btn btn-primary" onClick={reset}>
                Import another file
              </button>
            </div>
          </div>
        ))}
    </div>
  );
}
