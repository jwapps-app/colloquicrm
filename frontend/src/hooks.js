import { useEffect, useRef, useState } from 'react';
import { cachedGet, get, patch } from './api';
import { useToast } from './components/Toast';

// Load one entity + provide an inline-save helper that PATCHes and merges.
export function useEntity(apiPath, id) {
  const toast = useToast();
  const [entity, setEntity] = useState(null);
  const [error, setError] = useState(null);
  const [version, setVersion] = useState(0);

  // Which record this hook instance currently shows. Detail components are
  // reused across route-id changes, so a slow PATCH for record A can land
  // after the hook has moved on to B — its response must not be merged into
  // B. Bumped synchronously during render, the moment apiPath/id change.
  const generation = useRef(0);
  const target = `${apiPath}/${id}`;
  const lastTarget = useRef(target);
  if (lastTarget.current !== target) {
    lastTarget.current = target;
    generation.current += 1;
  }
  // Saves apply in the order they were requested, whatever order the
  // responses arrive in: each save waits for the one before it.
  const saveChain = useRef(Promise.resolve());

  useEffect(() => {
    let on = true;
    setEntity(null);
    setError(null);
    get(`${apiPath}/${id}`)
      .then((e) => {
        if (on) setEntity(e);
      })
      .catch((e) => {
        if (on) setError(e.message);
      });
    return () => {
      on = false;
    };
  }, [apiPath, id, version]);

  function save(body) {
    const gen = generation.current;
    const url = `${apiPath}/${id}`;
    const run = async () => {
      // The user already left this record: a queued edit for it is still sent
      // (they made it), but nothing about it may touch the new record's view.
      try {
        const updated = await patch(url, body);
        if (gen !== generation.current) return;
        setEntity((prev) =>
          prev
            ? {
                ...prev,
                ...body,
                ...(updated && typeof updated === 'object' && !Array.isArray(updated) ? updated : {}),
              }
            : prev
        );
      } catch (e) {
        toast.error(e.message);
      }
    };
    const next = saveChain.current.then(run);
    saveChain.current = next;
    return next;
  }

  const refresh = () => setVersion((v) => v + 1);
  return { entity, save, error, refresh };
}

/**
 * Load something a component shows, and say honestly how that went.
 *
 *   const { data, error, loading, reload } = useLoad(() => get('/things'), [id]);
 *
 * `data` is null until the first load for the current `deps` succeeds, then
 * the loaded value; it is kept across reload(), so a refresh after a save
 * doesn't blank the list. `error` is the Error of the latest load when it
 * failed (null otherwise) — a failed fetch is never presented as "empty".
 * `loading` is true while a request is in flight. A response that arrives
 * after `deps` changed (or after unmount) is dropped.
 *
 * `load` may return anything; shape the response inside it. To treat a
 * particular failure as a value (a 404 meaning "feature absent"), catch it
 * in `load` and return that value.
 */
export function useLoad(load, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  const loadedFor = useRef(null);

  useEffect(() => {
    let on = true;
    // New deps mean a different thing is being shown: drop the old one.
    // A reload of the same thing keeps what is on screen until it lands.
    const key = JSON.stringify(deps);
    const fresh = loadedFor.current !== key;
    loadedFor.current = key;
    setState((s) => ({ data: fresh ? null : s.data, error: null, loading: true }));
    Promise.resolve()
      .then(load)
      .then((data) => {
        if (on) setState({ data, error: null, loading: false });
      })
      .catch((error) => {
        if (on) setState((s) => ({ data: s.data, error, loading: false }));
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tick, ...deps]);

  const reload = () => setTick((t) => t + 1);
  const setData = (next) =>
    setState((s) => ({ ...s, data: typeof next === 'function' ? next(s.data) : next }));
  return { ...state, reload, setData };
}

export const RELATED_PAGE_SIZE = 100;

// Load a related list for a detail page (first RELATED_PAGE_SIZE rows).
// `deps` controls when it refetches — typically the parent entity id.
// `items` is null while loading, the rows on success, or the string 'error'
// when the fetch failed — so a failure doesn't masquerade as an empty list.
// `total` is the server's full count, so the panel can say when it is showing
// only part of the list.
export function useRelated(apiPath, params, deps) {
  const [state, setState] = useState({ items: null, total: 0 });
  useEffect(() => {
    let on = true;
    setState({ items: null, total: 0 });
    get(apiPath, { ...params, page: 1, page_size: RELATED_PAGE_SIZE })
      .then((d) => {
        const items = d?.items || [];
        if (on) setState({ items, total: Math.max(Number(d?.total) || 0, items.length) });
      })
      .catch(() => {
        if (on) setState({ items: 'error', total: 0 });
      });
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return state;
}

export function useUsers() {
  const [users, setUsers] = useState([]);
  useEffect(() => {
    cachedGet('/users')
      .then((d) => setUsers(d && d.items ? d.items : []))
      .catch(() => {});
  }, []);
  return users;
}

export function useContactTypes() {
  // Data-driven: whatever contact types exist in the org (plus defaults),
  // shaped as {value,label} options for selects.
  const [types, setTypes] = useState([]);
  useEffect(() => {
    cachedGet('/options/contact-types')
      .then((d) => setTypes((Array.isArray(d) ? d : []).map((v) => ({ value: v, label: v }))))
      .catch(() => {});
  }, []);
  return types;
}

export function usePipelines() {
  const [pipelines, setPipelines] = useState([]);
  useEffect(() => {
    cachedGet('/pipelines')
      .then((d) => setPipelines(Array.isArray(d) ? d : []))
      .catch(() => {});
  }, []);
  return pipelines;
}
