import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router';
import { useAuth } from '../auth';
import { useToast } from '../components/Toast';
import AutomationsSection from '../components/AutomationsSection';
import FormsSection from '../components/FormsSection';
import TrashSection from './settings/TrashSection';
import ProfileSection from './settings/ProfileSection';
import SecuritySection from './settings/SecuritySection';
import SessionsSection from './settings/SessionsSection';
import CustomFieldsSection from './settings/CustomFieldsSection';
import UsersSection from './settings/UsersSection';
import IntegrationsSection from './settings/IntegrationsSection';

/* ---------- Page ---------- */

const GOOGLE_RESULT_MESSAGES = {
  connected: null, // success toast
  denied: 'Google access was denied.',
  state_error: 'The sign-in link expired — try connecting again.',
  not_configured: 'The Google OAuth client is not configured.',
  exchange_error: 'Google rejected the token exchange — check the client ID and secret.',
  no_refresh_token: 'Google did not return an offline token — try again (you may need to remove the app at myaccount.google.com/permissions first).',
  sync_running:
    'A Google sync is running right now, so the account wasn’t switched. Give it a minute to finish, then connect again.',
};

export default function Settings() {
  const { user } = useAuth();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState(
    searchParams.get('tab') || (searchParams.get('google') ? 'integrations' : 'profile')
  );

  useEffect(() => {
    const result = searchParams.get('google');
    if (!result) return;
    if (result === 'connected') {
      toast.success('Google account connected');
    } else {
      toast.error(GOOGLE_RESULT_MESSAGES[result] || `Google connection failed (${result}).`);
    }
    setSearchParams({}, { replace: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tabs = [
    { id: 'profile', label: 'Profile' },
    { id: 'security', label: 'Security' },
    { id: 'fields', label: 'Custom Fields' },
    ...(user?.is_admin ? [{ id: 'users', label: 'Users' }] : []),
    ...(user?.is_admin ? [{ id: 'automations', label: 'Automations' }] : []),
    ...(user?.is_admin ? [{ id: 'forms', label: 'Forms' }] : []),
    { id: 'integrations', label: 'Integrations' },
    { id: 'trash', label: 'Trash' },
  ];

  return (
    <div className="page page-narrow">
      <div className="page-head">
        <h1>Settings</h1>
      </div>
      <div className="tabs settings-tabs">
        {tabs.map((t) => (
          <button key={t.id} className={'tab' + (tab === t.id ? ' active' : '')} onClick={() => setTab(t.id)}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === 'profile' && <ProfileSection />}
      {tab === 'security' && (
        <>
          <SecuritySection />
          <SessionsSection />
        </>
      )}
      {tab === 'fields' && <CustomFieldsSection />}
      {tab === 'users' && user?.is_admin && <UsersSection />}
      {tab === 'automations' && user?.is_admin && <AutomationsSection />}
      {tab === 'forms' && user?.is_admin && <FormsSection />}
      {tab === 'integrations' && <IntegrationsSection />}
      {tab === 'trash' && <TrashSection />}
    </div>
  );
}
