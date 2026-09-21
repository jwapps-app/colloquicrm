import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { get, post, upload } from '../api';
import { useToast } from '../components/Toast';
import { Loading } from '../components/ui';
import { useAuth } from '../auth';
import { IMPORT_TYPES } from '../constants/options';

const PREVIEW_PAGE = 100;

// The running import's job id, kept for this browser tab. The job lives on
// the server; without the id a reload or a dropped poll would leave the page
// with no way back to it — and a re-submit is how rows get duplicated.
const JOB_KEY = 'crm_import_job';
const POLL_MS = 1500;
const POLL_MAX_MS = 30000;

function readStoredJob() {
  try {
    const v = JSON.parse(sessionStorage.getItem(JOB_KEY));
    return v && typeof v.job_id === 'string' ? v : null;
  } catch {
    return null;
  }
}
function storeJob(job) {
  try {
    if (job) sessionStorage.setItem(JOB_KEY, JSON.stringify(job));
    else sessionStorage.removeItem(JOB_KEY);
  } catch {
    // storage unavailable — watching still works for as long as the page lives
  }
}

export default function ImportWizard() {
  const toast = useToast();
  const { user } = useAuth();
  const isAdmin = !!user?.is_admin;
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
  // Poll trouble while watching a job: { attempt, message } — shown inline,
  // never treated as the import failing.
  const [pollIssue, setPollIssue] = useState(null);
  // A job that ended in status 'failed' (the server's verdict, not a dropped poll).
  const [failedJob, setFailedJob] = useState(null);
  // A job from earlier in this tab, found on arriving at the page.
  const [earlier, setEarlier] = useState(null);
  // The commit request died without an answer — the server may have started it.
  const [unconfirmed, setUnconfirmed] = useState(false);
  const watchEpoch = useRef(0);

  // Guards the commit poll loop: cleared on unmount so it stops polling and
  // never sets state on an unmounted page. The import itself keeps running
  // server-side.
  const on = useRef(true);
  useEffect(() => {
    on.current = true;
    return () => {
      on.current = false;
    };
  }, []);

  // Coming back to the page: is an import from this tab still going?
  useEffect(() => {
    const stored = readStoredJob();
    if (!stored) return;
    get(`/imports/jobs/${stored.job_id}`)
      .then((job) => {
        if (on.current) setEarlier(job);
      })
      .catch((e) => {
        if (!on.current) return;
        if (e.status === 404) storeJob(null);
        // Unknown state: still offer to watch — watching retries by itself.
        else setEarlier({ job_id: stored.job_id, status: 'unknown', type: stored.type });
      });
  }, []);

  /** Follow a server-side import job to its end. Status checks are retried
   * with backoff for as long as the page is open: a failed poll says nothing
   * about the import, which keeps running either way. */
  async function watch(jobId, seed) {
    const epoch = ++watchEpoch.current;
    const live = () => on.current && epoch === watchEpoch.current;
    setStep(3);
    setBusy(true);
    setResult(null);
    setFailedJob(null);
    setEarlier(null);
    setPollIssue(null);
    if (seed?.total) setProgress(seed);
    let failures = 0;
    for (;;) {
      const delay = failures === 0 ? POLL_MS : Math.min(POLL_MAX_MS, POLL_MS * 2 ** failures);
      await new Promise((r) => setTimeout(r, delay));
      if (!live()) return;
      let job;
      try {
        job = await get(`/imports/jobs/${jobId}`);
      } catch (e) {
        if (!live()) return;
        if (e.status === 401) return; // signed out — api.js is redirecting
        if (e.status === 404) {
          // The server has no such job (any more): nothing left to watch.
          storeJob(null);
          setBusy(false);
          setProgress(null);
          setFailedJob({ job_id: jobId, missing: true, created: 0, merged: 0, skipped: 0, processed: 0, total: 0 });
          return;
        }
        failures += 1;
        setPollIssue({ attempt: failures, message: e.message });
        continue;
      }
      if (!live()) return;
      failures = 0;
      setPollIssue(null);
      setProgress(job);
      if (job.status === 'running') continue;
      storeJob(null);
      setBusy(false);
      setProgress(null);
      if (job.status === 'failed') {
        setFailedJob(job);
        toast.error(job.error || 'Import failed');
      } else {
        setResult(job);
        toast.success('Import complete');
      }
      return;
    }
  }

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
    if (
      unconfirmed &&
      !window.confirm(
        'The last attempt may have reached the server. If it did, importing again will create duplicate records. Import anyway?'
      )
    ) {
      return;
    }
    setBusy(true);
    setStep(3);
    let started;
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
      started = await post('/imports/commit', body);
    } catch (err) {
      if (!on.current) return;
      // Submitting is the only step that returns to review. An HTTP status
      // means the server refused and started nothing; no status means the
      // request died in transit and the import MAY be running.
      toast.error(err.message);
      setUnconfirmed(err.status === undefined);
      setBusy(false);
      setStep(2);
      return;
    }
    if (!on.current) return;
    setUnconfirmed(false);
    storeJob({ job_id: started.job_id, type });
    setProgress({ status: 'running', processed: 0, total: started.total, created: 0, merged: 0, skipped: 0 });
    // From here on the import is the server's; this page only watches it.
    await watch(started.job_id);
  }

  function reset() {
    setStep(1);
    setFile(null);
    setPreview(null);
    setRows([]);
    setResult(null);
    setFailedJob(null);
    setUnconfirmed(false);
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

      {step === 1 && earlier && (
        <div className="card import-resume" role="status">
          {earlier.status === 'running' || earlier.status === 'unknown' ? (
            <>
              <strong>An import you started is still running.</strong>{' '}
              <span className="muted">
                {earlier.total
                  ? `${earlier.processed} of ${earlier.total} rows so far. `
                  : 'Its progress couldn’t be read just now. '}
                Don&apos;t submit the same file again — that would duplicate its rows.
              </span>
            </>
          ) : earlier.status === 'failed' ? (
            <strong>Your last import stopped with an error.</strong>
          ) : (
            <>
              <strong>Your last import finished.</strong>{' '}
              <span className="muted">
                {earlier.created} created, {earlier.merged} merged, {earlier.skipped} skipped.
              </span>
            </>
          )}
          <div className="form-actions">
            <button
              className="btn"
              onClick={() => {
                storeJob(null);
                setEarlier(null);
              }}
            >
              Dismiss
            </button>
            <button className="btn btn-primary" onClick={() => watch(earlier.job_id, earlier)}>
              {earlier.status === 'running' || earlier.status === 'unknown' ? 'Watch progress' : 'View details'}
            </button>
          </div>
        </div>
      )}

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
            The first row should contain column headers.{' '}
            {isAdmin
              ? 'Columns the importer doesn’t recognize are created as custom fields when you commit.'
              : 'Columns the importer doesn’t recognize are imported only where a custom field with that name already exists — new fields have to be created by an administrator first. The review step lists anything that would be left out.'}
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
                {isAdmin
                  ? 'Unrecognized headers — will be created as custom fields: '
                  : 'Unrecognized headers — will be imported into the existing custom fields of the same name: '}
                {preview.unmapped_headers.join(', ')}
              </div>
            )}
            {preview.discarded_headers?.length > 0 && (
              <div className="import-warning import-discard" role="alert">
                <strong>These columns will NOT be imported:</strong> {preview.discarded_headers.join(', ')}. No custom
                field with that name exists, and only an administrator can create one. Ask an admin to create these
                fields first, then preview the file again — otherwise their values are left out.
              </div>
            )}
            {preview.discarded_pipelines?.length > 0 && (
              <div className="import-warning import-discard" role="alert">
                <strong>These pipelines / stages will NOT be imported:</strong>{' '}
                {preview.discarded_pipelines.join(', ')}. They don&apos;t exist yet and only an administrator can
                create them. The opportunities still import, but without that pipeline or stage — ask an admin to
                create these first if you need them placed.
              </div>
            )}
            {unconfirmed && (
              <div className="import-warning import-discard" role="alert">
                <strong>The last attempt got no answer from the server</strong> — it may have started anyway. Check
                the list for the new records before importing again; a second run would duplicate them.
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
        (failedJob ? (
          <div className="card import-result">
            <h2>{failedJob.missing ? 'Import not found' : 'Import stopped with an error'}</h2>
            {failedJob.missing ? (
              <p className="muted">
                The server no longer has this import, so its outcome can&apos;t be shown. Check the list for the new
                records before importing the file again.
              </p>
            ) : (
              <>
                <p className="form-error">{failedJob.error || 'The import failed.'}</p>
                <p>
                  It got through {failedJob.processed} of {failedJob.total} rows first:{' '}
                  <strong>
                    {failedJob.created} created, {failedJob.merged} merged, {failedJob.skipped} skipped
                  </strong>
                  . Those rows are already saved.
                </p>
                <p className="muted">
                  Importing the same file again would create them a second time. Remove the rows that already went in
                  (or choose Skip / Merge for them in the review step) before retrying.
                </p>
              </>
            )}
            <div className="form-actions">
              <button className="btn btn-primary" onClick={reset}>
                Start a new import
              </button>
            </div>
          </div>
        ) : busy || !result ? (
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
                  the import keeps running, and coming back here picks it up again.
                </p>
              </div>
            ) : (
              <Loading label={pollIssue ? 'Reconnecting to the import…' : 'Starting import…'} />
            )}
            {pollIssue && (
              <p className="import-warning" role="status">
                Can&apos;t reach the server right now ({pollIssue.message}) — still trying (attempt{' '}
                {pollIssue.attempt}). The import itself is not affected and keeps running; don&apos;t submit the file
                again.
              </p>
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
