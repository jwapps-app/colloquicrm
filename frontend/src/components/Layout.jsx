import { useState } from 'react';
import { Link, NavLink, Outlet } from 'react-router-dom';
import { useAuth } from '../auth';
import Icon from './Icon';
import GlobalSearch from './GlobalSearch';

const NAV = [
  { to: '/', icon: 'feed', label: 'Feed', end: true },
  { to: '/people', icon: 'people', label: 'People' },
  { to: '/leads', icon: 'leads', label: 'Leads' },
  { to: '/companies', icon: 'companies', label: 'Companies' },
  { to: '/opportunities', icon: 'opportunities', label: 'Opportunities' },
  { to: '/tasks', icon: 'tasks', label: 'Tasks' },
  { to: '/import', icon: 'import', label: 'Import' },
  { to: '/settings', icon: 'settings', label: 'Settings' },
];

export default function Layout() {
  const { user, appName, logout } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);

  const initials = (user?.display_name || user?.email || '?')
    .split(/[\s@.]+/)
    .filter(Boolean)
    .map((w) => w[0])
    .slice(0, 2)
    .join('')
    .toUpperCase();

  return (
    <div className="layout">
      <aside className={'sidebar' + (mobileOpen ? ' open' : '')}>
        <div className="sidebar-brand" title={appName}>
          <span className="brand-dot" />
          <span className="side-label">{appName}</span>
        </div>
        <nav className="sidebar-nav">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.end}
              className={({ isActive }) => 'side-link' + (isActive ? ' active' : '')}
              onClick={() => setMobileOpen(false)}
              title={n.label}
            >
              <Icon name={n.icon} />
              <span className="side-label">{n.label}</span>
            </NavLink>
          ))}
        </nav>
      </aside>
      {mobileOpen && <div className="sidebar-scrim" onClick={() => setMobileOpen(false)} />}

      <div className="main">
        <header className="topbar">
          <button className="hamburger icon-btn" onClick={() => setMobileOpen((o) => !o)} aria-label="Toggle menu">
            <Icon name="menu" />
          </button>
          <GlobalSearch />
          <div className="user-menu">
            <button className="avatar" onClick={() => setMenuOpen((o) => !o)} aria-label="User menu">
              {initials}
            </button>
            {menuOpen && (
              <>
                <div className="menu-scrim" onClick={() => setMenuOpen(false)} />
                <div className="menu">
                  <div className="menu-user">
                    <strong>{user?.display_name}</strong>
                    <span className="muted">{user?.email}</span>
                  </div>
                  <Link to="/settings" className="menu-item" onClick={() => setMenuOpen(false)}>
                    Settings
                  </Link>
                  <button
                    className="menu-item"
                    onClick={() => {
                      setMenuOpen(false);
                      logout();
                    }}
                  >
                    Log out
                  </button>
                </div>
              </>
            )}
          </div>
        </header>
        <main className="content">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
