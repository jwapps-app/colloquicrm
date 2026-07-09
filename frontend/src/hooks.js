import { useEffect, useState } from 'react';
import { get, patch } from './api';
import { useToast } from './components/Toast';

// Load one entity + provide an inline-save helper that PATCHes and merges.
export function useEntity(apiPath, id) {
  const toast = useToast();
  const [entity, setEntity] = useState(null);
  const [error, setError] = useState(null);
  const [version, setVersion] = useState(0);

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

  async function save(body) {
    try {
      const updated = await patch(`${apiPath}/${id}`, body);
      setEntity((prev) => ({
        ...prev,
        ...body,
        ...(updated && typeof updated === 'object' && !Array.isArray(updated) ? updated : {}),
      }));
    } catch (e) {
      toast.error(e.message);
    }
  }

  const refresh = () => setVersion((v) => v + 1);
  return { entity, setEntity, save, error, refresh };
}

export function useUsers() {
  const [users, setUsers] = useState([]);
  useEffect(() => {
    get('/users')
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
    get('/options/contact-types')
      .then((d) => setTypes((Array.isArray(d) ? d : []).map((v) => ({ value: v, label: v }))))
      .catch(() => {});
  }, []);
  return types;
}

export function usePipelines() {
  const [pipelines, setPipelines] = useState([]);
  useEffect(() => {
    get('/pipelines')
      .then((d) => setPipelines(Array.isArray(d) ? d : []))
      .catch(() => {});
  }, []);
  return pipelines;
}
