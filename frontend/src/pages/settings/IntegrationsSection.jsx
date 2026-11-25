import ColloquiSection from './ColloquiSection';
import GoogleSection from './GoogleSection';
import RingCentralSection from './RingCentralSection';

/* ---------- Integrations ---------- */

export default function IntegrationsSection() {
  return (
    <div className="integrations">
      <ColloquiSection />
      <GoogleSection />
      <RingCentralSection />
    </div>
  );
}
