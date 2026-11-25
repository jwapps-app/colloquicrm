/** Failure card for an integration whose status fetch didn't come back. */
export default function IntegrationErrorCard({ title, onRetry }) {
  return (
    <div className="card settings-card integration-card">
      <h3>{title}</h3>
      <div className="muted panel-empty">
        Couldn&apos;t load the integration status.{' '}
        <button className="linklike" onClick={onRetry}>
          Retry
        </button>
      </div>
    </div>
  );
}
