// PoolForm.tsx — pool node form: the bidirectional peers checkbox group
// (ADR-0019 V5 enforced by construction — toggling writes BOTH sides of the
// edge in the model via scopeModel.setPeer).

import { useT } from "../../../i18n";
import { Checkbox } from "../../ui/Checkbox";
import { HelperText } from "../../ui/HelperText";
import { FormSection } from "./FormSection";
import { asStringList, type PoolEntry } from "./scopeModel";

interface Props {
  pool: PoolEntry;
  otherPoolNames: string[];
  onSetPeer: (other: string, on: boolean) => void;
}

export function PoolForm({ pool, otherPoolNames, onSetPeer }: Props) {
  const t = useT();
  const peers = asStringList(pool.body.peers);
  return (
    <div className="space-y-4" data-testid="pools-pool-form">
      <h3 className="font-mono text-base font-semibold text-bright">
        {t("settings.poolsPanel.poolHeading", { name: pool.name })}
      </h3>
      <FormSection title={t("settings.poolsPanel.peers")}>
        <HelperText>{t("settings.poolsPanel.peersHelper")}</HelperText>
        {otherPoolNames.length === 0 ? (
          <p className="text-sm text-mute">{t("settings.poolsPanel.noOtherPools")}</p>
        ) : (
          <div className="grid grid-cols-2 gap-2">
            {otherPoolNames.map((name) => (
              <Checkbox
                key={name}
                label={name}
                checked={peers.includes(name)}
                onChange={(e) => onSetPeer(name, e.target.checked)}
              />
            ))}
          </div>
        )}
      </FormSection>
    </div>
  );
}
