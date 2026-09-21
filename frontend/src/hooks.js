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
