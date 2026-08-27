import { definePlugin } from "@decky/api";
import { FaSlidersH } from "react-icons/fa";
import { Content } from "./Content";
import { startInstallReadinessProbe } from "./installReadiness";
import { restoreAutomaticRecoveryGameFocus } from "./recoveryFocus";

export default definePlugin(() => {
  void restoreAutomaticRecoveryGameFocus();
  let cancelInstallReadiness = () => {};
  const plugin = {
    name: "RK-Enhanced",
    title: <div>RK-Enhanced</div>,
    content: <Content />,
    icon: <FaSlidersH />,
    alwaysRender: true,
    onDismount: () => cancelInstallReadiness(),
  };
  // Runs only after Decky invokes this registration factory for the exact
  // bundle. It deliberately does not depend on Quick Access being mounted.
  cancelInstallReadiness = startInstallReadinessProbe();
  return plugin;
});
