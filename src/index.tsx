import { definePlugin } from "@decky/api";
import { FaSlidersH } from "react-icons/fa";
import { Content } from "./Content";
import { restoreAutomaticRecoveryGameFocus } from "./recoveryFocus";

export default definePlugin(() => {
  void restoreAutomaticRecoveryGameFocus();
  return {
    name: "RK-Enhanced",
    title: <div>RK-Enhanced</div>,
    content: <Content />,
    icon: <FaSlidersH />,
    alwaysRender: true,
  };
});
