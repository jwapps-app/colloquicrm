import { useEffect, useState } from 'react';
import { Navigate, Route, Routes, useNavigate } from 'react-router-dom';
import { clearToken, get, getToken, post, setToken } from './api';
import { AuthContext, useAuth } from './auth';
import { APP_NAME } from './constants/branding';
import { ToastProvider } from './components/Toast';
import Layout from './components/Layout';
import Login from './pages/Login';
import Setup from './pages/Setup';
import Feed from './pages/Feed';
import PeopleList from './pages/PeopleList';
import PersonDetail from './pages/PersonDetail';
import LeadsList from './pages/LeadsList';
import LeadDetail from './pages/LeadDetail';
import CompaniesList from './pages/CompaniesList';
import CompanyDetail from './pages/CompanyDetail';
import OpportunitiesList from './pages/OpportunitiesList';
import OpportunityDetail from './pages/OpportunityDetail';
import TasksPage from './pages/TasksPage';
import ImportWizard from './pages/ImportWizard';
import Settings from './pages/Settings';

function Protected() {
  const { user } = useAuth();
  if (!getToken()) return <Navigate to="/login" replace />;
  if (!user) {
    return (
      <div className="app-loading">
        <div className="spinner" />
      </div>
    );
  }
  return <Layout />;
}

export default function App() {
  const nav = useNavigate();
  const [boot, setBoot] = useState(null);
  const [user, setUser] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    (async () => {
      let b = { needs_setup: false, app_name: '' };
      try {
        b = (await get('/auth/bootstrap')) || b;
      } catch {
        // Backend unreachable — fall through with defaults; requests will toast.
      }
      setBoot(b);
      if (!b.needs_setup && getToken()) {
        try {
          setUser(await get('/auth/me'));
        } catch {
          clearToken();
        }
      }
      setReady(true);
    })();
  }, []);

  const appName = boot?.app_name || APP_NAME;

  useEffect(() => {
    document.title = appName;
  }, [appName]);

  if (!ready) {
    return (
      <div className="app-loading">
        <div className="spinner" />
      </div>
    );
  }

  const auth = {
    user,
    setUser,
    appName,
    login: (token, u) => {
      setToken(token);
      setUser(u);
    },
    finishSetup: (token, u) => {
      setToken(token);
      setUser(u);
      setBoot((b) => ({ ...b, needs_setup: false }));
    },
    logout: async () => {
      try {
        await post('/auth/logout');
      } catch {
        // best-effort
      }
      clearToken();
      setUser(null);
      nav('/login', { replace: true });
    },
  };

  return (
    <AuthContext.Provider value={auth}>
      <ToastProvider>
        {boot?.needs_setup ? (
          <Routes>
            <Route path="/setup" element={<Setup />} />
            <Route path="*" element={<Navigate to="/setup" replace />} />
          </Routes>
        ) : (
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/setup" element={<Navigate to="/" replace />} />
            <Route element={<Protected />}>
              <Route path="/" element={<Feed />} />
              <Route path="/people" element={<PeopleList />} />
              <Route path="/people/:id" element={<PersonDetail />} />
              <Route path="/leads" element={<LeadsList />} />
              <Route path="/leads/:id" element={<LeadDetail />} />
              <Route path="/companies" element={<CompaniesList />} />
              <Route path="/companies/:id" element={<CompanyDetail />} />
              <Route path="/opportunities" element={<OpportunitiesList />} />
              <Route path="/opportunities/:id" element={<OpportunityDetail />} />
              <Route path="/tasks" element={<TasksPage />} />
              <Route path="/import" element={<ImportWizard />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Route>
          </Routes>
        )}
      </ToastProvider>
    </AuthContext.Provider>
  );
}
